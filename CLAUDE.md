# Blast Radius: handoff notes

Track: for each sqlglot patch, print which tests to run and how to split them over up to
8 workers. Cost per commit = busiest worker's recorded seconds + 2 s per worker
(empty selection = 0); +300 s if any failing test is left out; + container CPU time.
Exact rules: SCORING.md. Local scoring: `make sample` (30 sample commits, answers included).
Submissions: one per hour. Cutoff Sun 11:00 AM ET. Final uses ~1.5x commits, unseen
kinds of change; we are told top entries are read, so keep it principled (no patch->tests tables).

## Files

- `selector.py` - the selector (stdlib only, runs with `python -S`).
- `run.py` - entry point (imports the precompiled selector).
- `Dockerfile` - downloads sqlglot at the pinned commit to /app/src, unpacks `map.pkl.gz`.
- `map.pkl.gz` - per-test map built from the pinned commit (BUILT, 0.37 MB, all 1620 tests passed in isolation).
  Rebuild only if you change mapper/: `make -f Makefile.map map`.
- `mapper/mutation_check.py` - mutation testing: random mutants, labeled by the full suite, checked for misses.
- `mapper/run_isolated.py` - runs every test in its own forked process with coverage
  (incl. spawn process-pool workers, which the optimizer/executor tests use), records files
  each test reads and the sqlglot classes it instantiates.
- `mapper/run_probes.py` - comparison probes (see Results).
- `mapper/build_refs.py` - index of where every ALL_CAPS name is read.
- `mapper/build_map.py` - turns all of that into map.pkl.gz (deduplicated bitsets).
- `mapper/Dockerfile.map` - runs both in the exact labeling image (Python 3.13, SQLite, requirements.lock).

Without map.pkl.gz the selector runs every test, split optimally (~37 s/commit, never misses).

## How selection works

Patch -> apply hunks to /app/src file -> parse old and new with `ast` -> compare each
scope's own code (nested defs as placeholders, docstring and positions dropped):
- changed function -> tests whose process executed any line of its body
- changed class body -> tests that instantiated it or a subclass + users of its methods;
  never-instantiated class -> every test that imported the module
- changed module-level code / unparseable new file / deleted file -> every importer
- any change -> plus tests that read the file as text (e.g. test_typing reads typing/*.py)
- data/fixture file -> tests that read it; new file -> readers of files in its directory
- pyproject/setup.cfg/tox.ini/pytest.ini -> all tests only if pytest settings or syntax change; conftest -> all
Then LPT packing for k = 1..8 workers, pick min(max load + 2 s * k).

## Results on the 30 sample commits

0 misses. Simulated 113.3 s (v1 175.8 -> board 213.0; v2 117.2 -> board 200.5; v3 114.7; this is v4).
Perfect-knowledge floor on the samples (only the failing tests, best split) is 94.4 s.
Selector CPU 0.3 s for 30 commits.

v2 added two refinements, both sound by construction:
- Setting-level: if a class's only change is the value of ALL_CAPS settings (`WEEK_OFFSET = -1`),
  select tests that use the class AND execute a statement reading the name (`.NAME`, bare
  `NAME`, or the string "NAME"; index in mapper/build_refs.py). Settings read at import time
  (metaclass) make every importer a reader, so it falls back to class users. 535z: 171 -> 7 tests.
- Comparison probes (mapper/run_probes.py): every `is`/`is not`/`==`/`!=` in sqlglot is
  instrumented and records operand types per test. A patch that only swaps `is`<->`==` (or
  `is not`<->`!=`) at one site keeps only tests that compared a type with its own `==` there;
  for all others `==` falls back to identity, so execution is identical. 2haz, 66ks: 25 s -> 0.
  Verified negatively: `len(x) == 1` -> `is 1` (int has its own ==) keeps its 9 users.
Output identical when source comes from the tarball the Dockerfile downloads.
Remaining big costs are test_execute_tpcds (21.5 s), which really fails on 2jrq and 3utz,
and is selected-but-passing on 66ks (`is None` -> `== None`) and 2haz (`is` -> `==` on an enum).
Do NOT special-case "equivalent" edits: Expression overrides __eq__ and sqlglot enums subclass str.

Statement-level refinement (BLAST_REFINE=1, default): a changed function selects only users of
the changed statements, via difflib alignment of statement lists; falls back to the whole
function when locals/yield/global change, statements are inserted at function entry/exit,
or the signature/decorators change.

Instance tracking also covers spawn process-pool workers (optimizer/executor tests): 535z
(TSQL.WEEK_OFFSET) breaks test_simplify only through a worker.

v3 added (probes in mapper/run_probes.py, all sound by measurement):
- Ordering swaps (< <= > >= == !=): per test, whether the new operator would ever have given
  another result at that site (alternatives evaluated on the real operands every time).
- Default values: per test, which default-valued parameters were called with their default
  object. A change of only default values keeps tests that used that default.
- Module-level ALL_CAPS constants: like class settings; a constant read inside a default value
  counts only for calls that used that default.
On 80 synthetic mutants: 526 -> 496.5 s. Remaining big costs are slow tests (test_execute_tpcds
21.5 s, test_max_nodes 28.8 s) that really execute the changed code with the changed value;
tpcds passed on 8 of 10 synthetic selections, but proving that needs running it.

v4 (found with ground truth: realistic mutants labeled by the full suite):
- Edits inside else/elif branches no longer add the first `if` header's users (redundant: the
  edited statement or its neighbours already cover every test that reaches it). Sound.
- A statement that only assigns a different constant to local names selects tests that run the
  assignment AND a statement reading those names (falls back on global/nonlocal, locals(),
  vars(), eval, exec). `is_underscore_separated = True -> False`: 36.8 s -> 2.5 s (perfect).
- Ground truth: 11 realistic mutants, 0 misses, 48.7 s vs perfect 12.2 s. NOTE pytest 9 reports
  subtest failures as `SUBFAILED[...] id` or `SUBFAILED(...) id` beside a PASSED test; parse
  every line starting FAILED/ERROR/SUBFAILED for the tests/... id (see truth labeling).

## Verified earlier

- Environment reproduces the labels: test ids match tests.json exactly, all pass on the pinned commit.
- Patch application matches `git apply` byte-for-byte on 20 sample patches.
- Partial map (419/1620 tests): 0 misses among mapped tests; every miss was an unmapped test.
- Selector CPU: 0.16 s for 20 commits.

## Next steps (map is built; start at 2)

1. `make -f Makefile.map map` (30-45 min). Check its stderr for tests that did not pass in isolation.
2. Copy selector.py, run.py, Dockerfile, map.pkl.gz, mapper/ into the kit folder; `make sample`.
   Target: 0 misses. Inspect any miss: which scope changed, why its users missed the failing test.
3. Push, submit. Map is small (<5 MB) - under the 20 MB repo limit.
4. Then optimize cost (only after 0 misses):
   - The biggest cost driver is `tests/test_parser.py::TestParser::test_max_nodes` (28.8 s): any
     selection containing it costs >= 30.8 s. Check how often core changes pull it in.
   - Function-level users may be refined to line-level users for changed lines that were
     executed (a flipped condition only affects tests that ran that line) - but a removed or
     added line needs function level. Measure misses before keeping this.
   - Never trade misses for seconds: one miss = 300 s, more than most commits cost in total.

5. Mutation testing for unseen kinds (the final has them): on a multi-core box, in an env
   matching requirements.lock (e.g. the map image):
   `python mapper/mutation_check.py <sqlglot_src> sample/tests.json 50`
   Every line must say `ok`. A MISS shows the file and the missed tests: fix the selector's
   rule (never a lookup by patch), rebuild nothing unless mapper/ changed, re-run make sample.
