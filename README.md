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
  was being imported";
- the files it opens;
- the class attributes it reads (a descriptor records the first read), and for
  class-level dicts, which entries it looks up;
- for tests that hand work to a process pool, the same again per unit of work;
- how long it takes.

A fingerprint of every module global and class attribute after import is
stored too.

**Selection** (`selector.py`, `br/analyze.py`). A patched Python file is
compiled and compared, code object by code object, with the original:

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
reach. Tests known not to need their class's `setUpClass` run without it.

**What a check costs** (`BR_CPU_FACTOR`). The seconds in `tests.json` are not
what a test costs to run here: two scored runs put the container's CPU at
about a third of the recorded time of the work it did. Every CPU estimate is
the recorded-time estimate times `BR_CPU_FACTOR` (0.4, a little on the safe
side).

**Cut short after the optimizer** (`br/stages.py`, `stage_prints.json`).
`test_execute_tpcds` runs 99 queries: each is optimized, then planned and
executed over the TPC-DS tables, and the rows are compared with what duckdb
returns for the same query. Nearly all of its cost comes after the optimizer.
At the pinned commit the build records, for every query, a fingerprint of the
optimized expression and of the SQL handed to duckdb, and the sqlglot lines
that run after the optimizer returns. When a patch edits function bodies and
none of the edited lines is among those, a query whose fingerprints still match
has the same inputs and the same code from there on as it had at the pinned
commit, so it is stopped there; a query whose fingerprints differ runs in
full, and the first one that fails ends the test. That settles the test
exactly for about a tenth of its cost.

**The two giants.** `test_execute_tpcds` and `test_max_nodes` are recorded at
about 21 s and 29 s and cost about 13 s of CPU each to run here in full. When
the check above does not apply they are settled last, on what the other
candidates showed:

- *Left out on evidence* (`BR_PROBE=1`): every other test that runs the changed
  statement was run and passed, and there were at least a dozen. That is
  evidence, not proof. With `BR_PROBE=0` this is never done.
- *Run for real*: otherwise, if the seconds the giant would add to the schedule,
  times the chance that it passes, come to more than running it. A giant that
  works through a fixture one query at a time runs only the queries that reach
  the change, the cheap ones first in a pass of their own, and stops at the
  first that fails.
- *Scheduled*: when neither applies.

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
| `br/policy.py` | worker packing, verify-or-schedule arithmetic |
| `br/cells.py`, `br/pyindex.py`, `br/patch.py` | fingerprints, code structure, diff parsing |
| `br/tracer.py`, `br/aggregate.py`, `br/timing.py`, `br/fixtures.py`, `br/basestate.py` | build the maps |
| `br/entries.py` | fingerprints of fixture entries (`unit_entries.json`) |
| `br/stages.py` | what the executor's tests hand on from the optimizer (`stage_prints.json`) |
| `br/reindex.py`, `br/carry.py` | refresh parts of existing maps without tracing again |
| `tools/` | development aids (not in the image) |

## Running

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make maps     # rebuild maps.tar.xz from the pinned commit (ten minutes or so)
```

`BR_PROBE` in the `Dockerfile` turns dropping on evidence on or off;
`BR_CPU_FACTOR` is what a recorded second of test time is taken to cost.
