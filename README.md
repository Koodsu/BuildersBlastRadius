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
reach. An expensive test is run with only the statement groups that reach the
change: a dialect's catch-all test is hundreds of independent checks, and an
edit to one function is reached by a handful of them. A test known not to need
its class's `setUpClass` runs without it, unless the change is on a line the
setup runs or another candidate of the class needs it. duckdb, numpy and pandas
are stand-ins until a test touches them.

After a first failure a worker is needed anyway, and only the tests that cost
far less to check than to schedule are still checked.

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
| `br/dual.py` | old and new expression evaluated side by side |
| `br/policy.py` | worker packing, verify-or-schedule arithmetic |
| `br/cells.py`, `br/pyindex.py`, `br/patch.py` | fingerprints, code structure, diff parsing |
| `br/tracer.py`, `br/aggregate.py`, `br/timing.py`, `br/fixtures.py`, `br/basestate.py` | build the maps |
| `br/stmts.py`, `br/heavy.py`, `br/setups.py`, `br/modinfo.py` | build the maps that make the run cheaper (`stmts.py` also cuts a test down at run time) |
| `br/reindex.py`, `br/carry.py` | refresh parts of existing maps without tracing again |
| `tools/` | development aids (not in the image) |

## Running

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make maps     # rebuild maps.tar.xz from the pinned commit (a quarter of an hour)
```

`BR_PROBE` and `BR_DUAL` in the `Dockerfile` turn the two kinds of dropping on
evidence on or off.
