"""Index where every ALL_CAPS name is read in the pinned source: `.NAME`, a bare
`NAME`, or the string "NAME" (as in `"NAME" in cls.__dict__`). Each read maps to the
lines of its innermost statement (a compound statement's header only).

Usage: python build_refs.py <sqlglot_root>  -> prints a pickle-able dict on import
"""
import ast
import os
import re

CAPS = re.compile(r"^[A-Z][A-Z0-9_]*$")


def statement_spans(tree):
    """(start, end) of each statement's own lines, innermost last."""
    spans = []
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt):
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.AST) and not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                spans.append((node.lineno, max(node.lineno, body[0].lineno - 1)))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([node.lineno] + [d.lineno for d in node.decorator_list])
                end = node.body[0].lineno - 1 if node.body else node.end_lineno
                spans.append((first, max(first, end)))
            else:
                spans.append((node.lineno, node.end_lineno))
    return spans


def innermost(spans, line):
    best = None
    for start, end in spans:
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end)
    return best or (line, line)


def build(root):
    refs = {}
    for base in ("sqlglot", "tests"):
        for dirpath, _, names in os.walk(os.path.join(root, base)):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                rel = os.path.relpath(path, root)
                try:
                    tree = ast.parse(open(path, "rb").read())
                except SyntaxError:
                    continue
                spans = statement_spans(tree)
                # Names read inside a default value only matter to calls using that default.
                in_default = {}
                for fn in ast.walk(tree):
                    if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        site = f"{rel}:{fn.lineno}:{fn.col_offset}"
                        keyword = [d for d in fn.args.kw_defaults if d is not None]
                        for position, default in enumerate(list(fn.args.defaults) + keyword):
                            for inner in ast.walk(default):
                                in_default[id(inner)] = (site, position)
                for node in ast.walk(tree):
                    ident = None
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                        ident = node.attr
                    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        ident = node.id
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        ident = node.value
                    if ident and CAPS.match(ident):
                        if id(node) in in_default:
                            refs.setdefault(ident, set()).add((rel, "default") + in_default[id(node)])
                        else:
                            # "attr": `x.NAME`, which run_probes.py records with the class or module
                            # it was read from; "name" / "str": bare names and string literals,
                            # which it cannot see.
                            kind = "attr" if isinstance(node, ast.Attribute) else (
                                "name" if isinstance(node, ast.Name) else "str")
                            refs.setdefault(ident, set()).add((rel,) + innermost(spans, node.lineno) + (kind,))
    return {k: sorted(v, key=str) for k, v in refs.items()}
