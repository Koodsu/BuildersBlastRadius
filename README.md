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
- the class attributes it reads (a descriptor records the first read);
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
  read them;
- a fixture or other data file changed: the tests that open it.

**Verification** (`br/verify.py`, `br/runner.py`). When running the candidates
is expected to cost less CPU than scheduling them would cost in simulated time,
they are run against the patched code and only the ones that fail are
scheduled. The run goes cheapest first and stops at the first failure: a worker
is needed from then on, so the rest are scheduled unverified. A verifier gets
its patched sqlglot the cheapest way that is safe:

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

**Dropping on evidence** (`BR_PROBE` in the `Dockerfile`). Two tests are too
expensive to verify (about 21 s and 29 s). With `BR_PROBE=1`, such a test is
left out when every other test that runs the changed statement has been run and
has passed. That is evidence, not proof. With `BR_PROBE=0` it is scheduled
whenever it could be affected.

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
| `br/tracer.py`, `br/aggregate.py`, `br/timing.py`, `br/fixtures.py`, `br/basestate.py`, `br/reindex.py` | build the maps |
| `tools/` | development aids (not in the image) |

## Running

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make maps     # rebuild maps.tar.xz from the pinned commit (ten minutes or so)
```

`BR_PROBE` in the `Dockerfile` turns dropping on evidence on or off.
