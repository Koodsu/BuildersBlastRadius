"""Build-time map builder. Runs inside the image against the pinned sqlglot.

For every test, in a fresh fork of a process that has only imported the test's
module, it records which sqlglot/test source lines ran (split into "while a
module was being imported" and "at run time"), which files were opened, the
outcome and the CPU time. Running each test alone means lazily built caches
and lazily imported dialects are charged to every test that needs them.

    python -m br.tracer /sg /app/maps [jobs]
"""

from __future__ import annotations

import importlib
import os
import pickle
import sys
import time
import unittest

from br import cells

MON = sys.monitoring
TOOL = 4
DISABLE = MON.DISABLE
CO_OPTIMIZED = 0x1

SG = ""
SGP = ""

RT: dict = {}  # id(code) -> lines hit at run time
IM: dict = {}  # id(code) -> lines hit while some module under SG was executing
CODES: dict = {}
OPENS: set = set()
DEPTH = 0
CELLS_ON = False
SLICE_MIN_CPU = 0.2  # tests cheaper than this are never worth slicing


def _on_line(code, line):
    cid = id(code)
    if DEPTH:
        lines = IM.get(cid)
        if lines is None:
            if not code.co_filename.startswith(SGP):
                return DISABLE
            CODES[cid] = code
            lines = IM[cid] = set()
        lines.add(line)
        return None
    lines = RT.get(cid)
    if lines is None:
        if not code.co_filename.startswith(SGP):
            return DISABLE
        CODES[cid] = code
        lines = RT[cid] = set()
    lines.add(line)
    return DISABLE


def _note_path(path):
    if isinstance(path, bytes):
        try:
            path = path.decode()
        except Exception:
            return
    if not isinstance(path, str):
        try:
            path = os.fspath(path)
        except Exception:
            return
        if not isinstance(path, str):
            return
    if not path.startswith("/"):
        path = os.path.abspath(path)
    if path.startswith(SGP) and "__pycache__" not in path:
        OPENS.add(os.path.normpath(path)[len(SGP) :])


def _wrap_opens():
    """Record files and directories read from Python. (An audit hook would also
    fire on every id() call, which sqlglot makes constantly.)"""
    import builtins
    import io

    original_open = builtins.open

    def traced_open(file, *args, **kwargs):
        _note_path(file)
        return original_open(file, *args, **kwargs)

    builtins.open = traced_open
    io.open = traced_open

    original_open_code = io.open_code

    def traced_open_code(path):
        _note_path(path)
        return original_open_code(path)

    io.open_code = traced_open_code

    for name in ("listdir", "scandir"):
        original = getattr(os, name)

        def traced(path=".", _original=original):
            _note_path(path)
            return _original(path)

        setattr(os, name, traced)


def _install():
    from importlib.machinery import SourceFileLoader

    original = SourceFileLoader.exec_module

    def exec_module(self, module):
        global DEPTH
        if not getattr(self, "path", "").startswith(SGP):
            return original(self, module)
        if DEPTH == 0:
            MON.restart_events()
            cells.PAUSED[0] += 1
        DEPTH += 1
        try:
            return original(self, module)
        finally:
            DEPTH -= 1
            if DEPTH == 0:
                cells.PAUSED[0] -= 1
                if CELLS_ON:
                    cells.instrument_loaded()
                MON.restart_events()

    SourceFileLoader.exec_module = exec_module
    _wrap_opens()
    MON.use_tool_id(TOOL, "blast-radius")
    MON.register_callback(TOOL, MON.events.LINE, _on_line)
    MON.set_events(TOOL, MON.events.LINE)


class UnitRecorder:
    """Splits one test's coverage by unit of work (see runner.InlineExecutor).

    What runs up to the return of submit(k) belongs to unit k; what runs after
    result(k) is asked for belongs to unit k until the next event. Everything
    else is the test's skeleton, recorded under "A" (always).
    """

    def __init__(self):
        self.total = {}
        self.reads_total = set()
        self.owned = {}
        self.owned_reads = {}
        self.cost = {}
        self.after = None
        self.first = True
        self.count = 0
        self.mark = time.process_time()

    def _flush(self, owners):
        spent = time.process_time() - self.mark
        segment = {cid: lines for cid, lines in RT.items() if lines}
        RT.clear()
        reads = set(cells.READS)
        cells.READS.clear()
        for cid, lines in segment.items():
            self.total.setdefault(cid, set()).update(lines)
        self.reads_total |= reads
        for owner in owners:
            target = self.owned.setdefault(owner, {})
            for cid, lines in segment.items():
                target.setdefault(cid, set()).update(lines)
            self.owned_reads.setdefault(owner, set()).update(reads)
            self.cost[owner] = self.cost.get(owner, 0.0) + spent
        if CELLS_ON:
            cells.rearm()
        MON.restart_events()
        self.mark = time.process_time()

    def event(self, kind, unit):
        if kind == "submit":
            owners = {unit}
            if self.after is not None:
                owners.add(self.after)
            if self.first:
                owners.add("A")
            self.after = None
            self.count = max(self.count, unit + 1)
        else:
            owners = {"A"} if self.after is None else {self.after}
            self.after = unit
        self.first = False
        self._flush(owners)

    def finish(self):
        self._flush({"A"} if self.after is None else {self.after, "A"})
        RT.update(self.total)
        cells.READS.update(self.reads_total)

    def export(self):
        count = self.count
        if count < 2:
            return None
        lines = {}
        reads = {}
        for owner, by_code in self.owned.items():
            bit = 1 << (count if owner == "A" else owner)
            for cid, hit in by_code.items():
                target = lines.setdefault(CODES[cid].co_filename[len(SGP) :], {})
                for line in hit:
                    target[line] = target.get(line, 0) | bit
        for owner, keys in self.owned_reads.items():
            bit = 1 << (count if owner == "A" else owner)
            for key in keys:
                reads[key] = reads.get(key, 0) | bit
        return {
            "count": count,
            "lines": lines,
            "reads": reads,
            "cost": [self.cost.get(unit, 0.0) for unit in range(count)],
            "always": self.cost.get("A", 0.0),
        }


def _snapshot():
    """({file: lines} run time, {file: lines} import functions, {file: lines} import bodies)."""
    runtime: dict = {}
    import_functions: dict = {}
    import_bodies: dict = {}
    for cid, lines in RT.items():
        code = CODES[cid]
        runtime.setdefault(code.co_filename[len(SGP) :], set()).update(lines)
    for cid, lines in IM.items():
        code = CODES[cid]
        target = import_functions if code.co_flags & CO_OPTIMIZED else import_bodies
        target.setdefault(code.co_filename[len(SGP) :], set()).update(lines)
    return runtime, import_functions, import_bodies


def _discover(module):
    loader = unittest.TestLoader()
    found = []
    for name, value in vars(module).items():
        if (
            isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == module.__name__
        ):
            for method in loader.getTestCaseNames(value):
                found.append((name, method))
    return found


def _run_one(test_id, out_path, token_write, with_cells=True):
    """In a forked child: run one test under tracing and write what it touched."""
    global CELLS_ON
    from br import runner

    status = 1
    try:
        if not with_cells:
            CELLS_ON = False
            cells.restore_all()
        RT.clear()
        IM.clear()
        OPENS.clear()
        cells.READS.clear()
        MON.restart_events()
        marks = {}
        started = time.process_time()
        wall = time.perf_counter()

        def report(done_id, state):
            marks["state"] = state

        timings = {}
        recorder = UnitRecorder()
        runner.UNIT_HOOK = recorder.event
        failed, timed_out, class_errors = runner.run([test_id], report=report, timings=timings)
        runner.UNIT_HOOK = None
        recorder.finish()
        cpu = time.process_time() - started
        wall = time.perf_counter() - wall
        runtime, import_functions, import_bodies = _snapshot()
        with open(out_path, "wb") as handle:
            pickle.dump(
                {
                    "id": test_id,
                    "failed": bool(failed) or bool(class_errors),
                    "cpu": cpu,
                    "cpu_test": timings.get(test_id, cpu),
                    "wall": wall,
                    "rt": runtime,
                    "imf": import_functions,
                    "imb": import_bodies,
                    "opens": set(OPENS),
                    "reads": set(cells.READS) if with_cells else None,
                    "units": recorder.export() if with_cells else None,
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        status = 0
    except BaseException as error:  # noqa: BLE001
        sys.stderr.write(f"tracer: {test_id} crashed: {error!r}\n")
    finally:
        try:
            os.write(token_write, b"x")
        finally:
            os._exit(status)


def _zygote(rel_path, out_dir, token_read, token_write):
    """One per test file: import the module once, then fork a child for each test."""
    module_name = rel_path[:-3].replace("/", ".")
    part = os.path.join(out_dir, "parts", module_name)
    os.makedirs(part, exist_ok=True)

    # Untraced timing first: the whole file in one process, the way the verifier runs it.
    os.read(token_read, 1)
    pid = os.fork()
    if pid == 0:
        status = 1
        try:
            from br import runner

            module = importlib.import_module(module_name)
            ids = [f"{rel_path}::{cls}::{method}" for cls, method in _discover(module)]
            timings = {}
            started = time.process_time()
            runner.run(ids, timings=timings)
            total = time.process_time() - started
            with open(os.path.join(part, "_timing.pkl"), "wb") as handle:
                pickle.dump({"timings": timings, "total": total}, handle)
            status = 0
        except BaseException as error:  # noqa: BLE001
            sys.stderr.write(f"tracer: timing {module_name} failed: {error!r}\n")
        finally:
            os.write(token_write, b"x")
            os._exit(status)
    _, timing_status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(timing_status):
        os.write(token_write, b"x")

    _install()
    started = time.process_time()
    try:
        module = importlib.import_module(module_name)
    except BaseException as error:  # noqa: BLE001
        sys.stderr.write(f"tracer: cannot import {module_name}: {error!r}\n")
        os._exit(3)
    import_cpu = time.process_time() - started
    runtime, import_functions, import_bodies = _snapshot()
    eager_functions = {}
    for source in (runtime, import_functions):
        for name, lines in source.items():
            eager_functions.setdefault(name, set()).update(lines)

    tests = _discover(module)
    with open(os.path.join(part, "_file.pkl"), "wb") as handle:
        pickle.dump(
            {
                "file": rel_path,
                "import_cpu": import_cpu,
                "eager_functions": eager_functions,
                "eager_bodies": import_bodies,
                "eager_opens": set(OPENS),
                "tests": [f"{rel_path}::{cls}::{method}" for cls, method in tests],
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )

    global CELLS_ON
    CELLS_ON = True
    cells.instrument_loaded()

    children = 0
    for index, (cls, method) in enumerate(tests):
        os.read(token_read, 1)
        test_id = f"{rel_path}::{cls}::{method}"
        pid = os.fork()
        if pid == 0:
            _run_one(test_id, os.path.join(part, f"{index}.pkl"), token_write)
        children += 1
        # Reap whatever has finished so zombies do not pile up.
        while True:
            try:
                done, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if done == 0:
                break
            children -= 1
            if os.WIFSIGNALED(status):
                os.write(token_write, b"x")
    while children:
        done, status = os.waitpid(-1, 0)
        children -= 1
        if os.WIFSIGNALED(status):
            os.write(token_write, b"x")

    # A test that fails while attribute reads are being recorded is traced again without.
    for index, (cls, method) in enumerate(tests):
        out_path = os.path.join(part, f"{index}.pkl")
        failed = True
        if os.path.exists(out_path):
            with open(out_path, "rb") as handle:
                failed = pickle.load(handle)["failed"]
        if not failed:
            continue
        os.read(token_read, 1)
        pid = os.fork()
        if pid == 0:
            _run_one(f"{rel_path}::{cls}::{method}", out_path, token_write, with_cells=False)
        _, status = os.waitpid(pid, 0)
        if os.WIFSIGNALED(status):
            os.write(token_write, b"x")

    # Tests whose work splits into units: check that a run with units left out still
    # passes, both with every other unit and with none.
    mode = "none" if rel_path == "tests/test_executor.py" else "skip"
    for index, (cls, method) in enumerate(tests):
        out_path = os.path.join(part, f"{index}.pkl")
        if not os.path.exists(out_path):
            continue
        with open(out_path, "rb") as handle:
            data = pickle.load(handle)
        units = data.get("units")
        if data["failed"] or not units or data["cpu_test"] < SLICE_MIN_CPU:
            continue
        test_id = f"{rel_path}::{cls}::{method}"
        os.read(token_read, 1)
        pid = os.fork()
        if pid == 0:
            status = 1
            try:
                from br import runner

                MON.set_events(TOOL, 0)
                cells.restore_all()
                count = units["count"]
                good = True
                for keep in (int("01" * count, 2) & ((1 << count) - 1), 0):
                    runner.SLICE[test_id] = (mode, keep, count)
                    failed, _, class_errors = runner.run([test_id])
                    good = good and not failed and not class_errors
                with open(os.path.join(part, f"{index}.slice"), "w") as handle:
                    handle.write(mode if good else "bad")
                status = 0
            except BaseException as error:  # noqa: BLE001
                sys.stderr.write(f"tracer: slicing {test_id} failed: {error!r}\n")
            finally:
                os.write(token_write, b"x")
                os._exit(status)
        _, status = os.waitpid(pid, 0)
        if os.WIFSIGNALED(status):
            os.write(token_write, b"x")
    os._exit(0)


def _test_files(root):
    found = []
    for directory, _, names in os.walk(os.path.join(root, "tests")):
        for name in names:
            if name.startswith("test_") and name.endswith(".py"):
                found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def main(argv):
    global SG, SGP
    SG = os.path.abspath(argv[1])
    SGP = SG + "/"
    out_dir = os.path.abspath(argv[2])
    jobs = int(argv[3]) if len(argv) > 3 else (os.cpu_count() or 2)
    os.makedirs(os.path.join(out_dir, "parts"), exist_ok=True)

    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, SG)
    os.chdir(SG)

    # Heavy third-party imports happen once, before any fork.
    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    from pandas.testing import assert_frame_equal  # noqa: F401

    from br import runner

    runner.install_inline_pools()

    token_read, token_write = os.pipe()
    os.write(token_write, b"x" * jobs)

    files = _test_files(SG)
    zygotes = {}
    for rel_path in files:
        pid = os.fork()
        if pid == 0:
            _zygote(rel_path, out_dir, token_read, token_write)
        zygotes[pid] = rel_path
    failures = 0
    while zygotes:
        pid, status = os.waitpid(-1, 0)
        rel_path = zygotes.pop(pid, None)
        if rel_path is not None and status != 0:
            failures += 1
            sys.stderr.write(f"tracer: zygote for {rel_path} exited with {status}\n")
    sys.stderr.write(f"tracer: traced {len(files)} files, {failures} zygote failures\n")


if __name__ == "__main__":
    main(sys.argv)
