"""Index operand types from the unchanged checkout. No patches or test labels are inputs.

Run in the pinned SQLGlot checkout with mypy and dependencies from requirements.lock:
    python /path/to/mapper/build_type_map.py /path/to/type-map.json
"""

import ast
import enum
import importlib
import json
from pathlib import Path
import sys

from mypy.build import build
from mypy.main import process_options
from mypy.nodes import ComparisonExpr, TypeInfo
from mypy.server.subexpr import get_subexpressions
from mypy.types import Instance, LiteralType, NoneType, UnionType, get_proper_type


def nominal_types(t):
    t = get_proper_type(t)
    if isinstance(t, UnionType):
        return {name for item in t.items for name in nominal_types(item)}
    if isinstance(t, LiteralType):
        t = t.fallback
    if isinstance(t, Instance):
        return {t.type.fullname}
    if isinstance(t, NoneType):
        return {"None"}
    return {"?"}


def main(output):
    sys.path.insert(0, str(Path.cwd()))
    sources, options = process_options(
        [
            "--no-incremental",
            "--check-untyped-defs",
            "--ignore-missing-imports",
            "sqlglot",
        ]
    )
    options.export_types = options.preserve_asts = True
    result = build(sources=sources, options=options)
    classes = {}
    for mod, state in result.graph.items():
        if mod.startswith("sqlglot") and state.tree:
            for symbol in state.tree.names.values():
                if isinstance(symbol.node, TypeInfo):
                    classes[symbol.node.fullname] = symbol.node

    # Only plain identity-comparing classes and conventional singleton enums.
    # Reject custom equality, including overrides on known project subclasses.
    categories = {"None": "identity", "builtins.bool": "bool"}
    for name, info in classes.items():
        if not name.startswith("sqlglot."):
            continue
        if info.is_protocol or info.is_abstract:
            continue
        try:
            cls = getattr(importlib.import_module(info.module_name), info.name)
        except (ImportError, AttributeError):
            continue
        if not isinstance(cls, type) or cls.__module__ + "." + cls.__qualname__ != name:
            continue
        if isinstance(cls, enum.EnumMeta):
            if (
                cls.__eq__ in (str.__eq__, int.__eq__, object.__eq__)
                and cls.__ne__ in (str.__ne__, int.__ne__, object.__ne__)
                and all(
                    (a == b) == (a is b) and (a != b) == (a is not b)
                    for a in cls
                    for b in cls
                )
            ):
                categories[name] = name
        elif cls.__eq__ is object.__eq__ and cls.__ne__ is object.__ne__:
            # Equality can dispatch to either operand's subclass implementation.
            stack = [cls]
            visited = set()
            while stack:
                child = stack.pop()
                if child in visited:
                    continue
                visited.add(child)
                if (
                    child.__eq__ is not object.__eq__
                    or child.__ne__ is not object.__ne__
                ):
                    break
                stack.extend(child.__subclasses__())
            else:
                if all(
                    all(
                        child.get_containing_type_info(op)
                        is info.get_containing_type_info(op)
                        for op in ("__eq__", "__ne__")
                    )
                    for child in classes.values()
                    if info in child.mro
                ):
                    categories[name] = "identity"

    sites = {}
    used_types = set()
    for mod, state in result.graph.items():
        if not mod.startswith("sqlglot") or not state.tree:
            continue
        path = Path(state.path)
        positions = {
            (n.lineno, n.col_offset)
            for n in ast.walk(ast.parse(path.read_bytes()))
            if isinstance(n, ast.Compare) and len(n.ops) == 1
        }
        entries = {}
        for node in get_subexpressions(state.tree):
            if not isinstance(node, ComparisonExpr) or len(node.operands) != 2:
                continue
            if (node.line, node.column) not in positions or node.operators[0] not in (
                "is",
                "is not",
                "==",
                "!=",
            ):
                continue
            pair = [sorted(nominal_types(result.types.get(v))) for v in node.operands]
            if all(t in categories for side in pair for t in side):
                entries[f"{node.line},{node.column}"] = pair
                used_types.update(pair[0] + pair[1])
        if entries:
            sites[path.as_posix()] = entries
    Path(output).write_text(
        json.dumps(
            {"types": {t: categories[t] for t in sorted(used_types)}, "sites": sites},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main(sys.argv[1])
