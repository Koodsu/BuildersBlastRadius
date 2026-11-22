# BuildersBlastRadius

Our entry for the Blast Radius track: for each patch to sqlglot at the pinned
commit, print which tests to run. The task itself is described in
[CHALLENGE.md](CHALLENGE.md) and the scoring rules in [SCORING.md](SCORING.md).

## How it works

Everything the selector knows about sqlglot comes from maps built from the
pinned commit (`maps.tar.xz`, made by `make maps`). No patch, and no kind of
edit, is ever looked up in a table.

**Maps** (`br/tracer.py`, `br/aggregate.py`, `br/basestate.py`). Every test is
run once, alone, in a fresh fork of a process that has only imported its test
module. Recorded per test:

- the source lines it executes, split into "at run time" and "while a module
  was being imported". Entering a function counts as running its first line:
  Python reports no line event for it, and a lambda written on one line, as
  most entries of sqlglot's tables are, would otherwise never be seen to run;
- the files it opens;
- the class attributes it reads (a descriptor records the first read), and for
  class-level dicts, which entries it looks up;
- for tests that hand work to a process pool, the same again per unit of work;
- how long it takes.

A fingerprint of every module global and class attribute after import is
stored too.

Four smaller maps make the run cheaper (`br/stmts.py`, `br/heavy.py`,
`br/setups.py`, `br/modinfo.py`):

- an expensive test is split into groups of top-level statements that share no
  local name, each group is run on its own, and the lines it executes are
  recorded;
- which tests load duckdb, numpy or pandas;
- which lines each test class's `setUpClass` runs, and which class attributes
  it reads;
- per source file, the names it imports and its `from __future__` features:
  what the compiler needs to compile one function as it would inside its module.

**Selection** (`selector.py`, `br/analyze.py`). A patched Python file is
compiled and compared, code object by code object, with the original. When
every edited line is inside one function only that function is compiled, after
checking that the function as it was comes out the way the maps recorded it
(the parser is 11,000 lines and takes a second to compile whole):

- same code (a comment, a blank line): nothing to run;
- a function body changed: the tests that execute the changed statement;
- class-level or module-level code changed: the patched tree is imported, its
  state is fingerprinted, and the attributes that differ give the tests that
  read them. For a dict, only the tests that look up an entry that changed;
- a fixture or other data file changed: the tests that open it.

**Verification** (`br/verify.py`, `br/runner.py`). When running the candidates
is expected to cost less CPU than scheduling them would cost in simulated time,
they are run against the patched code and only the ones that fail are
scheduled. The run goes cheapest first. Once something fails a worker is
needed anyway, so from then on a test is only run while it is likely enough to
pass, and short enough against what it would add to that worker, to be worth
its CPU; the rest are scheduled unverified. A verifier gets its patched
sqlglot the cheapest way that is safe:

- *hot*: only function bodies changed, and none runs at import time. The new
  code objects are swapped into a forked copy of a process that has sqlglot and
  the test modules imported already.
- *leaf*: the changed modules are ones a bare `import sqlglot` does not load.
  The verifier is forked from a process that has done just that import and
  loads the rest from a view of the tree with the patch applied.
- *cold*: anything else imports the whole patched tree.

Tests that use a process pool are run in-process, with arguments and results
still pickled the way the pool would, and only the units of work the change can
reach. An expensive test is run with only the statement groups that reach the
change: a dialect's catch-all test is hundreds of independent checks, and an
edit to one function is reached by a handful of them. A test known not to need
its class's `setUpClass` runs without it, unless the change is on a line the
setup runs or another candidate of the class needs it. duckdb, numpy and pandas
are stand-ins until a test touches them.

**Old and new side by side** (`br/dual.py`, `BR_DUAL` in the `Dockerfile`).
When the edit is one expression that is safe to evaluate twice (names,
attributes, constants, comparisons, operators), the edited function is given
code that evaluates the new expression, as the patched program does, and each
time the old one as well. A test in which the two never differ runs exactly as
it does without the patch. Every candidate that is cheap to run is run this
way. If none fails, the two forms were compared at least 300 times in at least
20 tests, and they never once differed, the candidates that are dear to run are
left out. That is evidence, not proof: a test that was left out could feed the
expression a value none of the others did. On 436 generated edits to lines that
many tests run, this happened 12 times, every time for an `is` swapped with
`==`, and in none of them did a test that was left out fail, or the two forms
differ even once while it ran. `BR_DUAL=0` turns it off.

**What a check costs** (`BR_CPU_FACTOR`). Running a test here is charged as
CPU at about what the maps estimate for it. Scored with every estimate at 0.4
of that, the selector brought the practice set's simulated seconds from 148.9
down to 103.1 and was charged 89.7 s of CPU for it, against 11.75 s before. So
a check is only worth it when it is much cheaper than the seconds it can take
off the schedule, and the factor is 1.

**Cut short after the optimizer** (`br/stages.py`, `stage_prints.json`).
`test_execute_tpcds` runs 99 queries: each is optimized, then planned and
executed over the TPC-DS tables, and the rows are compared with what duckdb
returns for the same query. Nearly all of its cost comes after the optimizer.
At the pinned commit the build records, for every query, a fingerprint of the
optimized expression and of the SQL handed to duckdb, and the sqlglot lines
that run after the optimizer returns. When a patch edits function bodies and
none of the edited lines is among those, a query whose fingerprints still match
has the same inputs and the same code from there on as it had at the pinned
commit. So the test is first run with every query stopped right after the
optimizer: that executes nothing and shows which queries changed. If none
did, the test passes, exactly, for about a tenth of its cost. If some did,
what is left is a known amount of work: those queries are run in full when
that is worth it (the more of them changed, the less likely they all still
give the same rows), and otherwise the test is scheduled. The first pass
stops early once so much has changed that the test would be scheduled anyway.

**Cut short before the rows** (the same files). An edit to the generator, the
planner or an expression helper does run after the optimizer, so the check
above does not apply. But what follows the optimizer has two very unequal
parts: the query is planned and each step's expressions are turned into Python
source, which is quick; then the executor runs that source over the rows,
which is nearly all of the cost. The build records which sqlglot lines the
second part runs (the executor's own control, and everything it does row by
row), and for every query a fingerprint of the plan and of every piece of
generated source, taken from a run over the same tables with their rows
removed. It checks that a run over the real rows asks for no source that the
run without rows did not make. When none of the edited lines is among the
second part's lines, the test is run with the rows removed: a query whose plan
and source are what they were hands the unchanged executor the same plan, the
same rows and the same source, so its result is what it was. Queries where
something differs are run to the end, as above. That settles the test exactly
for about a sixth of its cost; 1,289 of the 1,884 lines that run after the
optimizer qualify. Edits inside `sqlglot/executor/` never do.

**The comparison with duckdb, skipped** (`stage_prints.json` again). Half of
what `test_execute_tpcds` costs is not sqlglot at all: for every query it runs
the same SQL in duckdb and compares the two tables. The build also records a
fingerprint of the table sqlglot's executor returns for each query (every
cell's type and value, in order). When a query's result has that fingerprint
and the SQL handed to duckdb is unchanged, the comparison comes out as it did
at the pinned commit, so it is not made. This holds for any kind of change,
and brings a full run of the test down to about four tenths of its cost; a
query whose result differs is compared for real.

**The two giants.** `test_execute_tpcds` and `test_max_nodes` are recorded at
about 21 s and 29 s. They are settled last, on what the other candidates
showed:

- *Left out on evidence* (`BR_PROBE=1`): every other test that runs the changed
  statement was run and passed, and there were at least a dozen. That is
  evidence, not proof. With `BR_PROBE=0` this is never done.
- *Checked after the optimizer, or before the rows*: `test_execute_tpcds`,
  when one of the two checks above applies. Exact.
- *Run to the end*: `test_execute_tpcds`, with the comparison skipped as above,
  when the seconds it would add to the schedule, times the chance that it
  passes, come to more than the run. It fails early when it fails: 52 of 54
  failing runs over generated edits had failed within three tenths of a run.
- *Scheduled*: otherwise. `test_max_nodes` costs more to run here than to
  schedule and is never run.

**Units of a test are not isolated.** A pooled test is cut down to the units of
work that reach a change only for edits to function bodies. sqlglot builds
some tables on first use and keeps them (the generators' dispatch tables), so
the unit seen to read a class attribute is only the first that needed it:
class-level changes run such a test whole, and so do edits to a function that
fills a module-level store.

**Fixture edits.** A test whose fixture was edited is scheduled without being
run: it almost always fails. The exception is a giant that takes its work, one
unit for each entry, from a list its class loads from the fixture
(`unit_entries.json` holds a fingerprint of every entry at the pinned commit).
The entries that still match do what they did before, so the giant is run on
the others alone and left out if it passes.

**When in doubt, run it.** New, deleted, renamed and binary files, pytest
configuration, a patch that does not apply, a verifier that crashes or hangs:
all fall back to scheduling every test that could be involved, up to the whole
suite.

## Layout

| | |
| --- | --- |
| `selector.py` | entry point: reads `/data`, prints one line a commit |
| `br/analyze.py` | patch → candidate tests |
| `br/verify.py` | forked verifier: state diff, then run candidates |
| `br/runner.py` | in-process unittest runner, inline process pool |
| `br/dual.py` | old and new expression evaluated side by side |
| `br/policy.py` | worker packing, verify-or-schedule arithmetic |
| `br/cells.py`, `br/pyindex.py`, `br/patch.py` | fingerprints, code structure, diff parsing |
| `br/tracer.py`, `br/aggregate.py`, `br/timing.py`, `br/fixtures.py`, `br/basestate.py` | build the maps |
| `br/entries.py` | fingerprints of fixture entries (`unit_entries.json`) |
| `br/stages.py` | what the executor's tests hand on from the optimizer (`stage_prints.json`) |
| `br/stmts.py`, `br/heavy.py`, `br/setups.py`, `br/modinfo.py` | build the maps that make the run cheaper (`stmts.py` also cuts a test down at run time) |
| `br/reindex.py`, `br/carry.py` | refresh parts of existing maps without tracing again |
| `tools/` | development aids (not in the image) |

## Running

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make maps     # rebuild maps.tar.xz from the pinned commit (a quarter of an hour)
```

`BR_PROBE` and `BR_DUAL` in the `Dockerfile` turn the two kinds of dropping on
evidence on or off; `BR_CPU_FACTOR` is what a recorded second of test time is
taken to cost.
