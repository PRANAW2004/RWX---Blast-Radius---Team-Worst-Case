"""Fingerprint sqlglot's import-time state: every class attribute and module global of
every sqlglot module, after importing every module and creating every dialect.

Comparing the fingerprints of the pinned source and a patched one tells exactly which
attributes a class- or module-level edit changed, including values a metaclass derives
at import (e.g. Snowflake.ESCAPED_SEQUENCES from Snowflake.Tokenizer.NUMERIC_ESCAPES).

Usage: python -S snapshot.py <root holding the sqlglot package> <out.pkl>
"""

import enum
import functools
import hashlib
import importlib
import os
import pickle
import pkgutil
import re
import sys
import types

SKIP_CLASS = {"__module__", "__qualname__", "__doc__", "__dict__", "__weakref__", "__firstlineno__",
              "__static_attributes__", "__annotations__", "__annotate__", "__orig_bases__", "__parameters__",
              "_abc_impl"}
BUILTIN_TYPES = (types.BuiltinFunctionType, types.WrapperDescriptorType, types.MethodWrapperType,
                 types.MemberDescriptorType, types.GetSetDescriptorType, types.MethodDescriptorType,
                 types.ClassMethodDescriptorType)


def qual(t):
    return f"{getattr(t, '__module__', '?')}.{getattr(t, '__qualname__', getattr(t, '__name__', '?'))}"


def const_shape(c):
    if isinstance(c, types.CodeType):
        return code_shape(c)
    if isinstance(c, (frozenset, set)):
        return (type(c).__name__, tuple(sorted(repr(const_shape(x)) for x in c)))
    if isinstance(c, tuple):
        return ("tuple", tuple(const_shape(x) for x in c))
    return (type(c).__name__, repr(c))


def code_shape(code):
    """Bytecode without file names or line numbers."""
    return ("code", code.co_qualname, code.co_code, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_flags, code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
            tuple(const_shape(c) for c in code.co_consts))


class Fingerprinter:
    def __init__(self, ordered=True):
        self.opaque = set()
        self.ordered = ordered  # False: dicts and sequences compare unordered (run-dependent entries)

    def canon(self, v, memo):
        if self.ordered:
            return self._canon(v, memo)
        # Unordered mode: memo holds only the current ancestors (cycle detection), so an
        # element's form does not depend on its siblings or on iteration order.
        key = id(v)
        result = self._canon(v, memo)
        entry = memo.get(key)
        if entry is not None and entry[1] is v and result[0] != "ref":
            del memo[key]
        return result

    def _canon(self, v, memo):
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            return (t.__name__, repr(v))
        if isinstance(v, enum.Enum):
            return ("enum", qual(type(v)), v.name)
        if isinstance(v, type):
            return ("class", qual(v))
        if isinstance(v, types.ModuleType):
            return ("module", v.__name__)
        if isinstance(v, types.CodeType):
            return code_shape(v)
        if isinstance(v, BUILTIN_TYPES):
            return ("builtin", qual(t), getattr(v, "__qualname__", getattr(v, "__name__", "")),
                    qual(getattr(v, "__objclass__", None) or type(getattr(v, "__self__", None))))
        key = id(v)
        if key in memo:
            return ("ref", memo[key][0])
        memo[key] = (len(memo), v)  # keep v alive so ids stay unique
        if t in (list, tuple):
            if not self.ordered:
                return (t.__name__, tuple(sorted(repr(self.canon(x, memo)) for x in v)))
            return (t.__name__, tuple(self.canon(x, memo) for x in v))
        if t in (set, frozenset):
            # Each element on its own: back-references must not depend on iteration order.
            return (t.__name__, tuple(sorted(repr(self.canon(x, memo if not self.ordered else {})) for x in v)))
        if isinstance(v, dict):
            if not self.ordered:
                return ("dict", qual(t), tuple(sorted(repr((self.canon(k, memo), self.canon(x, memo))) for k, x in v.items())))
            return ("dict", qual(t), tuple((self.canon(k, memo), self.canon(x, memo)) for k, x in v.items()))
        if isinstance(v, types.FunctionType):
            cells = []
            for cell in v.__closure__ or ():
                try:
                    cells.append(self.canon(cell.cell_contents, memo))
                except ValueError:
                    cells.append(("empty-cell",))
            return ("function", v.__module__, v.__qualname__, code_shape(v.__code__),
                    self.canon(v.__defaults__, memo), self.canon(v.__kwdefaults__, memo), tuple(cells))
        if isinstance(v, types.MethodType):
            return ("method", self.canon(v.__func__, memo), self.canon(v.__self__, memo))
        if isinstance(v, (staticmethod, classmethod)):
            return (t.__name__, self.canon(v.__func__, memo))
        if isinstance(v, property):
            return ("property", self.canon(v.fget, memo), self.canon(v.fset, memo), self.canon(v.fdel, memo))
        if isinstance(v, functools.partial):
            return ("partial", self.canon(v.func, memo), self.canon(v.args, memo), self.canon(v.keywords, memo))
        if isinstance(v, re.Pattern):
            return ("re", v.pattern, v.flags)
        if isinstance(v, types.MappingProxyType):
            return ("mappingproxy", self.canon(dict(v), memo))
        if t.__name__ in ("dict_values", "dict_keys", "dict_items"):
            return (t.__name__, self.canon(list(v), memo))
        if t.__module__ == "datetime":
            return ("datetime", repr(v))
        if t.__name__ in ("RLock", "_tuplegetter", "lock"):
            return ("structural", qual(t))  # locks and namedtuple field accessors hold no settings
        if t.__module__ in ("typing", "types") or t.__name__ in ("ForwardRef", "TypeVar", "ParamSpec"):
            return ("typing", repr(v))
        state = []
        if hasattr(v, "__dict__"):
            state.extend(sorted((k, self.canon(x, memo)) for k, x in vars(v).items()))
        for klass in t.__mro__:
            for slot in getattr(klass, "__slots__", ()) if isinstance(getattr(klass, "__slots__", ()), (tuple, list)) else ():
                if slot in ("__dict__", "__weakref__"):
                    continue
                try:
                    state.append((slot, self.canon(getattr(v, slot), memo)))
                except AttributeError:
                    state.append((slot, ("unset",)))
        if hasattr(v, "__dict__") or state:
            return ("object", qual(t), tuple(state))
        self.opaque.add(qual(t))
        return ("opaque", qual(t))

    def digest(self, v):
        return hashlib.sha1(repr(self.canon(v, {})).encode()).hexdigest()


def load_everything():
    failures = {}
    import sqlglot

    names = sorted(info.name for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."))
    for name in names:
        if name.endswith("__main__"):
            continue
        try:
            importlib.import_module(name)
        except BaseException as error:  # noqa: BLE001
            failures[name] = f"{type(error).__name__}"
    try:
        from sqlglot.dialects.dialect import Dialect, Dialects

        for dialect in Dialects:
            try:
                Dialect.get_or_raise(dialect.value)
            except BaseException as error:  # noqa: BLE001
                failures[f"dialect:{dialect.value}"] = f"{type(error).__name__}"
    except BaseException as error:  # noqa: BLE001
        failures["dialects"] = f"{type(error).__name__}"
    return failures


def snapshot(unordered_keys=()):
    failures = load_everything()
    fp = Fingerprinter()
    loose = Fingerprinter(ordered=False)
    unordered_keys = set(unordered_keys)

    def digest(key, value):
        return ("U", loose.digest(value)) if key in unordered_keys else fp.digest(value)

    result = {f"<import>|{k}": v for k, v in failures.items()}
    for name, module in sorted(sys.modules.items()):
        if module is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        classes = []
        for key, value in vars(module).items():
            if key.startswith("__") and key.endswith("__"):
                continue
            if isinstance(value, type) and value.__module__ == name:
                classes.append(value)
                continue
            if isinstance(value, types.ModuleType):
                continue
            result[f"{name}|{key}"] = digest(f"{name}|{key}", value)
        seen = set()
        while classes:
            klass = classes.pop()
            if klass in seen:
                continue
            seen.add(klass)
            owner = f"{name}:{klass.__qualname__}"
            for key, value in vars(klass).items():
                if key in SKIP_CLASS:
                    continue
                if isinstance(value, type) and value.__module__ == name and value.__qualname__.startswith(klass.__qualname__ + "."):
                    classes.append(value)
                result[f"{owner}|{key}"] = digest(f"{owner}|{key}", value)
            result[f"{owner}|<bases>"] = repr([qual(b) for b in klass.__bases__])
    result["<opaque>"] = sorted(fp.opaque)
    return result


def build_reference(root, out, seeds=("1", "2", "4", "5")):
    """Snapshot the pinned source in several runs. Entries that differ between runs (built
    by iterating sets) are compared order-insensitively; any that still differ are volatile:
    if one differs for a patch, the comparison is abandoned for that commit."""
    import subprocess

    def run(seed, keys=None, pad=0):
        path = f"{out}.run"
        command = [sys.executable, "-S", __file__, root, path] + ([keys] if keys else [])
        # Different environment sizes shift memory addresses, hence the order of sets of
        # objects hashed by identity: vary them as well as the hash seed.
        env = {**os.environ, "PYTHONHASHSEED": seed, "SNAPSHOT_PAD": "x" * pad}
        subprocess.run(command, check=True, env=env)
        with open(path, "rb") as f:
            data = pickle.load(f)
        os.remove(path)
        return data

    def differing(runs):
        keys = set().union(*runs)
        return sorted(k for k in keys if not k.startswith("<") and len({repr(r.get(k)) for r in runs}) > 1)

    variants = [(seed, pad) for seed in seeds for pad in (0, 4099)] + [("3", pad) for pad in (17, 777, 9001)]
    loose = differing([run(seed, pad=pad) for seed, pad in variants])
    keys_path = f"{out}.keys"
    with open(keys_path, "wb") as f:
        pickle.dump(loose, f)
    final = [run(seed, keys_path, pad) for seed, pad in [("3", 0)] + variants]
    volatile = differing(final)
    os.remove(keys_path)
    data = final[0]
    data["<unordered>"] = loose
    data["<volatile>"] = volatile
    with open(out, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"reference: {len(data)} entries, {len(loose)} order-dependent, volatile: {volatile}", file=sys.stderr)


if __name__ == "__main__":
    if sys.argv[1] == "--reference":
        build_reference(sys.argv[2], sys.argv[3])
        sys.exit(0)
    root, out = sys.argv[1], sys.argv[2]
    unordered = ()
    if len(sys.argv) > 3:
        with open(sys.argv[3], "rb") as f:
            unordered = pickle.load(f)
    sys.path.insert(0, root)
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    data = snapshot(unordered)
    with open(out, "wb") as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"{len(data)} entries, opaque types: {data['<opaque>']}", file=sys.stderr)
