"""Run every test in its own forked process with every single-operator is / is not /
== / != comparison in sqlglot instrumented, and record, per test, the sites where an
operand pair was not identity-safe (a type defining its own ==).

Swapping `is` with `==` (or `is not` with `!=`) at a site cannot change any test
whose every operand pair there was identity-safe: `==` falls back to identity, so
the test's execution is exactly the same.

Usage: python run_probes.py <sqlglot_root> <tests.json> <out_dir> [jobs]
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import marshal
import operator
import os
import sys
import time

OP_IDS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3}
OPS = [operator.is_, operator.is_not, operator.eq, operator.ne]
_seen = {}  # site -> set of (type(a), type(b))


def _probe(site, op, a, b):
    pairs = _seen.get(site)
    if pairs is None:
        pairs = _seen[site] = set()
    pairs.add((type(a), type(b)))
    return OPS[op](a, b)


def identity_safe(ta, tb):
    """True when `a == b` is exactly `a is b` (and `!=` is `is not`) for these types."""
    def owner(t, name):
        return next((k for k in getattr(t, "__mro__", ()) if name in k.__dict__), None)

    def plain(t):
        # object's identity comparison; NoneType's is the same since None is a singleton.
        return owner(t, "__eq__") in (object, type(None)) and owner(t, "__ne__") in (object, type(None))

    if plain(ta) and plain(tb):
        return True
    import enum

    # Members of one enum class: equal values means the same member (aliases are one object).
    if ta is tb and isinstance(ta, type) and issubclass(ta, enum.Enum):
        return ta.__eq__ in (object.__eq__, str.__eq__, int.__eq__) and ta.__ne__ in (object.__ne__, str.__ne__, int.__ne__)
    return False


class _Rewriter(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OP_IDS:
            return node
        site = f"{self.rel}:{node.lineno}:{node.col_offset}"
        call = ast.Call(
            func=ast.Name(id="__blast_probe__", ctx=ast.Load()),
            args=[ast.Constant(site), ast.Constant(OP_IDS[type(node.ops[0])]), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    root = None
    cache = None

    def get_code(self, fullname):
        path = self.get_filename(fullname)
        rel = os.path.relpath(path, self.root)
        key = os.path.join(self.cache, rel.replace(os.sep, "__") + ".marshal")
        try:
            with open(key, "rb") as f:
                return marshal.load(f)
        except OSError:
            pass
        source = self.get_data(path)
        tree = _Rewriter(rel).visit(ast.parse(source, path))
        ast.fix_missing_locations(tree)
        code = compile(tree, path, "exec", dont_inherit=True)
        tmp = f"{key}.{os.getpid()}"
        with open(tmp, "wb") as f:
            marshal.dump(code, f)
        os.replace(tmp, key)
        return code


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        if not os.path.realpath(spec.origin).startswith(_Loader.root + os.sep):
            return spec
        spec.loader = _Loader(fullname, spec.origin)
        return spec


def install(root, cache):
    _Loader.root, _Loader.cache = root, cache
    os.makedirs(cache, exist_ok=True)
    builtins.__blast_probe__ = _probe
    sys.meta_path.insert(0, _Finder())


def unsafe_sites():
    return sorted(site for site, pairs in _seen.items() if not all(identity_safe(a, b) for a, b in pairs))


def dump(path):
    with open(path, "w") as f:
        json.dump(unsafe_sites(), f)


if __name__ == "__mp_main__" and os.environ.get("BLAST_PROBE_OUT"):
    # A spawn process-pool worker of a test.
    import atexit
    import multiprocessing.util

    install(os.environ["BLAST_PROBE_ROOT"], os.environ["BLAST_PROBE_CACHE"])
    _out = os.environ["BLAST_PROBE_OUT"] + f".{os.getpid()}.json"
    multiprocessing.util.Finalize(None, lambda: dump(_out), exitpriority=100)
    atexit.register(lambda: dump(_out))


def main():
    root, tests_json, out_dir = (os.path.realpath(p) for p in sys.argv[1:4])
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    cache = os.path.join(out_dir, "code-cache")
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["BLAST_PROBE_ROOT"] = root
    os.environ["BLAST_PROBE_CACHE"] = cache
    os.makedirs(out_dir, exist_ok=True)
    import pytest  # noqa: F401

    for heavy in ("duckdb", "pandas", "numpy", "dateutil", "pytz"):
        try:
            __import__(heavy)
        except Exception:
            pass
    tests = json.load(open(tests_json))["tests"]

    def child(test):
        number = test["number"]
        parts = os.path.join(out_dir, f"parts.{number}")
        os.makedirs(parts, exist_ok=True)
        os.environ["BLAST_PROBE_OUT"] = os.path.join(parts, "worker")
        install(root, cache)
        import pytest

        code = pytest.main([test["id"], "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--no-header",
                            "-o", "addopts=", "--timeout=900"])
        unsafe = set(unsafe_sites())
        for name in os.listdir(parts):
            if name.startswith("worker."):
                with open(os.path.join(parts, name)) as f:
                    unsafe.update(json.load(f))
        with open(os.path.join(out_dir, f"probe.{number}.json"), "w") as f:
            json.dump({"number": number, "outcome": int(code), "unsafe": sorted(unsafe)}, f)

    # Warm the code cache once so children do not all rewrite the same modules.
    if os.fork() == 0:
        try:
            install(root, cache)
            import pkgutil
            import importlib
            import sqlglot

            for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
                try:
                    importlib.import_module(info.name)
                except Exception:
                    pass
        finally:
            os._exit(0)
    os.wait()

    todo = [t for t in tests if not os.path.exists(os.path.join(out_dir, f"probe.{t['number']}.json"))]
    todo.sort(key=lambda t: -t["seconds"])
    running, done, start = {}, 0, time.time()
    while todo or running:
        while todo and len(running) < jobs:
            test = todo.pop(0)
            pid = os.fork()
            if pid == 0:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                try:
                    child(test)
                except BaseException:
                    import traceback

                    with open(os.path.join(out_dir, f"err.{test['number']}"), "w") as f:
                        traceback.print_exc(file=f)
                finally:
                    os._exit(0)
            running[pid] = test
        pid, _ = os.wait()
        running.pop(pid, None)
        done += 1
        if done % 100 == 0:
            print(f"{done} done, {len(todo)} left, {time.time() - start:.0f}s", flush=True)
    print("finished", time.time() - start, flush=True)


if __name__ == "__main__":
    main()
