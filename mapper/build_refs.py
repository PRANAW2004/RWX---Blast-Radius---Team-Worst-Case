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
                for node in ast.walk(tree):
                    ident = None
                    if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                        ident = node.attr
                    elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                        ident = node.id
                    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                        ident = node.value
                    if ident and CAPS.match(ident):
                        refs.setdefault(ident, set()).add((rel,) + innermost(spans, node.lineno))
    return {k: sorted(v) for k, v in refs.items()}
