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
    if not align_blocks(old_fn, strip_docstring(old_fn, old_fn.body), strip_docstring(new_fn, new_fn.body), ranges):
        return None
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
        if i1 > 0:
            ranges.append(statement_range(old_body[i1 - 1]))
        if i2 < len(old_body):
            ranges.append(statement_range(old_body[i2]))
        if (i1 == 0 or i2 == len(old_body)) and parent is not None:
            if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False  # entry/exit of the function: use the whole function
            ranges.append((parent.lineno, parent.end_lineno) if isinstance(parent, ast.ExceptHandler) else header_range(parent))
    return True


def align_statement(old, new, ranges):
    if isinstance(old, DEF_TYPES) or isinstance(new, DEF_TYPES):
        return False
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
            if field != "body":
                ranges.append(header_range(old))
    old_handlers, new_handlers = getattr(old, "handlers", []), getattr(new, "handlers", [])
    if [own_shape_any(h) for h in old_handlers] != [own_shape_any(h) for h in new_handlers]:
        ranges.append(statement_range(old))  # exception matching: the whole try
    return True


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
        bits = 0
        for change in parse_patch(patch_text):
            try:
                bits |= self.select_change(change)
            except Exception as error:  # never crash: fall back to everything the file touches
                log(f"fallback on {change.get('old') or change.get('new')}: {error!r}")
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
            return bits | self.importers(old_path) | self.tests_in(old_path)

        old_scopes, new_scopes = Scopes(old_tree), Scopes(new_tree)
        for scope in changed_scopes(old_scopes, new_scopes, removed, added):
            bits |= self.scope_users(old_path, old_scopes, scope, new_scopes)
        if new_path != old_path:  # renamed module: everything importing the old name
            bits |= self.importers(old_path)
        return bits

    def scope_users(self, path, scopes, name, new_scopes=None):
        node = scopes.nodes[name]
        if name == "" or isinstance(node, ast.Module):
            return self.importers(path)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            new_node = new_scopes.nodes.get(name) if new_scopes is not None else None
            if REFINE and isinstance(new_node, type(node)) and header_shape_def(node) == header_shape_def(new_node):
                ranges = statement_ranges(node, new_node)
                if ranges is not None:
                    bits = 0
                    for start, end in ranges:
                        bits |= self.line_users(path, start, end)
                    return bits
            start = node.body[0].lineno if node.body else node.lineno
            return self.line_users(path, start, node.end_lineno)
        # A class: its instances' tests, and the users of its methods.
        module = module_name(path)
        key = f"{module}:{name}" if module else None
        bits = 0
        for child in iter_scope_children(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.body:
                bits |= self.line_users(path, child.body[0].lineno, child.end_lineno)
        index = self.classes.get(key) if key else None
        if index is None or "#" in name:
            return bits | self.importers(path)
        return bits | self.sets[index]

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
        numbers = bits_to_numbers(bits & selector.all)
        workers = schedule(numbers, selector.ms)
        cost = (max(sum(selector.ms[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{commit}: {len(numbers)} tests on {len(workers)} workers, {cost / 1000:.1f} s")
        out.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
    sys.stdout.write("\n".join(out) + ("\n" if out else ""))
    log(f"predicted simulated seconds without misses: {total / 1000:.1f}")


if __name__ == "__main__":
    main()
