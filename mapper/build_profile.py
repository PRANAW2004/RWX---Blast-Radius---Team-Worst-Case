"""Build dictionary, dispatch-call and comparison coverage from the unpatched test suite.

Usage: python build_profile.py PINNED_SOURCE tests.json profile.json.gz
Requires the locked dependencies and CPython 3.13 on Linux.
"""

import ast
import builtins
import concurrent.futures
import contextlib
import gzip
import importlib
import json
import multiprocessing
import os
from pathlib import Path
import pickle
import sys
import tempfile
import unittest
from collections import defaultdict

import profile_runtime as rt
import spawn_capture


def location(path, n):
    return f"{path}:{n.lineno}:{n.col_offset}"


def literal(n):
    try:
        return type(ast.literal_eval(n)) in (bool, int, str, tuple, bytes, type(None))
    except (ValueError, TypeError):
        return False


class Instrument(ast.NodeTransformer):
    def __init__(self, path, registry):
        self.path = path
        self.registry = registry

    def probe(self, node, method, args):
        site = method + ":" + location(self.path, node)
        idx = self.registry.setdefault(site, len(self.registry))
        call = ast.Call(
            ast.Name("_blast_" + method, ast.Load()), [ast.Constant(idx), *args], []
        )
        return ast.copy_location(call, node)

    def visit_Compare(self, n):
        self.generic_visit(n)
        if (
            len(n.ops) == 1
            and type(n.ops[0]).__name__ in rt.comparators
            and literal(n.comparators[0])
        ):
            return self.probe(
                n,
                "compare",
                [ast.Constant(type(n.ops[0]).__name__), n.left, n.comparators[0]],
            )
        return n

    def visit_ClassDef(self, cls):
        self.generic_visit(cls)
        for stmt in cls.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(
                stmt.value, ast.Dict
            ):
                continue
            d = stmt.value
            if (
                d.keys
                and all(
                    isinstance(k, ast.Constant) and isinstance(k.value, str)
                    for k in d.keys
                )
                and all(literal(v) for v in d.values)
            ):
                stmt.value = self.probe(d, "dict", [d])
            # A lambda stored directly in a dispatch table carries its site even
            # when Python copies the dictionary into another registry.
            for i, v in enumerate(d.values):
                if isinstance(v, ast.Lambda):
                    d.values[i] = self.probe(v, "called", [v])
                elif (
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id in self.factories
                ):
                    d.values[i] = self.probe(v, "called", [v])
        return cls


def compile_sources(root, registry):
    codes = {}
    parsed = {
        path: ast.parse(path.read_bytes(), str(path))
        for path in sorted((root / "sqlglot").rglob("*.py"))
    }
    factories = {}
    for path, tree in parsed.items():
        rel = path.relative_to(root).as_posix()

        transformer = Instrument(rel, registry)
        # Only wrap factories whose bodies merely form and return a function.
        transformer.factories = set()
        for n in tree.body:
            if isinstance(n, ast.FunctionDef):
                body = n.body
                if isinstance(body[0], ast.Expr) and isinstance(
                    body[0].value, ast.Constant
                ):
                    body = body[1:]
                if (
                    len(body) == 1
                    and isinstance(body[0], ast.Return)
                    and isinstance(body[0].value, ast.Lambda)
                    or len(body) == 2
                    and isinstance(body[0], ast.FunctionDef)
                    and not body[0].decorator_list
                    and isinstance(body[1], ast.Return)
                    and isinstance(body[1].value, ast.Name)
                    and body[1].value.id == body[0].name
                ):
                    transformer.factories.add(n.name)
        mod = rel[:-3].replace("/", ".")
        factories[mod] = transformer.factories
    for path, tree in parsed.items():
        rel = path.relative_to(root).as_posix()
        transformer = Instrument(rel, registry)
        transformer.factories = set(factories[rel[:-3].replace("/", ".")])
        for n in tree.body:
            if isinstance(n, ast.ImportFrom) and n.module in factories:
                for alias in n.names:
                    if alias.name in factories[n.module]:
                        transformer.factories.add(alias.asname or alias.name)
        tree = transformer.visit(tree)
        codes[str(path)] = compile(ast.fix_missing_locations(tree), str(path), "exec")
    return codes


def enable(root, directory):
    from importlib.machinery import SourceFileLoader

    original = SourceFileLoader.get_code
    registry = {}
    codes = compile_sources(root, registry)
    spawn_capture.install(directory / "codes", directory, codes)

    def get_code(loader, fullname):
        if loader.path in codes:
            return codes[loader.path]
        return original(loader, fullname)

    SourceFileLoader.get_code = get_code
    builtins._blast_compare = rt.compare
    builtins._blast_dict = rt.ObservedDict
    builtins._blast_called = rt.called
    return registry


def profile_file(job):
    root, directory, tests = job
    os.chdir(root)
    rt.events.clear()
    module = tests[0]["file"][:-3].replace("/", ".")
    with (
        open(os.devnull, "w") as sink,
        contextlib.redirect_stdout(sink),
        contextlib.redirect_stderr(sink),
    ):
        try:
            importlib.import_module(module)
        except Exception:
            return
        for t in tests:
            # New process per test isolates caches and preserves import events.
            pid = os.fork()
            if pid == 0:
                try:
                    dest = directory / ("workers-" + str(t["number"]))
                    os.environ["BLAST_TRACE_CHILD"] = str(dest)
                    pids = spawn_capture.expected_workers()
                    spawned = []

                    def audit(event, args):
                        if event in ("os.fork", "os.posix_spawn", "subprocess.Popen"):
                            spawned.append(event)

                    sys.addaudithook(audit)
                    suite = unittest.defaultTestLoader.loadTestsFromName(
                        t["id"].replace("::", ".").replace(t["file"], module, 1)
                    )
                    result = unittest.TestResult()
                    suite.run(result)
                    if (
                        result.testsRun == 1
                        and result.wasSuccessful()
                        and not (spawned or result.skipped)
                    ):
                        if any(not (dest / str(pid)).exists() for pid in pids):
                            os._exit(0)
                        if dest.exists():
                            for child in dest.iterdir():
                                rt.events.update(pickle.loads(child.read_bytes()))
                        data = {
                            (i, v)
                            for i, v in rt.events
                            if v is None or type(v) in (str, int)
                        }
                        with open(directory / str(t["number"]), "wb") as f:
                            pickle.dump(data, f)
                finally:
                    os._exit(0)
            os.waitpid(pid, 0)


def main():
    root = Path(sys.argv[1]).resolve()
    tests = json.loads(Path(sys.argv[2]).read_bytes())["tests"]
    output = Path(sys.argv[3]).resolve()
    os.environ.update(
        SKIP_INTEGRATION="0", OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1"
    )
    # Preload non-project heavy packages for copy-on-write sharing across forks.
    for package in ("duckdb", "numpy", "pandas", "pytest"):
        importlib.import_module(package)
    sys.path.insert(0, str(root))
    bootstrap_tmp = tempfile.TemporaryDirectory()
    registry = enable(root, Path(bootstrap_tmp.name))
    groups = defaultdict(list)
    for t in tests:
        groups[t["file"]].append(t)
    groups = sorted(groups.values(), key=lambda g: -sum(t["seconds"] for t in g))
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=4, mp_context=multiprocessing.get_context("fork")
        ) as pool:
            # One fresh module-import context per file, even when a pool process is reused.
            list(pool.map(isolated_file, [(root, directory, g) for g in groups]))
        mask = 0
        events = defaultdict(dict)
        for file in directory.iterdir():
            if not file.is_file():
                continue
            bit = 1 << int(file.name)
            mask |= bit
            for idx, val in pickle.loads(file.read_bytes()):
                key = json.dumps(val, separators=(",", ":"))
                events[idx][key] = events[idx].get(key, 0) | bit
        payload = {
            "complete": hex(mask),
            "sites": {
                name: {k: hex(v) for k, v in events.get(idx, {}).items()}
                for name, idx in registry.items()
            },
        }
        with gzip.open(output, "wt") as f:
            json.dump(payload, f, separators=(",", ":"))
        print(mask.bit_count(), "complete tests;", len(registry), "sites", flush=True)


def isolated_file(job):
    pid = os.fork()
    if pid == 0:
        try:
            profile_file(job)
        finally:
            os._exit(0)
    os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
