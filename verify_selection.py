"""Run inexpensive candidate groups against the actual patch, within a bounded budget."""

import concurrent.futures
import multiprocessing
import json
import os
from pathlib import Path
import resource
import shutil
import signal
import sys
import tempfile
import time
import unittest


class Probe:
    def __init__(self, source, data):
        self.source = Path(source).resolve()
        self.tests = json.loads((Path(data) / "tests.json").read_bytes())["tests"]
        self.stage = None
        self.spent = 0.0
        self.started = time.monotonic()

    def prepare(self):
        if self.stage is not None:
            return
        self.temp = tempfile.TemporaryDirectory(prefix="blast-")
        self.stage = Path(self.temp.name)
        shutil.copytree(
            self.source,
            self.stage,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns(
                ".git", ".mypy_cache", ".pytest_cache", ".ruff_cache"
            ),
        )
        # In the submission image these imports are shared across probe forks.
        # A missing optional dependency turns its test into an error and it is retained.
        import site

        site.main()
        for name in ("pandas", "duckdb", "pytz"):
            try:
                __import__(name)
            except ImportError:
                pass

    def filter(self, changes, numbers, ms, apply_hunks):
        total = sum(ms[n] for n in numbers)
        if (
            not numbers
            or total > 750
            or len(numbers) > 32
            or len(changes) != 1
            or self.spent >= 8
            or time.monotonic() - self.started > 75
        ):
            return numbers
        patch = changes[0]
        path = patch["old"]
        if (
            not path
            or path != patch["new"]
            or not path.startswith("sqlglot/")
            or not path.endswith(".py")
            or ".." in Path(path).parts
            or patch["binary"]
        ):
            return numbers
        self.prepare()
        src = self.source / path
        target = self.stage / path
        original = src.read_bytes()
        lines = original.decode("utf8").splitlines(keepends=True)
        modified = "".join(apply_hunks(lines, patch["hunks"])[0]).encode()
        cached = target.parent / "__pycache__"
        if cached.is_dir():
            for file in cached.glob(target.stem + ".*.pyc"):
                file.unlink()
        target.write_bytes(modified)
        read_fd, write_fd = os.pipe()
        pid = None
        try:
            pid = os.fork()
            if pid == 0:
                try:
                    os.close(read_fd)
                    os.setsid()
                    signal.alarm(5)
                    resource.setrlimit(resource.RLIMIT_CPU, (4, 4))
                    with open(os.devnull, "w") as sink:
                        os.dup2(sink.fileno(), 1)
                        os.dup2(sink.fileno(), 2)
                        os.chdir(self.stage)
                        sys.path.insert(0, str(self.stage))
                        os.environ["SKIP_INTEGRATION"] = "0"
                        # Keep query tasks in one worker. Forking preserves the staged
                        # interpreter and avoids importing an unpatched source tree.
                        pool = concurrent.futures.ProcessPoolExecutor

                        class ProbePool(pool):
                            def __init__(
                                self, max_workers=None, mp_context=None, **kwargs
                            ):
                                super().__init__(
                                    max_workers=1,
                                    mp_context=multiprocessing.get_context("fork"),
                                    **kwargs,
                                )

                        concurrent.futures.ProcessPoolExecutor = ProbePool
                        # Running the class suite together shares setUpClass, as pytest does.
                        by_id = {}
                        suite = unittest.TestSuite()
                        for n in numbers:
                            name = (
                                self.tests[n]["id"]
                                .replace(".py::", ".")
                                .replace("/", ".")
                                .replace("::", ".")
                            )
                            suite.addTests(
                                unittest.defaultTestLoader.loadTestsFromName(name)
                            )
                            by_id[name] = n

                        class Successes(unittest.TestResult):
                            def __init__(self):
                                super().__init__()
                                self.partial = set()

                            def addSkip(self, test, reason):
                                super().addSkip(test, reason)
                                self.partial.add(getattr(test, "test_case", test).id())

                            def addSuccess(self, test):
                                super().addSuccess(test)
                                n = by_id.get(test.id())
                                if n is not None and test.id() not in self.partial:
                                    os.write(write_fd, f"{n}\n".encode())

                        result = Successes()
                        suite.run(result)
                        # Class/module cleanup errors have no individual test ID.
                        if any(test.id() not in by_id for test, _ in result.errors):
                            os._exit(1)
                finally:
                    os._exit(0)
            os.close(write_fd)
            write_fd = None
            _, status, usage = os.wait4(pid, 0)
            self.spent += usage.ru_utime + usage.ru_stime
            if not os.WIFEXITED(status) or os.WEXITSTATUS(status):
                return numbers
            with os.fdopen(os.dup(read_fd), "rb") as f:
                passed = {int(n) for n in f}
            return [n for n in numbers if n not in passed]
        finally:
            os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            target.write_bytes(original)
