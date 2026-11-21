"""Collect events from genuine spawned Python workers before they import the project."""

import atexit
import builtins
import importlib.machinery
import marshal
import multiprocessing.process
import os
from pathlib import Path
import pickle
import sys


def install(cache, directory, codes):
    with open(cache, "wb") as f:
        marshal.dump(codes, f)
    hook = directory / "sitecustomize.py"
    hook.write_text("from spawn_capture import bootstrap\nbootstrap()\n")
    os.environ["PYTHONPATH"] = os.pathsep.join(
        (
            str(directory),
            str(Path(__file__).resolve().parent),
            os.environ.get("PYTHONPATH", ""),
        )
    )
    os.environ["BLAST_TRACE_CODE"] = str(cache)


def bootstrap():
    dest = os.environ.get("BLAST_TRACE_CHILD")
    if not dest or any(
        "multiprocessing.resource_tracker import main" in a for a in sys.orig_argv
    ):
        return
    import profile_runtime as rt

    with open(os.environ["BLAST_TRACE_CODE"], "rb") as f:
        codes = marshal.load(f)
    loader = importlib.machinery.SourceFileLoader
    prior = loader.get_code

    def get_code(self, fullname):
        if self.path in codes:
            return codes[self.path]
        return prior(self, fullname)

    loader.get_code = get_code
    builtins._blast_called = rt.called
    builtins._blast_compare = rt.compare
    builtins._blast_dict = rt.ObservedDict

    def save():
        path = Path(dest) / str(os.getpid())
        path.parent.mkdir(exist_ok=True)
        with path.open("wb") as f:
            pickle.dump(rt.events, f)

    atexit.register(save)


def expected_workers():
    pids = []
    prior = multiprocessing.process.BaseProcess.start

    def start(process):
        ret = prior(process)
        pids.append(process.pid)
        return ret

    multiprocessing.process.BaseProcess.start = start
    return pids
