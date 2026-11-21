"""Refine candidates using per-test events recorded on the pinned SQLGlot checkout."""

import ast
import gzip
import json
from pathlib import Path

ORDERS = {
    ast.Lt: {-1},
    ast.LtE: {-1, 0},
    ast.Gt: {1},
    ast.GtE: {0, 1},
    ast.Eq: {0},
    ast.NotEq: {-1, 1},
}


def same(left, right):
    return ast.dump(left) == ast.dump(right)


def changed_path(old, new):
    """Follow the only differing AST field, stopping at a branch or a scalar."""
    chain = [(old, new)]
    while type(old) is type(new):
        children = []
        for name, a in ast.iter_fields(old):
            b = getattr(new, name)
            if isinstance(a, ast.AST) and isinstance(b, ast.AST):
                if not same(a, b):
                    children.append((a, b))
            elif isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
                for x, y in zip(a, b):
                    if isinstance(x, ast.AST) and isinstance(y, ast.AST):
                        if not same(x, y):
                            children.append((x, y))
                    elif x != y:
                        return chain
            elif a != b:
                return chain
        if len(children) != 1:
            break
        old, new = children[0]
        chain.append((old, new))
    return chain


class Profiles:
    def __init__(self):
        with gzip.open(Path(__file__).with_name("profile.json.gz"), "rt") as f:
            data = json.load(f)
        self.sites = data["sites"]
        self.complete = int(data["complete"], 16)

    def mask(self, path, old, new):
        if type(old) is not type(new):
            return -1
        chain = changed_path(old, new)
        for i, (a, b) in enumerate(chain):
            if not hasattr(a, "lineno"):
                continue
            kind = (
                "compare"
                if isinstance(a, ast.Compare)
                else "dict"
                if isinstance(a, ast.Dict)
                else "called"
                if isinstance(a, (ast.Lambda, ast.Call))
                else None
            )
            site = self.sites.get(f"{kind}:{path}:{a.lineno}:{a.col_offset}")
            if site is None:
                continue
            keys = None
            if (
                isinstance(a, ast.Compare)
                and isinstance(b, ast.Compare)
                and len(a.ops) == len(b.ops) == 1
            ):
                left, right = type(a.ops[0]), type(b.ops[0])
                if (
                    left != right
                    and left in ORDERS
                    and right in ORDERS
                    and same(a.left, b.left)
                    and same(a.comparators[0], b.comparators[0])
                ):
                    keys = (ORDERS[left] ^ ORDERS[right]) | {2}
            elif (
                isinstance(a, ast.Dict)
                and isinstance(b, ast.Dict)
                and len(chain) > i + 1
            ):
                child = chain[i + 1][0]
                for key, value in zip(a.keys, a.values):
                    if (
                        child is value
                        and isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                    ):
                        try:
                            ast.literal_eval(b.values[a.values.index(value)])
                        except (ValueError, TypeError):
                            return -1
                        keys = (key.value, None)
                        break
            elif (
                isinstance(a, ast.Lambda)
                and isinstance(b, ast.Lambda)
                and same(a.args, b.args)
            ):
                keys = ("called",)
            elif (
                isinstance(a, ast.Call)
                and isinstance(b, ast.Call)
                and same(a.func, b.func)
            ):
                # Only constant argument substitutions in a factory call may use
                # the returned callable's invocation profile.
                x, y = chain[-1]
                if isinstance(x, ast.Constant) and isinstance(y, ast.Constant):
                    keys = ("called",)
            if keys is not None:
                retained = ~self.complete
                for k in keys:
                    retained |= int(site.get(json.dumps(k), "0"), 16)
                return retained
        return -1
