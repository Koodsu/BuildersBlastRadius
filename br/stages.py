"""Build-time: what the executor's pooled tests do up to the optimizer, and after it.

    python -m br.stages /sg stage_prints.json

For each test in runner.UNIT_LISTS, run once at the pinned commit, this records

  front   per unit, a fingerprint of the optimized expression and the tables the
          executor is given (None for a unit that executes nothing);
  duck    per unit, a fingerprint of the SQL the test hands to duckdb;
  result  per unit, a fingerprint of the table the executor returns;
  back    the sqlglot lines that run after the optimizer returns: planning,
          execution, and the comparison of the rows;
  share   the CPU of a run that is cut short after the optimizer, as a share
          of a full run;
  result_share  the CPU of a run that executes every query but skips the
          comparison with duckdb wherever the result is as recorded.

At run time (runner.STAGED) a unit whose fingerprints still match is cut short
right after the optimizer. That is only done for a change none of whose lines
is in `back`: then everything after the optimizer has the same inputs and the
same code as at the pinned commit.

A later cut, for a change that does reach `back`. What follows the optimizer
is: build the plan, turn each step's expressions into Python source, run that
source over the rows. Only the last is dear. Recorded for it:

  rows    the sqlglot lines a unit runs from the moment the plan is built,
          other than in turning an expression into source: the executor's own
          control and everything it does row by row; and the lines `execute`
          runs before the optimizer, which make the tables the executor reads.
          (From the optimizer to the plan, a run without rows does exactly
          what a run with them does, so those lines need no vouching for.)
  mid     per unit, a fingerprint of the plan and of every piece of source,
          taken from a run over the same tables with their rows removed; None
          for a unit where running over the rows asked for source that the
          run without rows did not make (a subquery run per row), or where
          two such runs did not agree;
  mid_share  the CPU of a run that cuts every such unit, as a share of a full run.

For a change none of whose lines is in `rows`, a unit whose `mid` fingerprint
still matches is cut: the executor's code is as it was, and it is handed the
same plan, the same rows, and the same source for every expression it asks for.
"""

from __future__ import annotations

import json
import os
import sys


class Recorder:
    def __init__(self, root):
        self.root = root
        self.prefix = os.path.join(root, "sqlglot") + os.sep
        self.front_prints = {}
        self.duck_prints = {}
        self.result_prints = {}
        self.duck_kwargs = None
        self.sound = True
        self.back = {}
        self.current = None
        self.entries = None
        self.tracing = False
        self.paused = False
        monitoring = sys.monitoring
        self.monitoring = monitoring
        self.tool = monitoring.COVERAGE_ID
        monitoring.use_tool_id(self.tool, "br-stages")
        monitoring.register_callback(self.tool, monitoring.events.LINE, self._line)
        # Python 3.13 reports no LINE event for the first line of a code object,
        # so a function whose whole body is on that line (a one-line lambda)
        # would never show: its start is recorded as a run of its first line.
        monitoring.register_callback(self.tool, monitoring.events.PY_START, self._start)
        self.events = monitoring.events.LINE | monitoring.events.PY_START

    def _line(self, code, line):
        name = code.co_filename
        if name.startswith(self.prefix):
            self.back.setdefault(name[len(self.root) + 1 :], set()).add(line)
        return self.monitoring.DISABLE

    def _start(self, code, offset):
        return self._line(code, code.co_firstlineno)

    def _set(self, on):
        if on != self.tracing:
            self.monitoring.set_events(self.tool, self.events if on else 0)
            self.tracing = on

    # called by the runner
    def unit(self, unit):
        self._set(False)
        self.current = unit

    def front(self, unit, fingerprint):
        if unit in self.front_prints:
            self.sound = False  # a unit that optimizes twice is not what this models
        self.front_prints[unit] = fingerprint
        self._set(True)

    def result(self, unit, fingerprint):
        if unit in self.result_prints:
            self.sound = False
        self.result_prints[unit] = fingerprint

    def pause(self):
        self.paused = self.tracing
        self._set(False)

    def resume(self):
        self._set(self.paused)

    def duck(self, args, kwargs, fingerprint):
        unit = self.current
        entry = self.entries[unit] if unit is not None and unit < len(self.entries) else None
        if entry is None or len(args) != 1 or args[0] != entry[1] or unit in self.duck_prints:
            self.sound = False
            return
        if self.duck_kwargs is None:
            self.duck_kwargs = dict(kwargs)
        elif self.duck_kwargs != kwargs:
            self.sound = False
        self.duck_prints[unit] = fingerprint

    def finish(self):
        self._set(False)
        self.monitoring.free_tool_id(self.tool)


class RowsRecorder:
    """Which lines a unit runs inside `execute` before the optimizer and after the
    plan is built, outside the generating of source. Stands in for the Recorder in
    the runner."""

    probe = False

    def __init__(self, root, module):
        import sqlglot.executor as executor

        self.root = root
        self.prefix = os.path.join(root, "sqlglot") + os.sep
        self.rows = {}
        self.depth = 0
        self.skip = False  # from the optimizer's start to the plan's completion
        self.tracing = False
        self.paused = False
        monitoring = sys.monitoring
        self.monitoring = monitoring
        self.tool = monitoring.COVERAGE_ID
        monitoring.use_tool_id(self.tool, "br-rows")
        monitoring.register_callback(self.tool, monitoring.events.LINE, self._line)
        monitoring.register_callback(self.tool, monitoring.events.PY_START, self._start)
        self.events = monitoring.events.LINE | monitoring.events.PY_START
        # The runner wraps these names when the test starts: it then wraps these.
        self.saved = (
            executor,
            module,
            executor.optimize,
            executor.Plan,
            executor.PythonExecutor.__dict__["generate"],
            module.execute,
        )
        real_optimize = executor.optimize
        real_plan = executor.Plan
        real = module.execute

        def optimize(*args, **kwargs):
            self.skip = True
            return real_optimize(*args, **kwargs)

        def plan(*args, **kwargs):
            self.skip = True
            try:
                return real_plan(*args, **kwargs)
            finally:
                self.skip = False

        def execute(*args, **kwargs):
            self.skip = False
            self._set(True)
            return real(*args, **kwargs)

        executor.optimize = optimize
        executor.Plan = plan
        executor.PythonExecutor.generate = self._inside(self.saved[4])
        module.execute = execute

    def _inside(self, function):
        def wrapper(*args, **kwargs):
            self.depth += 1
            try:
                return function(*args, **kwargs)
            finally:
                self.depth -= 1

        return wrapper

    def _line(self, code, line):
        if self.depth or self.skip:
            return None  # not counted here; it may yet run outside
        name = code.co_filename
        if name.startswith(self.prefix):
            self.rows.setdefault(name[len(self.root) + 1 :], set()).add(line)
        return self.monitoring.DISABLE

    def _start(self, code, offset):
        return self._line(code, code.co_firstlineno)

    def _set(self, on):
        if on != self.tracing:
            self.monitoring.set_events(self.tool, self.events if on else 0)
            self.tracing = on

    def unit(self, unit):
        self._set(False)

    def front(self, unit, fingerprint):
        pass

    def result(self, unit, fingerprint):
        pass

    def pause(self):
        self.paused = self.tracing
        self._set(False)

    def resume(self):
        self._set(self.paused)

    def duck(self, args, kwargs, fingerprint):
        pass

    def finish(self):
        self._set(False)
        self.monitoring.free_tool_id(self.tool)
        executor, module, optimize, plan, generate, execute = self.saved
        executor.optimize = optimize
        executor.Plan = plan
        executor.PythonExecutor.generate = generate
        module.execute = execute


class MidProbe:
    """Collects each unit's fingerprint from a run over no rows (see runner._Probe)."""

    probe = True

    def __init__(self):
        self.mids = {}
        self.twice = set()

    def mid(self, unit, fingerprint):
        if unit in self.mids:
            self.twice.add(unit)
        self.mids[unit] = fingerprint

    def unit(self, unit):
        pass

    def front(self, unit, fingerprint):
        pass

    def result(self, unit, fingerprint):
        pass

    def pause(self):
        pass

    def resume(self):
        pass

    def duck(self, args, kwargs, fingerprint):
        pass


def add_mid(runner, root, test_id, module, record, whole, count):
    """Adds `rows`, `mid` and `mid_share` to a record; leaves it alone if any check fails."""
    recorder = RowsRecorder(root, module)
    runner.STAGE_TRACE = recorder
    try:
        failed, _, _ = runner.run([test_id])
    finally:
        runner.STAGE_TRACE = None
        recorder.finish()
    if failed or not recorder.rows:
        print(test_id, "rows: the recording run failed")
        return
    rounds = []
    for _ in range(2):
        probe = MidProbe()
        runner.STAGE_TRACE = probe
        try:
            failed, _, _ = runner.run([test_id])
        finally:
            runner.STAGE_TRACE = None
        if failed:
            print(test_id, "mid: a probing run failed")
            return
        rounds.append(
            [None if unit in probe.twice else probe.mids.get(unit) for unit in range(count)]
        )
    mid = [a if a is not None and a == b else None for a, b in zip(*rounds)]
    executed = [unit for unit in range(count) if record["front"][unit] is not None]
    known = [unit for unit in executed if mid[unit] is not None]
    if not known:
        print(test_id, "mid: no unit can be vouched for")
        return
    # A scouting run at the pinned commit: every unit with a fingerprint is cut,
    # and exactly the others are handed back as not judged.
    trial = dict(record, front_ok=False, mid_cut=True, scout=True, mid=mid)
    runner.STAGED[test_id] = trial
    timings = {}
    try:
        runner.run([test_id], timings=timings)
    finally:
        runner.STAGED.pop(test_id, None)
    cut = runner._STAGE["cut"]
    differs = runner.STAGE_DIFFERS.pop(test_id, 0)
    unknown = runner.STAGE_UNKNOWN.pop(test_id, 0)
    runner.STAGE_OVER.discard(test_id)
    expected = sum(1 << unit for unit in executed if mid[unit] is None)
    if cut != len(known) or differs != expected or unknown != expected:
        print(test_id, f"mid: the scouting run does not hold: cut {cut} of {len(known)},"
              f" differs {differs:x}, unknown {unknown:x}, expected {expected:x}")
        return
    share = round(timings[test_id] / whole, 4) if whole > 0 else 1.0
    # ...and a run that cuts those and runs the others to the end passes.
    runner.STAGED[test_id] = dict(record, front_ok=False, mid_cut=True, mid=mid)
    try:
        failed, _, _ = runner.run([test_id])
    finally:
        runner.STAGED.pop(test_id, None)
    if failed or runner._STAGE["cut"] != len(known):
        print(test_id, "mid: the cut run does not pass")
        return
    record["rows"] = {source: sorted(lines) for source, lines in sorted(recorder.rows.items())}
    record["mid"] = mid
    record["mid_share"] = share
    back = sum(len(lines) for lines in record["back"].values())
    inside = sum(
        len(set(lines) & set(record["back"].get(source, ()))) for source, lines in record["rows"].items()
    )
    print(
        test_id,
        f"mid: {len(known)} of {len(executed)} units vouched for, a scouting run"
        f" {share * whole:.2f}s; {inside} of the {back} lines after the optimizer are the rows' own",
    )


def main(argv):
    root = os.path.abspath(argv[1])
    out_path = os.path.abspath(argv[2])
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    from br import runner

    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    found = {}
    for test_id, attribute in sorted(runner.UNIT_LISTS.items()):
        module_name, class_name, _ = runner.split_id(test_id)
        module = __import__(module_name, fromlist=[class_name])
        klass = getattr(module, class_name)

        # 1. A full run, timed.
        timings = {}
        failed, _, _ = runner.run([test_id], timings=timings)
        if failed:
            print(test_id, "fails at the pinned commit: skipped")
            continue
        whole = timings[test_id]

        # 2. A full run, recorded.
        recorder = Recorder(root)
        klass.setUpClass()
        recorder.entries = list(getattr(klass, attribute))
        klass.tearDownClass()
        runner.STAGE_TRACE = recorder
        try:
            failed, _, _ = runner.run([test_id])
        finally:
            runner.STAGE_TRACE = None
            recorder.finish()
        count = len(recorder.entries)
        if failed or not recorder.sound or not recorder.front_prints:
            print(test_id, "cannot be staged")
            continue
        record = {
            "front": [recorder.front_prints.get(unit) for unit in range(count)],
            "duck": [recorder.duck_prints.get(unit) for unit in range(count)],
            "result": [recorder.result_prints.get(unit) for unit in range(count)],
            "duck_kwargs": recorder.duck_kwargs or {},
            "back": {source: sorted(lines) for source, lines in sorted(recorder.back.items())},
        }

        # 3. A staged run: every unit must be cut short, and the test must pass.
        runner.STAGED[test_id] = record
        timings = {}
        try:
            failed, _, _ = runner.run([test_id], timings=timings)
        finally:
            runner.STAGED.pop(test_id, None)
        cut = runner._STAGE["cut"]
        executed = sum(1 for value in record["front"] if value is not None)
        if failed or cut != executed:
            print(test_id, f"staged run does not hold: failed={bool(failed)} cut {cut} of {executed}")
            continue
        record["share"] = round(timings[test_id] / whole, 4) if whole > 0 else 1.0

        # 4. Every query executed, the comparison skipped where the result is as recorded.
        runner.STAGED[test_id] = dict(record, front_ok=False)
        timings = {}
        try:
            failed, _, _ = runner.run([test_id], timings=timings)
        finally:
            runner.STAGED.pop(test_id, None)
        same = runner._STAGE["same"]
        known = sum(1 for value in record["result"] if value is not None)
        if failed or same != known or known != executed:
            print(test_id, f"results do not repeat: failed={bool(failed)} same {same} of {known}")
            record["result"] = [None] * count
            record["result_share"] = 1.0
        else:
            record["result_share"] = round(timings[test_id] / whole, 4) if whole > 0 else 1.0
        found[test_id] = record
        if not os.environ.get("BR_NO_MID"):
            add_mid(runner, root, test_id, module, record, whole, count)
        print(
            test_id,
            f"{executed} of {count} units execute, whole {whole:.2f}s, "
            f"cut after the optimizer {record['share'] * whole:.2f}s, "
            f"comparison skipped {record['result_share'] * whole:.2f}s,"
            f" {sum(len(lines) for lines in record['back'].values())} lines in"
            f" {len(record['back'])} files after the optimizer",
        )
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(found, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


if __name__ == "__main__":
    main(sys.argv)
