"""Find misses before the scorer does: make random mutants of the pinned sqlglot,
label each by running the whole test suite, and check the selector selects every
failing test.

Usage (in the map image or any env matching requirements.lock, from the repo root):
    python mapper/mutation_check.py <sqlglot_src> <tests.json> <n_mutants> [seed]

Writes mutants/<id>.patch and mutants/answers.json (same format as the kit's), so
`python score.py mutants-data mutants/answers.json out.jsonl` works too.
"""

import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys

src, tests_json, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
tests = json.load(open(tests_json))["tests"]
number_of = {t["id"]: t["number"] for t in tests}
by_file = {}
for t in tests:
    by_file.setdefault(t["file"], []).append(t["number"])

OPERATORS = [
    (r" and ", " or "), (r" or ", " and "), (r" == ", " != "), (r" != ", " == "),
    (r" < ", " <= "), (r" <= ", " < "), (r" > ", " >= "), (r" >= ", " > "),
    (r"\bTrue\b", "False"), (r"\bFalse\b", "True"), (r" is not ", " is "),
    (r" is ", " is not "), (r"\bnot ", ""), (r"\b(\d+)\b", None),
    (r"\breturn ", "return None and "), (r'"([A-Z_]{2,})"', None),
]


def candidates():
    files = []
    for base in ("sqlglot", "tests"):
        for dirpath, _, names in os.walk(os.path.join(src, base)):
            for name in names:
                rel = os.path.relpath(os.path.join(dirpath, name), src)
                if name.endswith(".py") or (name.endswith(".sql") and "fixtures" in rel):
                    files.append(rel)
    return sorted(files)


def mutate(text, path):
    lines = text.splitlines(keepends=True)
    for _ in range(200):
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if path.endswith(".sql"):
            if line.startswith("#") or len(line) < 8:
                continue
            j = rng.randrange(len(line) - 1)
            lines[i] = line[:j] + rng.choice(["x", " 1 ", "(", ""]) + line[j + 1 :]
            return "".join(lines)
        kind = rng.random()
        if kind < 0.08:  # comment only: should select (almost) nothing
            indent = line[: len(line) - len(line.lstrip())]
            lines.insert(i, f"{indent}# note\n")
            return "".join(lines)
        rng.shuffle(OPERATORS)
        for pattern, repl in OPERATORS:
            match = re.search(pattern, line)
            if not match:
                continue
            if repl is None and pattern.startswith(r"\b("):
                new = line[: match.start()] + str(int(match.group(1)) + 1) + line[match.end() :]
            elif repl is None:
                new = line[: match.start()] + f'"{match.group(1)}X"' + line[match.end() :]
            else:
                new = line[: match.start()] + repl + line[match.end() :]
            if new != line:
                lines[i] = new
                return "".join(lines)
    return None


def run_suite(root):
    env = {**os.environ, "SKIP_INTEGRATION": "0", "PYTHONPATH": root}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "-rfE", "-p", "no:cacheprovider",
         "-n", str(os.cpu_count()), "--timeout=300", "-o", "addopts="],
        cwd=root, env=env, capture_output=True, text=True,
    )
    failing = set()
    for line in result.stdout.splitlines():
        m = re.match(r"(FAILED|ERROR|SUBFAILED\S*)\s+(\S+)", line)
        if not m:
            continue
        nodeid = m.group(2).split(" - ")[0]
        if nodeid in number_of:
            failing.add(number_of[nodeid])
        elif nodeid.endswith(".py"):  # collection error: the whole file fails
            failing.update(by_file.get(nodeid, []))
        else:
            base = re.sub(r"\[.*$", "", nodeid)
            if base in number_of:
                failing.add(number_of[base])
    return sorted(failing)


os.makedirs("mutants-data/commits", exist_ok=True)
shutil.copy(tests_json, "mutants-data/tests.json")
answers = {}
files = candidates()
work = "/tmp/mutant-src"
for k in range(count):
    while True:
        path = rng.choice(files)
        original = open(os.path.join(src, path), encoding="utf-8").read()
        mutated = mutate(original, path)
        if mutated:
            break
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(src, work, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    with open(os.path.join(work, path), "w", encoding="utf-8") as f:
        f.write(mutated)
    patch = "".join(difflib.unified_diff(
        original.splitlines(keepends=True), mutated.splitlines(keepends=True),
        fromfile=f"a/{path}", tofile=f"b/{path}"))
    commit = f"m{k:03d}"
    with open(f"mutants-data/commits/{commit}.patch", "w") as f:
        f.write(f"diff --git a/{path} b/{path}\n" + patch)
    failing = run_suite(work)
    answers[commit] = {"failing": failing, "kind": "mutant"}
    json.dump({"commits": answers}, open("mutants-data/answers.json", "w"), indent=1)

    env = {**os.environ, "BLAST_DATA": "mutants-data", "BLAST_SOURCE": src}
    out = subprocess.run([sys.executable, "-S", "selector.py"], env=env, capture_output=True, text=True).stdout
    selected = {}
    for line in out.splitlines():
        o = json.loads(line)
        selected[o["commit"]] = {n for w in o["workers"] for n in w}
    missed = [n for n in failing if n not in selected.get(commit, set())]
    status = f"MISS {missed}" if missed else "ok"
    print(f"{commit} {path}: {len(failing)} failing, {len(selected.get(commit, ()))} selected - {status}", flush=True)
