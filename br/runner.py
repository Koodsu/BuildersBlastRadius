"""In-process runner for sqlglot's unittest-style tests.

Shared by the build-time map builder and the run-time verifier, so both see
tests behave the same way. Process pools are replaced with an executor that
runs the work inline but still pickles arguments and results, which is what
the real spawn workers do (Expr.__reduce__ goes through sqlglot/serde.py).
"""

from __future__ import annotations

import importlib
import pickle
import signal
import time
import unittest
from concurrent.futures import Future


class TestTimeout(BaseException):
    """Raised by the alarm handler. BaseException so `except Exception` cannot eat it."""


class SkipUnit(unittest.SkipTest):
    """Raised from the result of a unit of work that a sliced run leaves out."""


# A test that hands its work to a pool does it in independent units, one per
# submit() call. A sliced run executes only the units a change can reach.
SLICE: dict = {}  # test id -> (mode, keep bitset, expected unit count)
PROBE: dict = {}  # test id -> seconds: running that long without failing counts as a pass
UNIT_HOOK = None  # build time: called with ("submit" | "result", unit number)
_UNITS = {"count": 0, "plan": None}


class _UnitFuture(Future):
    def __init__(self, unit, skipped=False):
        super().__init__()
        self._br_unit = unit
        self._br_skipped = skipped

    def result(self, timeout=None):
        if UNIT_HOOK is not None:
            UNIT_HOOK("result", self._br_unit)
        if self._br_skipped:
            raise SkipUnit()
        return super().result(timeout)


class InlineExecutor:
    """Stands in for ProcessPoolExecutor(mp_context=spawn)."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **_):
        if initializer is not None:
            initializer(*pickle.loads(pickle.dumps(initargs)))

    def submit(self, fn, /, *args, **kwargs):
        unit = _UNITS["count"]
        _UNITS["count"] = unit + 1
        plan = _UNITS["plan"]
        if plan is not None and not plan[1] >> unit & 1:
            future = _UnitFuture(unit, skipped=plan[0] == "skip")
            future.set_result(None)
            return future
        future = _UnitFuture(unit)
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
        except Exception as error:
            future.set_exception(error)
        else:
            try:
                result = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
            except Exception as error:
                try:
                    error = pickle.loads(pickle.dumps(error))
                except Exception:
                    pass
                future.set_exception(error)
            else:
                future.set_result(result)
        if UNIT_HOOK is not None:
            UNIT_HOOK("submit", unit)
        return future

    def map(self, fn, *iterables, timeout=None, chunksize=1):
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return (future.result() for future in futures)

    def shutdown(self, wait=True, *, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_inline_pools():
    import concurrent.futures
    import concurrent.futures.process

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    concurrent.futures.process.ProcessPoolExecutor = InlineExecutor


def limit_duckdb_threads(count=2):
    """duckdb starts a thread per host core for every database; the run has two CPUs
    and a cap on processes."""
    try:
        import duckdb
    except Exception:  # noqa: BLE001
        return
    original = duckdb.connect
    if getattr(original, "_br_limited", False):
        return

    def connect(*args, **kwargs):
        config = dict(kwargs.get("config") or {})
        config.setdefault("threads", count)
        kwargs["config"] = config
        return original(*args, **kwargs)

    connect._br_limited = True
    duckdb.connect = connect


def split_id(test_id: str):
    """'tests/dialects/test_x.py::TestX::test_y' -> ('tests.dialects.test_x', 'TestX', 'test_y')."""
    path, cls, method = test_id.split("::")
    return path[:-3].replace("/", "."), cls, method


class Result(unittest.TestResult):
    """Records which tests failed. A failing subtest ends its test at once."""

    def __init__(self, stop=None):
        self._stop = stop
        self._hit = False
        super().__init__()
        self.failed = set()
        self.unsure = set()
        self.units = {}
        self.timed_out = set()
        self.class_errors = []
        self.seen = set()
        self.report = None
        self.limits = None
        self.timings = None

    # unittest's failfast decorator calls stop(); only the per-test effect is wanted.
    def stop(self):
        pass

    # A subtest that fails ends its test (failfast); one left out of a sliced run does not.
    @property
    def failfast(self):
        return self._hit

    @failfast.setter
    def failfast(self, value):
        pass

    @property
    def shouldStop(self):
        return bool(self._stop and self._stop.get("stop"))

    @shouldStop.setter
    def shouldStop(self, value):
        pass

    def _mark(self, test, err=None):
        test_id = getattr(test, "_br_id", None)
        if test_id is None:
            # setUpClass / tearDownClass / module fixtures: unittest hands an _ErrorHolder.
            self.class_errors.append(str(getattr(test, "description", test)))
            return
        if err is not None and err[0] is not None and issubclass(err[0], TestTimeout):
            if test_id in PROBE:
                return  # it got as far as it was asked to
            self.timed_out.add(test_id)
        self.failed.add(test_id)
        self._hit = True

    def addError(self, test, err):
        self._mark(test, err)

    def addFailure(self, test, err):
        self._mark(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._mark(test, err)

    def addUnexpectedSuccess(self, test):
        self._mark(test)

    def addSuccess(self, test):
        pass

    def addSkip(self, test, reason):
        pass

    def addExpectedFailure(self, test, err):
        pass

    def startTest(self, test):
        super().startTest(test)
        test_id = getattr(test, "_br_id", None)
        self._hit = False
        _UNITS["count"] = 0
        _UNITS["plan"] = SLICE.get(test_id)
        if self.timings is not None:
            self.timings[test_id] = time.process_time()
        if self.limits and test_id in self.limits:
            signal.setitimer(signal.ITIMER_REAL, self.limits[test_id])

    def stopTest(self, test):
        signal.setitimer(signal.ITIMER_REAL, 0)
        super().stopTest(test)
        test_id = getattr(test, "_br_id", None)
        if test_id is None:
            return
        self.seen.add(test_id)
        plan = _UNITS["plan"]
        _UNITS["plan"] = None
        self.units[test_id] = _UNITS["count"]
        if plan is not None and plan[2] != _UNITS["count"]:
            # Not the units the slice was planned for: its verdict means nothing.
            self.unsure.add(test_id)
        if self.timings is not None:
            self.timings[test_id] = time.process_time() - self.timings.get(test_id, 0.0)
        if self.report is not None:
            if test_id in self.timed_out:
                self.report(test_id, "T")
            elif test_id in self.failed:
                self.report(test_id, "F")
            elif test_id in self.unsure:
                self.report(test_id, "U")
            else:
                self.report(test_id, "P")


def _alarm(signum, frame):
    raise TestTimeout()


def run(test_ids, limits=None, report=None, timings=None, stop=None, units=None):
    """Run tests in the given order.

    limits: optional {test_id: seconds} wall limit for a test.
    report: optional callable(test_id, status), called as soon as a test's
            outcome is known. Status is 'P' passed, 'F' failed, 'T' timed out,
            'U' a sliced run that did not go as planned.
    units:  optional dict filled with {test_id: number of units of work it submitted}.
    stop:   optional dict; once stop['stop'] is true no further test starts,
            and tests that never started are not reported.
    Returns (failed_ids, timed_out_ids, class_errors).
    """
    signal.signal(signal.SIGALRM, _alarm)
    failed = set()
    timed_out = set()
    class_errors = []

    groups = []
    for test_id in test_ids:
        module, cls, method = split_id(test_id)
        if groups and groups[-1][0] == (module, cls):
            groups[-1][1].append((test_id, method))
        else:
            groups.append(((module, cls), [(test_id, method)]))

    for (module_name, class_name), members in groups:
        if stop and stop.get("stop"):
            break
        try:
            module = importlib.import_module(module_name)
            klass = getattr(module, class_name)
        except (Exception, SystemExit):  # the file cannot be imported: all its tests fail
            for test_id, _ in members:
                failed.add(test_id)
                if report:
                    report(test_id, "F")
            continue

        cases = []
        for test_id, method in members:
            try:
                case = klass(method)
            except Exception:
                failed.add(test_id)
                if report:
                    report(test_id, "F")
                continue
            case._br_id = test_id
            cases.append(case)
        if not cases:
            continue

        result = Result(stop)
        result.report = report
        result.limits = limits
        result.timings = timings
        try:
            unittest.TestSuite(cases).run(result)
        except TestTimeout:
            pass
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)

        failed |= result.failed | result.unsure
        timed_out |= result.timed_out
        if units is not None:
            units.update(result.units)
        class_errors.extend(result.class_errors)
        if stop and stop.get("stop"):
            break
        # A class whose setUpClass failed never starts its tests: they all error.
        for case in cases:
            if case._br_id not in result.seen:
                failed.add(case._br_id)
                if report:
                    report(case._br_id, "F")

    return failed, timed_out, class_errors
