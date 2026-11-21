"""Run every test in its own forked process and record, per test:
  - line coverage of sqlglot/ and tests/ (including import-time code),
  - files it opens under the repository (fixtures, source read as text),
  - the sqlglot classes it instantiates (with their MRO).
Usage: python run_isolated.py <sqlglot_root> <tests.json> <out_dir> [jobs]
"""
import builtins, io, json, os, sys, time

def main():
    root, tests_json, out_dir = (os.path.realpath(p) for p in sys.argv[1:4])
    jobs = int(sys.argv[4]) if len(sys.argv) > 4 else 4
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["SKIP_INTEGRATION"] = "0"
    os.makedirs(out_dir, exist_ok=True)

    # Heavy, sqlglot-free imports happen once in the parent and are shared by fork.
    import coverage, pytest  # noqa: E402
    for heavy in ("duckdb", "pandas", "numpy", "dateutil", "pytz"):
        try:
            __import__(heavy)
        except Exception:
            pass

    tests = json.load(open(tests_json))["tests"]


    def child(test):  # uses root/out_dir from main()
        number, nodeid = test["number"], test["id"]
        parts = os.path.join(out_dir, f"parts.{number}")
        os.makedirs(parts, exist_ok=True)
        include = [os.path.join(root, "sqlglot", "*"), os.path.join(root, "tests", "*")]
        # Process pools (spawn) in the optimizer and executor tests run sqlglot in
        # child processes: measure them too and combine afterwards.
        rcfile = os.path.join(parts, "coveragerc")
        with open(rcfile, "w") as f:
            f.write(
                "[run]\n"
                f"data_file = {os.path.join(parts, 'cov')}\n"
                "parallel = True\n"
                "branch = False\n"
                "concurrency = multiprocessing,thread\n"
                "include =\n    " + "\n    ".join(include) + "\n"
            )
        cov = coverage.Coverage(config_file=rcfile)
        reads = set()
        real_open = builtins.open

        def traced_open(file, *args, **kwargs):
            if isinstance(file, (str, bytes, os.PathLike)):
                try:
                    path = os.path.realpath(os.fsdecode(file))
                    if path.startswith(root + os.sep):
                        reads.add(os.path.relpath(path, root))
                except Exception:
                    pass
            return real_open(file, *args, **kwargs)

        builtins.open = traced_open
        io.open = traced_open
        cov.start()

        # Every test module imports sqlglot, so importing it here first changes nothing,
        # and lets us wrap the __init__ of its classes to record instantiation.
        import sqlglot  # noqa: F401
        instantiated = set()
        wrapped = set()

        def wrap_module_classes():
            for name, module in list(sys.modules.items()):
                if not name.startswith("sqlglot") or module is None:
                    continue
                for value in list(vars(module).values()):
                    if isinstance(value, type) and value.__module__ == name and "__init__" in value.__dict__ and value not in wrapped:
                        init = value.__dict__["__init__"]
                        if not callable(init):
                            continue
                        wrapped.add(value)

                        def make(init):
                            def __init__(self, *a, **k):
                                instantiated.add(type(self))
                                return init(self, *a, **k)
                            __init__.__wrapped__ = init
                            return __init__
                        try:
                            value.__init__ = make(init)
                        except Exception:
                            pass

        wrap_module_classes()

        class Plugin:
            @pytest.hookimpl(tryfirst=True)
            def pytest_runtest_setup(self, item):
                wrap_module_classes()  # classes from lazily imported dialects

        outcome = "?"
        try:
            code = pytest.main([nodeid, "-q", "-p", "no:cacheprovider", "-p", "no:randomly", "--no-header",
                                "-o", "addopts=", "--timeout=600"], plugins=[Plugin()])
            outcome = int(code)
        except BaseException as error:  # noqa: BLE001
            outcome = repr(error)
        cov.stop()
        cov.save()
        combined = coverage.CoverageData(os.path.join(out_dir, f"cov.{number}"))
        for name in os.listdir(parts):
            if name.startswith("cov."):
                piece = coverage.CoverageData(os.path.join(parts, name))
                piece.read()
                combined.update(piece)
        combined.write()
        subprocess_count = sum(1 for name in os.listdir(parts) if name.startswith("cov.")) - 1
        classes = set()
        for cls in instantiated:
            for base in cls.__mro__:
                module = getattr(base, "__module__", "") or ""
                if module.startswith("sqlglot"):
                    classes.add(f"{module}:{base.__qualname__}")
        with real_open(os.path.join(out_dir, f"meta.{number}.json"), "w") as f:
            json.dump({"number": number, "outcome": outcome, "subprocesses": subprocess_count, "reads": sorted(reads), "classes": sorted(classes)}, f)


    done_marker = lambda n: os.path.exists(os.path.join(out_dir, f"meta.{n}.json"))
    todo = [t for t in tests if not done_marker(t["number"])]
    todo.sort(key=lambda t: -t["seconds"])  # longest first
    running = {}
    start = time.time()
    completed = 0
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
        completed += 1
        if completed % 100 == 0:
            print(f"{completed} done, {len(todo)} left, {time.time() - start:.0f}s", flush=True)
    print("finished", time.time() - start, flush=True)


if __name__ == '__main__':
    main()
