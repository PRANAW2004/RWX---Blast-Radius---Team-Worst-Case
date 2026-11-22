"""Blast Radius selector.

For each patch, decide which tests can be affected, using maps built from the
pinned sqlglot commit (see mapper/):

  * per-test line coverage, each test run in its own fresh process, so the
    lines a test depends on include import-time and cached code;
  * the files each test reads as data (fixtures, sources read as text);
  * the sqlglot classes each test instantiates.

A change is located by comparing the syntax trees of the old and new file, scope
by scope (module, class, function). Comment, blank-line and docstring edits do
not change a scope's tree, so they select only tests that read the file as text.

  changed function  -> tests that executed any line of its body
  changed class     -> tests that instantiated it or a subclass, plus users of its
                       methods; if it is never instantiated, every test that
                       imported its module
  changed module    -> every test that imported the module
  unparseable file  -> every test that imported the module
  data file         -> tests that read it
  pytest config     -> every test, if the pytest settings or the file's syntax change

The selected tests are packed onto 1..8 workers, longest first, and the worker
count with the lowest cost (busiest worker + 2 s a worker) is printed.
"""

import ast
import gzip
import json
import os
import pickle
import sys

DATA = os.environ.get("BLAST_DATA", "/data")
HERE = os.path.dirname(os.path.abspath(__file__))
SOURCE = os.environ.get("BLAST_SOURCE", os.path.join(HERE, "src"))
MAP = os.environ.get("BLAST_MAP", os.path.join(HERE, "map.pkl.gz"))
WORKER_MS = 2000
MAX_WORKERS = 8

DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
REFINE = os.environ.get("BLAST_REFINE", "1") == "1"
CONFIG_FILES = {"pyproject.toml", "setup.cfg", "tox.ini", "pytest.ini"}


def log(message):
    print(message, file=sys.stderr)


# ---------------------------------------------------------------- patches


def parse_patch(text):
    """Return a list of file changes: dict(old, new, hunks, binary)."""
    changes = []
    current = None
    hunk = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if hunk is not None and (hunk["old_left"] > 0 or hunk["new_left"] > 0):
            # Inside a hunk the counts decide, so "--- x" is a removed "-- x" line.
            tag = line[:1] if line else " "
            if line == "" and i == len(lines) - 1:
                break
            if tag == "\\":
                hunk["lines"].append(line)
            else:
                if tag not in " +-":
                    tag, line = " ", " " + line
                hunk["lines"].append(line if line else " ")
                if tag in " -":
                    hunk["old_left"] -= 1
                if tag in " +":
                    hunk["new_left"] -= 1
            i += 1
            continue
        if hunk is not None and line.startswith("\\"):
            hunk["lines"].append(line)
        elif line.startswith("diff --git "):
            current = {"old": None, "new": None, "hunks": [], "binary": False, "git": line}
            changes.append(current)
            hunk = None
            parts = line[len("diff --git ") :].split(" b/")
            if len(parts) == 2 and parts[0].startswith("a/"):
                current["old"] = parts[0][2:]
                current["new"] = parts[1]
        elif line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            if current is None or current["hunks"]:
                current = {"old": None, "new": None, "hunks": [], "binary": False, "git": None}
                changes.append(current)
            current["old"] = strip_prefix(line[4:])
            current["new"] = strip_prefix(lines[i + 1][4:])
            hunk = None
            i += 1
        elif line.startswith("rename from ") and current is not None:
            current["old"] = line[len("rename from ") :]
        elif line.startswith("rename to ") and current is not None:
            current["new"] = line[len("rename to ") :]
        elif line.startswith("new file mode") and current is not None:
            current["old"] = None
        elif line.startswith("deleted file mode") and current is not None:
            current["new"] = None
        elif (line.startswith("Binary files") or line.startswith("GIT binary patch")) and current is not None:
            current["binary"] = True
        elif line.startswith("@@") and current is not None:
            head = line.split("@@")[1].strip().split()
            old_start, old_len = range_of(head[0][1:])
            new_start, new_len = range_of(head[1][1:])
            hunk = {"old_start": old_start, "old_len": old_len, "new_start": new_start, "new_len": new_len,
                    "old_left": old_len, "new_left": new_len, "lines": []}
            current["hunks"].append(hunk)
        i += 1
    return changes


def strip_prefix(path):
    path = path.split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def range_of(text):
    if "," in text:
        start, length = text.split(",", 1)
        return int(start), int(length)
    return int(text), 1


def apply_hunks(old_lines, hunks):
    """Apply hunks to a list of lines (with line endings). Return new lines and
    the old line numbers removed and new line numbers added (1-based)."""
    new_lines = []
    removed, added = [], []
    old_index = 0  # 0-based index of the next old line to copy
    for hunk in hunks:
        start = hunk["old_start"] - 1 if hunk["old_len"] else hunk["old_start"]
        while old_index < start and old_index < len(old_lines):
            new_lines.append(old_lines[old_index])
            old_index += 1
        last_added = False
        for line in hunk["lines"]:
            tag, body = line[:1], line[1:]
            if tag == "\\":
                if last_added and new_lines:
                    new_lines[-1] = new_lines[-1].rstrip("\n")
                continue
            last_added = False
            if tag == " ":
                if old_index < len(old_lines):
                    new_lines.append(old_lines[old_index])
                old_index += 1
            elif tag == "-":
                removed.append(old_index + 1)
                old_index += 1
            elif tag == "+":
                new_lines.append(body + "\n")
                added.append(len(new_lines))
                last_added = True
    new_lines.extend(old_lines[old_index:])
    return new_lines, removed, added


# ---------------------------------------------------------------- scopes


class Scopes:
    """Every module, class and function scope of a parsed file, by qualname."""

    def __init__(self, tree):
        self.nodes = {"": tree}
        self.parent = {"": None}
        self.spans = []  # (start, end, qualname)
        self._walk(tree, "")

    def _walk(self, node, prefix):
        for child in iter_scope_children(node):
            name = f"{prefix}.{child.name}" if prefix else child.name
            unique, n = name, 1
            while unique in self.nodes:
                n += 1
                unique = f"{name}#{n}"
            self.nodes[unique] = child
            self.parent[unique] = prefix
            start = min([child.lineno] + [d.lineno for d in child.decorator_list])
            self.spans.append((start, child.end_lineno, unique))
            self._walk(child, unique)

    def innermost(self, line):
        best, best_size = "", None
        for start, end, name in self.spans:
            if start <= line <= end and (best_size is None or end - start < best_size):
                best, best_size = name, end - start
        return best


def iter_scope_children(node):
    """Defs nested in node's own code (not inside other defs)."""
    stack = list(ast.iter_child_nodes(node))
    found = []
    while stack:
        child = stack.pop()
        if isinstance(child, DEF_TYPES):
            found.append(child)
        else:
            stack.extend(ast.iter_child_nodes(child))
    found.sort(key=lambda n: (n.lineno, n.col_offset))
    return found


def own_shape(node):
    """A comparable form of a scope's own code: nested defs become placeholders,
    its docstring and all positions are left out."""

    def shape(value, top):
        if isinstance(value, ast.AST):
            if not top and isinstance(value, DEF_TYPES):
                return ("def", type(value).__name__, value.name)
            fields = []
            for name, field in ast.iter_fields(value):
                if name == "type_comment":
                    continue
                if top and name == "body" and isinstance(field, list):
                    field = strip_docstring(value, field)
                fields.append((name, shape(field, False)))
            return (type(value).__name__, tuple(fields))
        if isinstance(value, list):
            return tuple(shape(item, False) for item in value)
        return (type(value).__name__, repr(value))

    return shape(node, True)


def strip_docstring(node, body):
    if (
        isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def header_shape_def(node):
    """A function's signature and decorators."""
    return (own_shape_any(node.args), tuple(own_shape_any(d) for d in node.decorator_list),
            own_shape_any(node.returns) if node.returns else None, type(node).__name__)


SETTING = __import__("re").compile(r"^[A-Z][A-Z0-9_]*$")


def changed_settings(old, new):
    """Names of ALL_CAPS class settings whose values changed, if that is the class's
    only change (same header, same statements otherwise); else None."""
    if isinstance(old, ast.Module) and isinstance(new, ast.Module):
        pass
    elif not isinstance(new, ast.ClassDef) or not isinstance(old, ast.ClassDef):
        return None
    elif own_shape_any(old.bases) != own_shape_any(new.bases) or own_shape_any(old.keywords) != own_shape_any(
        new.keywords
    ) or own_shape_any(old.decorator_list) != own_shape_any(new.decorator_list):
        return None
    old_body, new_body = strip_docstring(old, old.body), strip_docstring(new, new.body)
    if len(old_body) != len(new_body):
        return None
    names = set()
    for o, n in zip(old_body, new_body):
        if stmt_shape(o) == stmt_shape(n):
            continue
        target_o, target_n = setting_target(o), setting_target(n)
        if target_o is None or target_o != target_n:
            return None
        names.add(target_o)
    return names or None


def setting_target(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        name = node.targets[0].id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
        name = node.target.id
    else:
        return None
    return name if SETTING.match(name) else None


COMPARE_OPS = {ast.Is: 0, ast.IsNot: 1, ast.Eq: 2, ast.NotEq: 3, ast.Lt: 4, ast.LtE: 5, ast.Gt: 6, ast.GtE: 7}


def single_operator_swap(old, new):
    """If old and new differ only in the operator of one single-operator is / is not /
    == / != comparison, return ("line:col", old operator id, new operator id), else None."""
    old_nodes, new_nodes = list(ast.walk(old)), list(ast.walk(new))
    if len(old_nodes) != len(new_nodes):
        return None
    found = None
    for o, n in zip(old_nodes, new_nodes):
        if isinstance(o, ast.cmpop) and isinstance(n, ast.cmpop):
            continue  # judged with their Compare
        if type(o) is not type(n):
            return None
        for name, value in ast.iter_fields(o):
            other = getattr(n, name, None)
            if isinstance(value, ast.AST) or name == "ops":
                continue
            if isinstance(value, list):
                if len(value) != len(other) or any(
                    not isinstance(v, ast.AST) and (type(v) is not type(w) or v != w) for v, w in zip(value, other)
                ):
                    return None
            elif type(value) is not type(other) or value != other:
                return None
        if isinstance(o, ast.Compare):
            old_ops, new_ops = [type(x) for x in o.ops], [type(x) for x in n.ops]
            if old_ops != new_ops:
                if found is not None or len(old_ops) != 1 or len(new_ops) != 1:
                    return None
                if old_ops[0] not in COMPARE_OPS or new_ops[0] not in COMPARE_OPS:
                    return None
                found = (f"{o.lineno}:{o.col_offset}", COMPARE_OPS[old_ops[0]], COMPARE_OPS[new_ops[0]])
    return found


def lambda_only_sites(old, new):
    """If every difference between old and new (a scope's own code: nested defs are
    compared by name only) lies inside lambda bodies, return the "line:col" sites of the
    innermost such lambdas in old; else None."""
    sites = set()

    def walk(o, n, lam, top):
        if type(o) is not type(n):
            return record(lam)
        if isinstance(o, ast.AST):
            if not top and isinstance(o, DEF_TYPES):
                return True if o.name == n.name else record(lam)
            for name, value in ast.iter_fields(o):
                if name == "type_comment":
                    continue
                inner = o if isinstance(o, ast.Lambda) and name == "body" else lam
                if not walk(value, getattr(n, name, None), inner, False):
                    return False
            return True
        if isinstance(o, list):
            if len(o) != len(n):
                return record(lam)
            return all(walk(x, y, lam, False) for x, y in zip(o, n))
        return True if (type(o) is type(n) and o == n) else record(lam)

    def record(lam):
        if lam is None:
            return False
        sites.add(f"{lam.lineno}:{lam.col_offset}")
        return True

    return sites if walk(old, new, None, True) and sites else None


def setting_targets(statement):
    """ALL_CAPS names a class-body statement assigns, if it is a plain assignment."""
    if isinstance(statement, ast.Assign) and all(isinstance(t, ast.Name) for t in statement.targets):
        names = [t.id for t in statement.targets]
    elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name) and statement.value is not None:
        names = [statement.target.id]
    else:
        return None
    return names if all(SETTING.match(n) for n in names) else None


def changed_defaults(old, new):
    """Positions (positional defaults first, then keyword-only ones, as run_probes.py
    numbers them) of parameters whose default value is the only change; else None."""
    if not isinstance(new, type(old)) or own_shape(old) == own_shape(new):
        return None
    if [own_shape_any(d) for d in old.decorator_list] != [own_shape_any(d) for d in new.decorator_list]:
        return None
    if (own_shape_any(old.returns) if old.returns else None) != (own_shape_any(new.returns) if new.returns else None):
        return None
    a, b = old.args, new.args
    for field in ("posonlyargs", "args", "kwonlyargs", "vararg", "kwarg"):
        if own_shape_any(getattr(a, field)) != own_shape_any(getattr(b, field)):
            return None
    if len(a.defaults) != len(b.defaults) or [d is None for d in a.kw_defaults] != [d is None for d in b.kw_defaults]:
        return None
    if [stmt_shape(x) for x in strip_docstring(old, old.body)] != [stmt_shape(x) for x in strip_docstring(new, new.body)]:
        return None
    changed = []
    for i, (x, y) in enumerate(zip(a.defaults, b.defaults)):
        if own_shape_any(x) != own_shape_any(y):
            changed.append(i)
    base = len(a.defaults)
    keyword = [(x, y) for x, y in zip(a.kw_defaults, b.kw_defaults) if x is not None]
    for j, (x, y) in enumerate(keyword):
        if own_shape_any(x) != own_shape_any(y):
            changed.append(base + j)
    return changed or None


def changed_scopes(old_scopes, new_scopes, removed, added):
    candidates = set()
    for line in removed:
        candidates.add(("old", old_scopes.innermost(line)))
    for line in added:
        candidates.add(("new", new_scopes.innermost(line)))
    names = set()
    for side, name in candidates:
        names.add(name)
    changed = set()
    pending = list(names)
    seen = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        old = old_scopes.nodes.get(name)
        new = new_scopes.nodes.get(name)
        if old is not None and new is not None:
            if type(old) is not type(new) or own_shape(old) != own_shape(new):
                changed.add(name)
        elif old is not None:  # removed scope: it and its parent changed
            changed.add(name)
            pending.append(old_scopes.parent[name])
        else:  # added scope: its parent changed
            parent = new_scopes.parent.get(name)
            if parent is not None:
                pending.append(parent)
    return changed



COMPOUND = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar)
BLOCK_FIELDS = ("body", "orelse", "finalbody")


def statement_ranges(old_fn, new_fn):
    """Old-file line ranges whose execution covers every effect of the change from
    old_fn to new_fn, or None when the change cannot be localized safely."""
    if function_traits(old_fn) != function_traits(new_fn):
        return None
    ranges = []
    _FUNCTION.append(old_fn)
    try:
        if not align_blocks(old_fn, strip_docstring(old_fn, old_fn.body), strip_docstring(new_fn, new_fn.body), ranges):
            return None
    finally:
        _FUNCTION.pop()
    return ranges


def function_traits(fn):
    """Compile-time facts about a function that a local edit can change globally."""
    stored, flags = set(), set()
    stack = list(strip_docstring(fn, fn.body))
    while stack:
        node = stack.pop()
        if isinstance(node, DEF_TYPES):
            stored.add(node.name)
            continue  # nested scope
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
            stored.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            flags.add((type(node).__name__, tuple(node.names)))
        elif isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            flags.add(type(node).__name__)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            stored.update((a.asname or a.name).split(".")[0] for a in node.names)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            stored.add(node.name)
        elif isinstance(node, ast.alias):
            pass
        stack.extend(ast.iter_child_nodes(node))
    return stored, flags


def stmt_shape(node):
    if isinstance(node, DEF_TYPES):
        return ("def", type(node).__name__, node.name)
    return own_shape_any(node)


def own_shape_any(node):
    def shape(value):
        if isinstance(value, ast.AST):
            if isinstance(value, DEF_TYPES):
                return ("def", type(value).__name__, value.name)
            return (type(value).__name__, tuple((n, shape(f)) for n, f in ast.iter_fields(value) if n != "type_comment"))
        if isinstance(value, list):
            return tuple(shape(v) for v in value)
        return (type(value).__name__, repr(value))

    return shape(node)


def header_shape(node):
    fields = []
    for name, value in ast.iter_fields(node):
        if name in BLOCK_FIELDS or name in ("handlers", "type_comment"):
            continue
        fields.append((name, own_shape_any(value) if isinstance(value, ast.AST) else
                       tuple(own_shape_any(v) for v in value) if isinstance(value, list) else repr(value)))
    return (type(node).__name__, tuple(fields))


def header_range(node):
    first_body = node.body[0].lineno if node.body else node.lineno + 1
    return (node.lineno, max(node.lineno, first_body - 1))


def statement_range(node):
    return (node.lineno, node.end_lineno)


def align_blocks(parent, old_body, new_body, ranges):
    """Align two statement lists; record ranges whose users cover the change."""
    import difflib

    old_shapes = [stmt_shape(s) for s in old_body]
    new_shapes = [stmt_shape(s) for s in new_body]
    matcher = difflib.SequenceMatcher(None, old_shapes, new_shapes, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" and i2 - i1 == j2 - j1:
            for o, n in zip(old_body[i1:i2], new_body[j1:j2]):
                if not align_statement(o, n, ranges):
                    return False
            continue
        if any(isinstance(s, DEF_TYPES) for s in old_body[i1:i2] + new_body[j1:j2]):
            return False
        # Statements removed or inserted: tests that reach this point of the block.
        for o in old_body[i1:i2]:
            ranges.append(statement_range(o))
        # Reaching the edit point means running the statement before it, or (at the start of
        # a block) the first statement after it.
        if i1 > 0:
            ranges.append(statement_range(old_body[i1 - 1]))
        if i2 < len(old_body):
            ranges.append(statement_range(old_body[i2]))
        if (i1 == 0 or i2 == len(old_body)) and isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return False  # entry/exit of the function: use the whole function
    return True


def align_statement(old, new, ranges):
    if isinstance(old, DEF_TYPES) or isinstance(new, DEF_TYPES):
        return False
    sites = lambda_only_sites(old, new)
    if sites:
        # Only lambda bodies changed: only tests that call those lambdas.
        ranges.append(("lambda", sites))
        return True
    uses = constant_assignment_uses(old, new)
    if uses is not None:
        # The new value matters only to tests that run the assignment AND read the name.
        ranges.append(("&", statement_range(old), uses))
        return True
    if type(old) is not type(new) or not isinstance(old, COMPOUND):
        ranges.append(statement_range(old))
        return True
    if header_shape(old) != header_shape(new):
        ranges.append(header_range(old))
    for field in BLOCK_FIELDS:
        old_block, new_block = getattr(old, field, None) or [], getattr(new, field, None) or []
        if stmt_shape_list(old_block) != stmt_shape_list(new_block):
            if not old_block or not new_block:
                ranges.append(statement_range(old))
                continue
            if not align_blocks(old if field == "body" else None, old_block, new_block, ranges):
                return False
    old_handlers, new_handlers = getattr(old, "handlers", []), getattr(new, "handlers", [])
    if [own_shape_any(h) for h in old_handlers] != [own_shape_any(h) for h in new_handlers]:
        ranges.append(statement_range(old))  # exception matching: the whole try
    return True


_FUNCTION = []  # the old function being aligned, for def-use lookups
DYNAMIC_SCOPE = {"locals", "vars", "eval", "exec", "globals"}


def is_constant(node):
    """A literal whose evaluation cannot raise or have side effects."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd, ast.Not)):
        return isinstance(node.operand, ast.Constant) and isinstance(node.operand.value, (int, float, bool))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(is_constant(e) for e in node.elts)
    return False


def assigned_names(target):
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)) and all(isinstance(e, ast.Name) for e in target.elts):
        return [e.id for e in target.elts] if not isinstance(target, ast.List) or True else None
    return None


def constant_assignment_uses(old, new):
    """If old and new assign different constants to the same local names, return the
    ranges of the statements that read those names (the only places the value can
    matter); else None."""
    if not _FUNCTION or type(old) is not type(new):
        return None
    if isinstance(old, ast.Assign):
        if len(old.targets) != 1 or len(new.targets) != 1:
            return None
        old_target, new_target = old.targets[0], new.targets[0]
    elif isinstance(old, ast.AnnAssign) and old.value is not None and new.value is not None and old.simple:
        old_target, new_target = old.target, new.target
        if own_shape_any(old.annotation) != own_shape_any(new.annotation):
            return None
    else:
        return None
    names = assigned_names(old_target)
    if not names or own_shape_any(old_target) != own_shape_any(new_target):
        return None
    if not (is_constant(old.value) and is_constant(new.value)):
        return None
    if isinstance(old_target, ast.Tuple) and not (
        isinstance(old.value, ast.Tuple) and isinstance(new.value, ast.Tuple)
        and len(old.value.elts) == len(new.value.elts) == len(names)
    ):
        return None  # unpacking a constant could raise differently
    function = _FUNCTION[-1]
    wanted = set(names)
    for node in ast.walk(function):
        if isinstance(node, (ast.Global, ast.Nonlocal)) and wanted & set(node.names):
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in DYNAMIC_SCOPE:
            return None
    spans = own_statement_spans(function)
    uses = []
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and node.id in wanted and isinstance(node.ctx, (ast.Load, ast.Del)):
            uses.append(innermost_span(spans, node.lineno))
    return uses


def own_statement_spans(function):
    spans = []
    for node in ast.walk(function):
        if isinstance(node, ast.stmt) and node is not function:
            body = getattr(node, "body", None)
            if isinstance(body, list) and body and isinstance(body[0], ast.AST):
                spans.append((node.lineno, max(node.lineno, body[0].lineno - 1)))
            else:
                spans.append((node.lineno, node.end_lineno))
    return spans


def innermost_span(spans, line):
    best = None
    for start, end in spans:
        if start <= line <= end and (best is None or end - start < best[1] - best[0]):
            best = (start, end)
    return best or (line, line)


def stmt_shape_list(block):
    return [stmt_shape(s) for s in block]


# ---------------------------------------------------------------- selection


class Selector:
    def __init__(self, data):
        self.count = data["count"]
        self.ms = data["ms"]
        self.sets = data["sets"]
        self.lines = data["lines"]
        self.reads = data["reads"]
        self.classes = data["classes"]
        self.test_files = data["test_files"]
        self.refs = data.get("refs", {})
        self.probes = data.get("probes", {})
        self.probe_unreliable = data.get("probe_unreliable", 0)
        self.probed_files = set(data.get("probed_files", ()))
        self.probe_masks = data.get("probe_masks", {})
        self.probe_used = data.get("probe_used", {})
        self.attr_reads = data.get("attr_reads")
        self.lambda_calls = data.get("lambda_calls")
        self.runtime_reads = data.get("runtime_reads")
        self.all = (1 << self.count) - 1

    def line_users(self, path, start, end):
        per_line = self.lines.get(path)
        if not per_line:
            return 0
        bits = 0
        sets = self.sets
        for line in range(start, end + 1):
            index = per_line.get(line)
            if index is not None:
                bits |= sets[index]
        return bits

    def importers(self, path):
        """Every test whose process executed any line of the file."""
        per_line = self.lines.get(path)
        if not per_line:
            return 0
        bits = 0
        for index in set(per_line.values()):
            bits |= self.sets[index]
        return bits

    def readers(self, path):
        index = self.reads.get(path)
        return self.sets[index] if index is not None else 0

    def tests_in(self, path):
        index = self.test_files.get(path)
        return self.sets[index] if index is not None else 0

    def select(self, patch_text):
        # Setting-level changes are tracked apart, so the import-state comparison can
        # replace their contribution; anything else that might break it marks it unusable.
        self.setting_bits = 0
        self.setting_keys = []  # (owner, names) of setting-level changes
        self.setting_ok = True
        self.patched = {}
        bits = 0
        for change in parse_patch(patch_text):
            try:
                bits |= self.select_change(change)
            except Exception as error:  # never crash: fall back to everything the file touches
                log(f"fallback on {change.get('old') or change.get('new')}: {error!r}")
                self.setting_ok = False
                path = change.get("old") or change.get("new") or ""
                bits |= self.importers(path) | self.readers(path) | self.tests_in(path)
                if not bits:
                    bits = self.all
        return bits

    def select_change(self, change):
        old_path, new_path = change["old"], change["new"]
        bits = 0
        for path in {old_path, new_path} - {None}:
            bits |= self.readers(path)
        path = old_path or new_path
        name = os.path.basename(path)

        if name in CONFIG_FILES or name == "conftest.py":
            return bits | self.config_change(change)

        if not (old_path == new_path and path.startswith("sqlglot/") and path.endswith(".py")) or change["binary"]:
            self.setting_ok = False
        if not path.endswith(".py"):
            if old_path is None and new_path is not None:
                bits |= self.directory_readers(new_path)
            return bits

        if old_path is None:  # a new Python file: nothing at the pinned commit imports it
            return bits
        if new_path is None or change["binary"]:
            return bits | self.importers(old_path) | self.tests_in(old_path)

        old_source = read_source(old_path)
        if old_source is None:
            return bits | self.importers(old_path) | self.tests_in(old_path)
        old_lines = old_source.decode("utf-8", "surrogateescape").splitlines(keepends=True)
        new_lines, removed, added = apply_hunks(old_lines, change["hunks"])
        new_source = "".join(new_lines).encode("utf-8", "surrogateescape")
        try:
            old_tree = ast.parse(old_source)
            new_tree = ast.parse(new_source)
        except (SyntaxError, ValueError):
            self.setting_ok = False
            return bits | self.importers(old_path) | self.tests_in(old_path)
        self.patched[old_path] = new_source

        old_scopes, new_scopes = Scopes(old_tree), Scopes(new_tree)
        for scope in changed_scopes(old_scopes, new_scopes, removed, added):
            bits |= self.scope_users(old_path, old_scopes, scope, new_scopes)
        if new_path != old_path:  # renamed module: everything importing the old name
            bits |= self.importers(old_path)
        return bits

    def scope_users(self, path, scopes, name, new_scopes=None):
        node = scopes.nodes[name]
        if name == "" or isinstance(node, ast.Module):
            new_node = new_scopes.nodes.get("") if new_scopes is not None else None
            sites = lambda_only_sites(node, new_node) if REFINE and new_node is not None else None
            if sites and path in self.probed_files and self.lambda_calls is not None:
                return self.lambda_users(path, sites)
            settings = changed_settings(node, new_node) if REFINE and new_node is not None else None
            if settings:
                # Only ALL_CAPS constants changed value: importers that run code reading them.
                result = self.setting_readers(settings, module_name(path), self.importers(path))
                self.setting_bits |= result
                self.setting_keys.append((module_name(path), settings))
                return result
            return self.importers(path)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            new_node = new_scopes.nodes.get(name) if new_scopes is not None else None
            start_line = node.body[0].lineno if node.body else node.lineno
            if REFINE and isinstance(new_node, type(node)) and header_shape_def(node) == header_shape_def(new_node):
                ranges = statement_ranges(node, new_node)
                if ranges is not None:
                    bits = 0
                    for item in ranges:
                        if item[0] == "lambda":
                            if self.lambda_calls is None or path not in self.probed_files:
                                return self.line_users(path, start_line, node.end_lineno)
                            bits |= self.lambda_users(path, item[1])
                        elif item[0] == "&":
                            uses = 0
                            for start, end in item[2]:
                                uses |= self.line_users(path, start, end)
                            bits |= self.line_users(path, *item[1]) & uses
                        else:
                            bits |= self.line_users(path, *item)
                    swap = single_operator_swap(node, new_node) if path in self.probed_files else None
                    if swap is not None and {swap[1], swap[2]} in ({0, 2}, {1, 3}):
                        # Only `is` <-> `==` (or `is not` <-> `!=`) changed at one site. For a test
                        # whose operand types there were all identity-safe, `==` is `is`: its
                        # execution is unchanged. Keep only the other tests.
                        index = self.probes.get(f"{path}:{swap[0]}")
                        unsafe = self.sets[index] if index is not None else 0
                        bits &= unsafe | self.probe_unreliable
                    elif swap is not None and swap[1] >= 4 and swap[2] >= 2:
                        # An ordering comparison's operator changed: keep the tests in which the new
                        # operator would ever have given a different result there.
                        index = self.probe_masks.get(f"{path}:{swap[0]}", {}).get(swap[2])
                        differs = self.sets[index] if index is not None else 0
                        bits &= differs | self.probe_unreliable
                    return bits
            start = node.body[0].lineno if node.body else node.lineno
            bits = self.line_users(path, start, node.end_lineno)
            changed = changed_defaults(node, new_node) if REFINE and path in self.probed_files else None
            if changed:
                # Only default values changed: a call that passed those arguments behaves the same.
                used = self.probe_used.get(f"{path}:{node.lineno}:{node.col_offset}", {})
                users = self.probe_unreliable
                for position in list(changed) + [-1]:
                    index = used.get(position)
                    if index is not None:
                        users |= self.sets[index]
                return bits & users
            return bits
        # A class: its instances' tests, and the users of its methods.
        module = module_name(path)
        key = f"{module}:{name}" if module else None
        bits = 0
        for child in iter_scope_children(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.body:
                bits |= self.line_users(path, child.body[0].lineno, child.end_lineno)
        new_node = new_scopes.nodes.get(name) if new_scopes is not None else None
        sites = lambda_only_sites(node, new_node) if REFINE and new_node is not None else None
        if sites and path in self.probed_files and self.lambda_calls is not None:
            return self.lambda_users(path, sites)  # only lambda bodies changed: their callers
        index = self.classes.get(key) if key else None
        if index is None or "#" in name:
            return bits | self.importers(path)
        users = self.sets[index]
        settings = changed_settings(node, new_node) if REFINE and new_node is not None else None
        if settings:
            # Only setting values changed: tests that read them from this class (measured), or
            # that load the class's module and run a read the probes cannot attribute. Not just
            # the class's users: a metaclass may copy a setting into other classes at import.
            result = self.setting_readers(settings, key, self.importers(path), node, path)
            self.setting_bits |= result
            self.setting_keys.append((key, settings))
            return result
        return bits | users

    def untracked_readers(self, name, owner):
        """Run-time reads of NAME the probes cannot attribute: string literals anywhere, reads
        in files they do not instrument (tests), bare names inside the owning module (for a
        module constant), and reads inside default values (via the default-use probes)."""
        module = owner.split(":")[0]
        module_path = module.replace(".", "/") + ".py"
        if module_path not in self.lines:
            module_path = module.replace(".", "/") + "/__init__.py"
        importers = self.importers(module_path)
        bits = 0
        for ref in self.refs.get(name, ()):
            if len(ref) == 4 and ref[1] == "default":
                used = self.probe_used.get(ref[2])
                if used is None or ref[0] not in self.probed_files:
                    bits |= self.importers(ref[0])
                    continue
                for position in (ref[3], -1):
                    index = used.get(position)
                    if index is not None:
                        bits |= self.sets[index]
                continue
            path, start, end = ref[:3]
            kind = ref[3] if len(ref) == 4 else "str"
            if kind == "attr" and path in self.probed_files:
                continue  # attributed by the probes
            if kind == "name" and (":" in owner or path != module_path):
                continue  # a bare name cannot read a class attribute or another module's global
            bits |= self.line_users(path, start, end)
        return bits & importers

    def lambda_users(self, path, sites):
        bits = self.probe_unreliable
        for site in sites:
            index = self.lambda_calls.get(f"{path}:{site}")
            if index is not None:
                bits |= self.sets[index]
        return bits

    def setting_readers(self, names, owner, users, scope=None, path=None, seen=None):
        """Tests a change of these ALL_CAPS names, defined by owner (a class key or a module
        name), can affect. `x.NAME` reads in instrumented files are attributed to the class or
        module NAME was read from; every other read counts for all of `users`."""
        if self.attr_reads is None:
            return users & self.readers_of(names)  # without probes: the older, coarser rule
        seen = set(names) if seen is None else seen
        bits = 0
        for name in names:
            for key in (f"{owner}|{name}", f"<unknown>|{name}"):
                index = self.attr_reads.get(key)
                if index is not None:
                    bits |= self.sets[index]
        # A bare-name read in this class's own body that only computes another setting
        # (DERIVED = f(NAME)) passes the change on to DERIVED: follow it instead.
        derived, skip = set(), []
        if isinstance(scope, ast.ClassDef) and path is not None:
            for statement in scope.body:
                targets = setting_targets(statement)
                if not targets:
                    continue
                reads = {n.id for n in ast.walk(statement.value) if isinstance(n, ast.Name) and n.id in names}
                if reads:
                    skip.append((statement.lineno, statement.end_lineno))
                    derived.update(t for t in targets if t not in seen)
        if derived:
            seen |= derived
            bits |= self.setting_readers(derived, owner, users, scope, path, seen)
        # A bare name never reads another class's attribute (in a class body it means that
        # class's own namespace or a module global), so for a class setting only bare reads in
        # the class's own body count.
        own = (path, scope.lineno, scope.end_lineno) if isinstance(scope, ast.ClassDef) and path else None
        return bits | (users & self.readers_of(names, attributed=True, skip=(path, skip), own_body=own))

    def readers_of(self, names, attributed=False, skip=(None, ()), own_body=None):
        """Tests that execute a statement reading any of these names."""
        readers = 0
        for name in names:
            for ref in self.refs.get(name, ()):
                if len(ref) == 4 and ref[1] == "default":
                    # Read inside a default value: only calls that used that default.
                    used = self.probe_used.get(ref[2])
                    if used is None or ref[0] not in self.probed_files:
                        readers |= self.importers(ref[0])
                        continue
                    readers |= self.probe_unreliable
                    for position in (ref[3], -1):
                        index = used.get(position)
                        if index is not None:
                            readers |= self.sets[index]
                else:
                    ref_path, start, end = ref[:3]
                    if attributed and len(ref) == 4 and ref[3] == "attr" and ref_path in self.probed_files:
                        continue  # measured per class / module by the probes
                    if ref_path == skip[0] and any(a <= start and end <= b for a, b in skip[1]):
                        continue  # followed into the setting it computes
                    if own_body is not None and len(ref) == 4 and ref[3] == "name" and not (
                        ref_path == own_body[0] and own_body[1] <= start and end <= own_body[2]
                    ):
                        continue  # a bare name outside the class's body cannot read its attribute
                    readers |= self.line_users(ref_path, start, end)
        return readers

    def directory_readers(self, path):
        directory = os.path.dirname(path) + "/"
        bits = 0
        for other, index in self.reads.items():
            if other.startswith(directory) and "/" not in other[len(directory) :]:
                bits |= self.sets[index]
        return bits

    def config_change(self, change):
        old_path, new_path = change["old"], change["new"]
        name = os.path.basename(old_path or new_path)
        if name == "conftest.py" or old_path is None or new_path is None:
            return self.all
        old_source = read_source(old_path)
        if old_source is None:
            return self.all
        old_text = old_source.decode("utf-8", "surrogateescape")
        new_text = "".join(apply_hunks(old_text.splitlines(keepends=True), change["hunks"])[0])
        try:
            if pytest_settings(name, old_text) == pytest_settings(name, new_text):
                return 0
        except Exception:
            pass
        return self.all


def pytest_settings(name, text):
    """The parts of a config file pytest reads. Raises if the file does not parse."""
    if name == "pyproject.toml":
        import tomllib

        tool = tomllib.loads(text).get("tool", {})
        return {k: v for k, v in tool.items() if "pytest" in k}
    import configparser

    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(text)
    return {s: dict(parser[s]) for s in parser.sections() if "pytest" in s}


def module_name(path):
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def read_source(path):
    try:
        with open(os.path.join(SOURCE, path), "rb") as f:
            return f.read()
    except OSError:
        return None


# ---------------------------------------------------------------- import-time state

SNAPSHOT = os.environ.get("BLAST_SNAPSHOT", os.path.join(HERE, "snapshot.pkl"))
SNAPSHOT_BUDGET = float(os.environ.get("BLAST_SNAPSHOT_BUDGET", "40"))  # CPU seconds for the batch
SNAPSHOT_COST = 1.3  # CPU seconds for one patched snapshot
NAMEISH = __import__("re").compile(r"^_*[A-Z][A-Z0-9_]*$")


class ImportState:
    """For setting-level changes, load the patched sqlglot in a subprocess and compare its
    import-time state with the pinned one (snapshot.py). Effects at import, such as values
    a metaclass derives, then show up as changed attributes, and only tests that read a
    changed attribute while running can be affected. Any doubt keeps the selection."""

    def __init__(self):
        with open(SNAPSHOT, "rb") as f:
            self.reference = pickle.load(f)
        self.volatile = set(self.reference.get("<volatile>", ()))
        self.spent = 0.0

    def refine(self, selector, commit, bits):
        if not selector.setting_bits or not selector.setting_ok or not selector.patched:
            return bits
        others = bits & ~selector.setting_bits
        # Best case: the change has no effect at import, so only run-time readers of the changed
        # settings remain. Compare only if that would save more than the comparison costs.
        best = others
        for owner, names in selector.setting_keys:
            for name in names:
                for probe in (f"{owner}|{name}", f"<unknown>|{name}"):
                    index = selector.runtime_reads.get(probe)
                    if index is not None:
                        best |= selector.sets[index]
                best |= selector.untracked_readers(name, owner)
        gain = commit_cost(bits_to_numbers(bits), selector.ms) - commit_cost(bits_to_numbers(best), selector.ms)
        if gain <= SNAPSHOT_COST + 0.5 or self.spent + SNAPSHOT_COST > SNAPSHOT_BUDGET:
            return bits
        changed, used = self.changed_keys(selector.patched)
        self.spent += used
        if changed is None:
            return bits
        readers = 0
        for key in changed:
            owner, name = key.rsplit("|", 1)
            if not NAMEISH.match(name):
                log(f"{commit}: import state: {key} changed, its reads are not tracked; keeping selection")
                return bits
            for probe in (key, f"<unknown>|{name}"):
                index = selector.runtime_reads.get(probe)
                if index is not None:
                    readers |= selector.sets[index]
            readers |= selector.untracked_readers(name, owner)
        log(f"{commit}: import state changed {sorted(changed)[:3]}: {bin(readers).count('1')} readers ({used:.1f} s cpu)")
        return others | readers | (selector.probe_unreliable & bits)

    def changed_keys(self, patched):
        import resource
        import shutil
        import subprocess
        import tempfile

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        root = tempfile.mkdtemp(prefix="state-", dir=os.environ.get("TMPDIR", "/tmp"))
        changed = None
        try:
            shutil.copytree(os.path.join(SOURCE, "sqlglot"), os.path.join(root, "sqlglot"), copy_function=os.symlink)
            for path, text in patched.items():
                target = os.path.join(root, path)
                os.unlink(target)
                with open(target, "wb") as f:
                    f.write(text)
            keys = os.path.join(root, "unordered.pkl")
            with open(keys, "wb") as f:
                pickle.dump(self.reference.get("<unordered>", []), f)
            out = os.path.join(root, "state.pkl")
            subprocess.run(
                [sys.executable, "-S", os.path.join(HERE, "snapshot.py"), root, out, keys],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60, check=True,
                env={**os.environ, "PYTHONHASHSEED": "3", "PYTHONDONTWRITEBYTECODE": "1"},
            )
            with open(out, "rb") as f:
                state = pickle.load(f)
            keys = set(self.reference) | set(state)
            diff = {k for k in keys if not k.startswith("<") and self.reference.get(k) != state.get(k)}
            failures = {k for k in keys if k.startswith("<import>|") and self.reference.get(k) != state.get(k)}
            if not (diff & self.volatile) and not failures:
                changed = diff
        except Exception as error:  # any doubt keeps the selection
            log(f"import state error: {error!r}")
        finally:
            shutil.rmtree(root, ignore_errors=True)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        return changed, (after.ru_utime + after.ru_stime) - (before.ru_utime + before.ru_stime)


def commit_cost(numbers, ms):
    workers = schedule(numbers, ms)
    return (max(sum(ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) / 1000 if workers else 0.0


# ---------------------------------------------------------------- scheduling


def schedule(numbers, ms):
    """Pick the worker count and LPT split with the lowest cost."""
    if not numbers:
        return []
    ordered = sorted(numbers, key=lambda n: (-ms[n], n))
    best, best_cost = None, None
    for count in range(1, MAX_WORKERS + 1):
        if count > len(ordered):
            break
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for n in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(n)
            loads[lightest] += ms[n]
        cost = max(loads) + WORKER_MS * count
        if best_cost is None or cost < best_cost:
            best, best_cost = workers, cost
    return best


def bits_to_numbers(bits):
    numbers = []
    index = 0
    while bits:
        chunk = bits & 0xFFFFFFFFFFFFFFFF
        while chunk:
            low = chunk & -chunk
            numbers.append(index + low.bit_length() - 1)
            chunk ^= low
        bits >>= 64
        index += 64
    return numbers


def main():
    opener = gzip.open if MAP.endswith(".gz") else open
    try:
        with opener(MAP, "rb") as f:
            selector = Selector(pickle.load(f))
    except OSError:
        # No map yet: run every test, split optimally. Never misses.
        log(f"no map at {MAP}: selecting every test")
        with open(os.path.join(DATA, "tests.json"), "rb") as f:
            tests = json.load(f)["tests"]
        count = len(tests)
        selector = Selector({"count": count, "ms": [int(t["seconds"] * 1000 + 0.5) for t in tests],
                             "sets": [], "lines": {}, "reads": {}, "classes": {}, "test_files": {}})
        selector.select = lambda text: selector.all
    commits = os.path.join(DATA, "commits")
    snapshots = ImportState() if getattr(selector, "runtime_reads", None) is not None and os.path.exists(SNAPSHOT) else None
    out = []
    total = 0
    for entry in sorted(os.listdir(commits)):
        if not entry.endswith(".patch"):
            continue
        commit = entry[: -len(".patch")]
        try:
            with open(os.path.join(commits, entry), "rb") as f:
                text = f.read().decode("utf-8", "surrogateescape")
            bits = selector.select(text)
        except Exception as error:
            log(f"{commit}: error {error!r}, running everything")
            bits = selector.all
            selector.setting_bits = 0
        bits &= selector.all
        if snapshots is not None:
            try:
                bits = snapshots.refine(selector, commit, bits)
            except Exception as error:  # any doubt keeps the selection
                log(f"{commit}: import state error {error!r}")
        numbers = bits_to_numbers(bits)
        workers = schedule(numbers, selector.ms)
        cost = (max(sum(selector.ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{commit}: {len(numbers)} tests on {len(workers)} workers, {cost / 1000:.1f} s")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    log(f"predicted simulated seconds without misses: {total / 1000:.1f}")


if __name__ == "__main__":
    main()
