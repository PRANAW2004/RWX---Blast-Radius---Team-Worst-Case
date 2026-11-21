"""Collect coverage of direct reads of uppercase scalar class attributes.

Usage: python mapper/build_attribute_map.py PINNED_CHECKOUT map.pkl.gz attributes.json
String references disqualify a name because getattr or a registry may consume it.
"""

import ast
import gzip
import json
import pickle
import sys
from pathlib import Path


def collect(root, coverage):
    candidates, indirect, users = set(), set(), {}
    for folder in ("sqlglot", "tests"):
        for path in sorted((root / folder).rglob("*.py")):
            lines = coverage["lines"].get(path.relative_to(root).as_posix(), {})
            tree = ast.parse(path.read_bytes())
            parents = {
                child: node
                for node in ast.walk(tree)
                for child in ast.iter_child_nodes(node)
            }
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value.isidentifier()
                ):
                    indirect.add(n.value)
                if isinstance(n, ast.ClassDef):
                    for stmt in n.body:
                        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                            target = stmt.targets[0]
                            if isinstance(target, ast.Name) and target.id.isupper():
                                candidates.add(target.id)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                    name = n.attr
                elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
                    name = n.id
                else:
                    continue
                if not name.isupper():
                    continue
                # A multiline expression's trace event may be on its enclosing statement.
                stmt = n
                while not isinstance(stmt, ast.stmt):
                    stmt = parents[stmt]
                bits = users.get(name, 0)
                for line in range(stmt.lineno, stmt.end_lineno + 1):
                    if line in lines:
                        bits |= coverage["sets"][lines[line]]
                users[name] = bits
    return {
        name: hex(users[name])
        for name in sorted(candidates - indirect)
        if users.get(name)
    }


if __name__ == "__main__":
    with gzip.open(sys.argv[2], "rb") as f:
        coverage = pickle.load(f)
    data = collect(Path(sys.argv[1]), coverage)
    Path(sys.argv[3]).write_text(json.dumps(data, separators=(",", ":")) + "\n")
