"""Build map.pkl.gz from the per-test isolated runs (run_isolated.py output).

The map is built from the pinned sqlglot commit only: for every test, the lines
it executes (imports included, since each test ran in a fresh process), the
files it reads as data, and the sqlglot classes it instantiates.

Usage: python build_map.py <sqlglot_root> <tests.json> <iso_dir> <out.pkl.gz>
"""

import ast
import glob
import gzip
import json
import os
import pickle
import sys

import coverage

root, tests_json, iso_dir, out_path = sys.argv[1:5]
probe_dir = sys.argv[5] if len(sys.argv) > 5 else None
root = os.path.realpath(root)
tests = json.load(open(tests_json))["tests"]
count = len(tests)

line_bits = {}  # relfile -> {line: bitset}
read_bits = {}  # relfile -> bitset
class_bits = {}  # "module:qualname" -> bitset
failed = []

IGNORED_READS = (".coveragerc", ".coveragerc.toml")

for test in tests:
    number = test["number"]
    bit = 1 << number
    meta_path = os.path.join(iso_dir, f"meta.{number}.json")
    cov_path = os.path.join(iso_dir, f"cov.{number}")
    if not (os.path.exists(meta_path) and os.path.exists(cov_path)):
        sys.exit(f"missing isolated run for test {number}")
    meta = json.load(open(meta_path))
    if meta["outcome"] != 0:
        failed.append((number, meta["outcome"]))
    for path in meta["reads"]:
        if "__pycache__" in path or path in IGNORED_READS:
            continue
        read_bits[path] = read_bits.get(path, 0) | bit
    for key in meta["classes"]:
        class_bits[key] = class_bits.get(key, 0) | bit
    data = coverage.CoverageData(cov_path)
    data.read()
    for measured in data.measured_files():
        rel = os.path.relpath(os.path.realpath(measured), root)
        per_line = line_bits.setdefault(rel, {})
        for line in data.lines(measured) or ():
            per_line[line] = per_line.get(line, 0) | bit

if failed:
    print(f"warning: {len(failed)} tests did not pass in isolation: {failed[:10]}", file=sys.stderr)

# Deduplicate bitsets: many lines share the same set of tests.
table = {}


def intern(bits):
    if bits not in table:
        table[bits] = len(table)
    return table[bits]


lines = {f: {line: intern(b) for line, b in d.items()} for f, d in line_bits.items()}
reads = {f: intern(b) for f, b in read_bits.items()}
classes = {k: intern(b) for k, b in class_bits.items()}

files = {}
for test in tests:
    files[test["file"]] = files.get(test["file"], 0) | (1 << test["number"])
test_files = {f: intern(b) for f, b in files.items()}


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_refs import build as build_refs  # noqa: E402

# Comparison probes (run_probes.py): site -> tests where an operand pair was not
# identity-safe, i.e. where swapping `is` and `==` could change the result.
probe_bits, probe_unreliable, probed_files = {}, 0, []
if probe_dir:
    for test in tests:
        number = test["number"]
        path = os.path.join(probe_dir, f"probe.{number}.json")
        if not os.path.exists(path):
            probe_unreliable |= 1 << number
            continue
        record = json.load(open(path))
        if record["outcome"] != 0:
            probe_unreliable |= 1 << number
        for site in record["unsafe"]:
            probe_bits[site] = probe_bits.get(site, 0) | (1 << number)
    cache = os.path.join(probe_dir, "code-cache")
    probed_files = sorted(name[: -len(".marshal")].replace("__", "/") for name in os.listdir(cache) if name.endswith(".marshal"))
    print(f"probes: {len(probe_bits)} unsafe sites, {bin(probe_unreliable).count('1')} unreliable tests, {len(probed_files)} files", file=sys.stderr)

payload = {
    "probes": {site: intern(b) for site, b in probe_bits.items()},
    "probe_unreliable": probe_unreliable,
    "probed_files": probed_files,
    "refs": build_refs(root),
    "count": count,
    "ms": [int(t["seconds"] * 1000 + 0.5) for t in tests],
    "lines": lines,
    "reads": reads,
    "classes": classes,
    "test_files": test_files,
}
# The lookup table is filled last, after every set above has been interned.
sets = [0] * len(table)
for bits, index in table.items():
    sets[index] = bits
payload["sets"] = sets

with gzip.open(out_path, "wb", compresslevel=9) as f:
    pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
print(
    f"{len(lines)} files, {sum(len(d) for d in lines.values())} lines, {len(sets)} distinct sets, "
    f"{len(classes)} classes, {len(reads)} read files -> {os.path.getsize(out_path) / 1e6:.2f} MB"
)
