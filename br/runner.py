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
import types
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
LIGHT: set = set()  # test ids probed on a few units: skip the pool's start-up pickling
# Tests that take their units of work from a list held on the class: in a partial
# run the entries that are left out are blanked, so they are not even parsed.
UNIT_LISTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
}
UNIT_HOOK = None  # build time: called with ("submit" | "result", unit number)
# Tests run with only some of their top-level statements (see br/stmts.py):
# test id -> (the test's statement units, bitset of the units to keep).
STMT: dict = {}
_UNITS = {"count": 0, "plan": None, "light": False}


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
            if _UNITS["light"]:
                initializer(*initargs)
            else:
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
    import sys

    duckdb = sys.modules.get("duckdb")
    if isinstance(duckdb, _LazyModule):
        _DUCKDB_THREADS[0] = count  # applied when the real module is loaded
        return
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


# The optimizer and executor test modules import duckdb, numpy and pandas at the
# top, which costs most of a second, and few of their tests touch them. A stand-in
# takes each module's place until something reads an attribute of it.
HEAVY_MODULES = ("numpy", "pandas", "pandas.testing", "duckdb")
_DUCKDB_THREADS = [None]
_LAZY: dict = {}


class _LazyModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        value = getattr(load_heavy(self.__name__), name)
        try:
            object.__setattr__(self, name, value)
        except Exception:  # noqa: BLE001
            pass
        return value


def load_heavy(name=None):
    """Replace the stand-ins with the real modules. Returns the one asked for."""
    import sys

    if _LAZY:
        stand_ins = dict(_LAZY)
        _LAZY.clear()
        for key, module in stand_ins.items():
            if sys.modules.get(key) is module:
                del sys.modules[key]
        # numpy first: pandas and duckdb reach for its C interface while loading.
        for key in HEAVY_MODULES:
            if key in stand_ins:
                try:
                    importlib.import_module(key)
                except Exception:  # noqa: BLE001
                    pass
        if _DUCKDB_THREADS[0] is not None:
            limit_duckdb_threads(_DUCKDB_THREADS[0])
    return importlib.import_module(name) if name else None


def install_lazy_heavy():
    """Put stand-ins for the heavy third-party modules into sys.modules."""
    import sys

    if any(name in sys.modules for name in HEAVY_MODULES):
        return
    for name in HEAVY_MODULES:
        module = _LazyModule(name)
        module.__path__ = []  # lets `from pandas.testing import ...` resolve the stand-in
        module.__spec__ = None
        _LAZY[name] = module
        sys.modules[name] = module

    def assert_frame_equal(*args, **kwargs):
        return load_heavy("pandas.testing").assert_frame_equal(*args, **kwargs)

    _LAZY["pandas.testing"].assert_frame_equal = assert_frame_equal
    _LAZY["pandas"].testing = _LAZY["pandas.testing"]


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
        # With a "keep_going" set the suite carries on and skips the tests outside it.
        stop = self._stop
        return bool(stop and stop.get("stop") and not stop.get("keep_going"))

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
        _UNITS["plan"] = plan = SLICE.get(test_id)
        _UNITS["light"] = test_id in LIGHT
        self._trimmed = None
        self._swapped = None
        cut = STMT.get(test_id)
        if cut is not None:
            try:
                from br import stmts

                function = stmts.target_function(type(test), test._testMethodName)
                self._swapped = stmts.install(
                    type(test), test._testMethodName, cut[0], cut[1], function.__code__.co_filename
                )
            except Exception:  # noqa: BLE001
                self._swapped = None  # it runs whole
        attribute = UNIT_LISTS.get(test_id) if plan is not None else None
        if attribute is not None:
            entries = getattr(type(test), attribute, None)
            if isinstance(entries, list) and len(entries) == plan[2]:
                blank = ({}, "SELECT 1", "")
                setattr(
                    type(test),
                    attribute,
                    [entry if plan[1] >> unit & 1 else blank for unit, entry in enumerate(entries)],
                )
                self._trimmed = (type(test), attribute, entries)
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
        if getattr(self, "_swapped", None) is not None:
            function, original = self._swapped
            function.__code__ = original
            self._swapped = None
        if getattr(self, "_trimmed", None) is not None:
            owner, attribute, entries = self._trimmed
            setattr(owner, attribute, entries)
            self._trimmed = None
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


class _Suite(unittest.TestSuite):
    """A suite that, once something has failed, only runs the tests still worth checking."""

    _cleanup = False
    _br_stop = None

    def __iter__(self):
        stop = self._br_stop
        for case in self._tests:
            if stop and stop.get("stop") and case._br_id not in stop.get("keep_going", ()):
                continue
            yield case


_MISSING = object()


def _strip_class_fixtures(klass):
    """Make a class's setUpClass and tearDownClass do nothing; returns what to restore."""
    saved = {}
    for name in ("setUpClass", "tearDownClass"):
        saved[name] = klass.__dict__.get(name, _MISSING)
        setattr(klass, name, classmethod(lambda cls: None))
    return saved


def _restore_class_fixtures(klass, saved):
    for name, value in saved.items():
        if value is _MISSING:
            delattr(klass, name)
        else:
            setattr(klass, name, value)


def run(test_ids, limits=None, report=None, timings=None, stop=None, units=None, bare=None):
    """Run tests in the given order.

    limits: optional {test_id: seconds} wall limit for a test.
    report: optional callable(test_id, status), called as soon as a test's
            outcome is known. Status is 'P' passed, 'F' failed, 'T' timed out,
            'U' a sliced run that did not go as planned.
    units:  optional dict filled with {test_id: number of units of work it submitted}.
    bare:   optional set of test ids known to pass without their class's
            setUpClass. A class whose tests here are all in it skips that setup.
    stop:   optional dict; once stop['stop'] is true no further test starts,
            and tests that never started are not reported. If it holds a set
            'keep_going', the tests in that set are still run.
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
            allowed = stop.get("keep_going") or ()
            members = [member for member in members if member[0] in allowed]
            if not members:
                continue
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
        stripped = None
        if bare and all(case._br_id in bare for case in cases):
            stripped = _strip_class_fixtures(klass)
        try:
            suite = _Suite(cases)
            suite._br_stop = stop
            suite.run(result)
        except TestTimeout:
            pass
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            if stripped is not None:
                _restore_class_fixtures(klass, stripped)

        failed |= result.failed | result.unsure
        timed_out |= result.timed_out
        if units is not None:
            units.update(result.units)
        class_errors.extend(result.class_errors)
        if stop and stop.get("stop"):
            continue  # what did not run was left out on purpose
        # A class whose setUpClass failed never starts its tests: they all error.
        for case in cases:
            if case._br_id not in result.seen:
                failed.add(case._br_id)
                if report:
                    report(case._br_id, "F")

    return failed, timed_out, class_errors
