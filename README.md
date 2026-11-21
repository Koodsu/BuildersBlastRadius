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
costs less CPU than scheduling them would cost in simulated time, they are run
against a view of the tree with the patch applied (symlinks plus the changed
files, in `/tmp`), and only the ones that fail are scheduled. The run stops at
the first failure and schedules the rest. Tests that use a process pool are
run in-process, with arguments and results still pickled the way the pool
would, and only the units of work the change can reach.

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
| `br/tracer.py`, `br/aggregate.py`, `br/basestate.py` | build the maps |
| `tools/` | development aids (not in the image) |

## Running

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
make maps     # rebuild maps.tar.xz from the pinned commit (a few minutes)
```

`BR_PROBE` in the `Dockerfile` turns probing on or off; see the comment there.
