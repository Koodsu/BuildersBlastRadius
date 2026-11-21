"""Build-time: what the executor's pooled tests do up to the optimizer, and after it.

    python -m br.stages /sg stage_prints.json

For each test in runner.UNIT_LISTS, run once at the pinned commit, this records

  front   per unit, a fingerprint of the optimized expression and the tables the
          executor is given (None for a unit that executes nothing);
  duck    per unit, a fingerprint of the SQL the test hands to duckdb;
  back    the sqlglot lines that run after the optimizer returns: planning,
          execution, and the comparison of the rows;
  share   the CPU of a run that is cut short after the optimizer, as a share
          of a full run.

At run time (runner.STAGED) a unit whose fingerprints still match is cut short
right after the optimizer. That is only done for a change none of whose lines
is in `back`: then everything after the optimizer has the same inputs and the
same code as at the pinned commit.
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
        found[test_id] = record
        print(
            test_id,
            f"{executed} of {count} units execute, whole {whole:.2f}s, staged {timings[test_id]:.2f}s,"
            f" {sum(len(lines) for lines in record['back'].values())} lines in"
            f" {len(record['back'])} files after the optimizer",
        )
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(found, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


if __name__ == "__main__":
    main(sys.argv)
