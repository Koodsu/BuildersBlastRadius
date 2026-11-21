"""In-process runner for sqlglot's unittest-style tests.

Shared by the build-time map builder and the run-time verifier, so both see
tests behave the same way. Process pools are replaced with an executor that
runs the work inline but still pickles arguments and results, which is what
the real spawn workers do (Expr.__reduce__ goes through sqlglot/serde.py).
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import pickle
import signal
import sys
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
# A test listed more than once in a run is run in passes, each with its own
# slice: test id -> [(slice, light)], taken in order. A light pass skips the
# pickling of what the pool's workers are started with; the last pass does it.
PASSES: dict = {}
# Tests that take their units of work from a list held on the class: in a partial
# run the entries that are left out are blanked, so they are not even parsed.
UNIT_LISTS = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": "tpcds_sqls",
    "tests/test_executor.py::TestExecutor::test_execute_tpch": "tpch_sqls",
}
# A patch that only edits the fixture a unit list is loaded from: test id ->
# (slice mode, recorded fingerprint of each entry, cost of each unit, cost allowed).
# The slice is then the units whose entries changed, worked out once the class
# has loaded its list. If the lists do not line up, or the changed units cost
# more than allowed, the test is left out instead.
ENTRY: dict = {}
UNIT_HOOK = None  # build time: called with ("submit" | "result", unit number)
# Tests run with only some of their top-level statements (see br/stmts.py):
# test id -> (the test's statement units, bitset of the units to keep).
STMT: dict = {}
_UNITS = {"count": 0, "plan": None, "light": False, "lazy": False}
NOT_RUN = "left out by the verifier"

# The staged check. A unit of the executor's tests is a query: it is optimized,
# then planned and run over the tables, and the rows are compared with what
# duckdb gives for the same query. Nearly all of the cost is after the
# optimizer. If a change runs no line after that point, and a query's
# optimized expression, its tables and the SQL handed to duckdb are exactly
# what they were at the pinned commit, the rest of that unit does what it did
# then, so it is cut short. A unit where any of them differs runs in full; or,
# when the record says "scout", is cut short as well and noted in
# STAGE_DIFFERS, and the test's verdict is "unsure": the verifier then knows
# which units are left to run, and what they cost, before it runs any of them.
# With "costs" (per unit) and "stop_at" in the record, scouting ends as soon as
# the changed units cost more than that, or once enough units have been looked
# at that, at the rate seen so far, the units still to come would take it well
# past that: the test is then in STAGE_OVER.
STAGED: dict = {}  # test id -> its record from the maps (see br/stages.py)
STAGE_DIFFERS: dict = {}  # test id -> bitset of the units a scouting run found changed
STAGE_OVER: set = set()  # test ids whose scouting run was ended early
STAGE_TRACE = None  # build time: the recorder
_STAGE = {"record": None, "unit": None, "full": set(), "tables": None, "saved": None,
          "entries": None, "cut": 0, "differs": 0, "spent": 0.0, "over": False,
          "looked": 0, "looked_cost": 0.0, "kept_cost": 0.0}
SCOUT_MIN_UNITS = 16  # units to look at before the rate so far says anything
SCOUT_SLACK = 1.0  # ...and how far past the limit it must point (the limit has its own margin)


_RAW = object()  # marks a literal piece of the fingerprint on the walker's stack
_TYPE = object()  # marks a node reached as another node's type


class _Unchanged(BaseException):
    """Raised out of the optimizer's return when a unit is as it was. Not an Exception,
    so nothing between there and the executor swallows it."""


class _Differs(BaseException):
    """Raised the same way by a scouting run when a unit is not as it was."""


def expr_digest(node):
    """A fingerprint of an expression tree: classes, every argument, types, comments,
    meta, and whether each node's links back to its parent are in order.

    Walks the objects directly, so it does not depend on any sqlglot code a
    patch could have changed.
    """
    digest = hashlib.sha1()
    feed = digest.update
    # (item, the node it hangs under, the argument it is, its place in a list)
    stack = [(node, None, None, None)]
    while stack:
        item, owner, key, place = stack.pop()
        if type(item) is bytes and owner is _RAW:
            feed(item)
            continue
        args = getattr(item, "args", None)
        if type(args) is dict and hasattr(item, "parent"):
            feed(b"<" + type(item).__module__.encode() + b"." + type(item).__qualname__.encode())
            if key is not _TYPE and (
                item.parent is not owner
                or getattr(item, "arg_key", None) != key
                or getattr(item, "index", None) != place
            ):
                feed(b"!")
            tail = [(b">", _RAW, None, None)]
            kind = getattr(item, "_type", None)
            if kind is not None and kind is not item:
                tail += [(kind, None, _TYPE, None), (b"T", _RAW, None, None)]
            comments = getattr(item, "comments", None)
            if comments:
                feed(b"C" + repr(comments).encode("utf-8", "backslashreplace"))
            meta = getattr(item, "_meta", None)
            if meta:
                feed(b"M" + repr(meta).encode("utf-8", "backslashreplace"))
            for name, value in reversed(list(args.items())):
                if type(value) is list:
                    tail.append((b"]", _RAW, None, None))
                    tail.extend((child, item, name, at) for at, child in reversed(list(enumerate(value))))
                    tail.append((b"[", _RAW, None, None))
                else:
                    tail.append((value, item, name, None))
                tail.append((b"=" + str(name).encode() + b":", _RAW, None, None))
            stack.extend(tail)
        elif type(item) is list or type(item) is tuple:
            feed(b"(")
            stack.append((b")", _RAW, None, None))
            stack.extend((child, None, None, None) for child in reversed(item))
        else:
            feed(type(item).__name__.encode() + b"'")
            feed(repr(item).encode("utf-8", "backslashreplace") + b"'")
    return digest.hexdigest()[:20]


def text_digest(value):
    return hashlib.sha1(repr(value).encode("utf-8", "backslashreplace")).hexdigest()[:20]


def _stage_install(test):
    """Wrap the optimizer as the executor calls it, and the test module's own
    `execute` and `transpile`. Returns False if the names are not there."""
    try:
        import sqlglot.executor as executor

        module = sys.modules[type(test).__module__]
        saved = {
            "executor": executor,
            "module": module,
            "optimize": executor.optimize,
            "execute": module.execute,
            "transpile": module.transpile,
        }
    except Exception:  # noqa: BLE001
        return False

    def optimize(*args, **kwargs):
        result = saved["optimize"](*args, **kwargs)
        unit = _STAGE["unit"]
        if unit is None:
            return result
        if STAGE_TRACE is not None:
            STAGE_TRACE.front(unit, text_digest((_STAGE["tables"], expr_digest(result))))
            return result
        record = _STAGE["record"]
        if record is None or unit >= len(record["front"]):
            return result
        expected = record["front"][unit]
        if (
            unit not in _STAGE["full"]
            and expected is not None
            and expected == text_digest((_STAGE["tables"], expr_digest(result)))
        ):
            raise _Unchanged()
        if record.get("scout"):
            raise _Differs()
        return result  # this unit runs on: planning, execution, comparison of its rows

    def execute(*args, **kwargs):
        tables = kwargs.get("tables")
        _STAGE["tables"] = sorted(map(str, tables)) if type(tables) is dict else None
        return saved["execute"](*args, **kwargs)

    def transpile(*args, **kwargs):
        if STAGE_TRACE is None:
            return saved["transpile"](*args, **kwargs)
        STAGE_TRACE.pause()
        try:
            result = saved["transpile"](*args, **kwargs)
        finally:
            STAGE_TRACE.resume()
        STAGE_TRACE.duck(args, kwargs, text_digest(result))
        return result

    executor.optimize = optimize
    module.execute = execute
    module.transpile = transpile
    _STAGE["saved"] = saved
    return True


def _stage_remove():
    saved = _STAGE["saved"]
    if saved is not None:
        saved["executor"].optimize = saved["optimize"]
        saved["module"].execute = saved["execute"]
        saved["module"].transpile = saved["transpile"]
    _STAGE.update(record=None, unit=None, tables=None, saved=None, entries=None, differs=0)
    _STAGE.update(spent=0.0, over=False, looked=0, looked_cost=0.0)
    _STAGE["full"] = set()


def _stage_duck(unit):
    """Is the SQL this unit hands to duckdb what it was? If not, the unit runs in full."""
    record = _STAGE["record"]
    expected = record["duck"][unit] if unit < len(record["duck"]) else None
    if expected is None:
        return  # at the pinned commit this unit compared nothing
    try:
        sql = _STAGE["entries"][unit][1]
        produced = _STAGE["saved"]["transpile"](sql, **record["duck_kwargs"])
        same = text_digest(produced) == expected
    except Exception:  # noqa: BLE001
        same = False
    if not same:
        _STAGE["full"].add(unit)


def entry_digest(entry):
    return hashlib.sha1(repr(entry).encode("utf-8", "backslashreplace")).hexdigest()[:16]


def _entry_plan(test, test_id):
    """The slice for a test whose fixture entries were edited, or None to leave it out."""
    mode, digests, costs, allowed = ENTRY[test_id]
    entries = getattr(type(test), UNIT_LISTS[test_id], None)
    if not isinstance(entries, list) or len(entries) != len(digests):
        return None
    changed = 0
    spent = 0.0
    for unit, entry in enumerate(entries):
        if entry_digest(entry) != digests[unit]:
            changed |= 1 << unit
            spent += costs[unit]
    if spent > allowed:
        return None
    return (mode, changed, len(digests))


def _execute(future, fn, args, kwargs):
    staged = _STAGE["saved"] is not None
    if staged and _STAGE["over"]:
        future.set_result(None)  # scouting has seen enough: nothing more is run
        return
    if staged:
        unit = future._br_unit
        if STAGE_TRACE is not None:
            STAGE_TRACE.unit(unit)
        elif _STAGE["record"] is not None:
            _stage_duck(unit)
        _STAGE["unit"] = unit
        _STAGE["tables"] = None
    try:
        result = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
    except (_Unchanged, _Differs) as outcome:
        unit = future._br_unit
        record = _STAGE["record"] or {}
        costs = record.get("costs")
        cost = costs[unit] if costs is not None and unit < len(costs) else 0.0
        _STAGE["looked"] += 1
        _STAGE["looked_cost"] += cost
        if isinstance(outcome, _Unchanged):
            _STAGE["cut"] += 1
        else:
            _STAGE["differs"] |= 1 << unit
            _STAGE["spent"] += cost
            if _STAGE["spent"] > record.get("stop_at", float("inf")):
                _STAGE["over"] = True
            elif (
                _STAGE["looked"] >= SCOUT_MIN_UNITS
                and _STAGE["looked_cost"] > 0
                and _STAGE["spent"] / _STAGE["looked_cost"] * _STAGE["kept_cost"]
                > SCOUT_SLACK * record.get("stop_at", float("inf"))
            ):
                _STAGE["over"] = True
        future.set_result(None)
    except Exception as error:
        try:
            error = pickle.loads(pickle.dumps(error))
        except Exception:
            pass
        future.set_exception(error)
    else:
        future.set_result(result)
    finally:
        if staged:
            _STAGE["unit"] = None


class _UnitFuture(Future):
    def __init__(self, unit, skipped=False):
        super().__init__()
        self._br_unit = unit
        self._br_skipped = skipped
        self._br_call = None  # work put off until its result is asked for

    def result(self, timeout=None):
        if UNIT_HOOK is not None:
            UNIT_HOOK("result", self._br_unit)
        if self._br_skipped:
            raise SkipUnit()
        if self._br_call is not None:
            call, self._br_call = self._br_call, None
            _execute(self, *call)
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
            if _UNITS["lazy"]:
                # The test takes results one at a time and checks each: doing the
                # work then lets a unit that fails end the test before the rest run.
                future._br_call = (fn, args, kwargs)
            else:
                _execute(future, fn, args, kwargs)
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
# takes each module's place until something reads an attribute of it or imports
# one of its submodules.
HEAVY_MODULES = ("numpy", "pandas", "pandas.testing", "duckdb")
# What has to be real before each of them is: pandas and duckdb reach for
# numpy's C interface while they load.
_HEAVY_NEEDS = {
    "numpy": ("numpy",),
    "pandas": ("numpy", "pandas", "pandas.testing"),
    "pandas.testing": ("numpy", "pandas", "pandas.testing"),
    "duckdb": ("numpy", "duckdb"),
}
_DUCKDB_THREADS = [None]
_LAZY: dict = {}
_LAZY_LOADED: list = []  # what this process had to load itself
# Attributes the import system and introspection look for on any module: a
# stand-in answers these itself instead of loading the real module.
_OWN_DUNDERS = frozenset(
    (
        "__path__", "__spec__", "__loader__", "__file__", "__cached__", "__package__",
        "__wrapped__", "__all__", "__builtins__", "__getattr__", "__dir__", "__warningregistry__",
        "__origin__", "__func__", "__self__", "__code__", "__module__", "__qualname__",
        "__mro_entries__", "__set_name__", "__get__", "__set__", "__delete__",
    )
)


class _LazyModule(types.ModuleType):
    def __getattr__(self, name):
        if name in _OWN_DUNDERS:
            raise AttributeError(name)
        value = getattr(load_heavy(self.__name__), name)
        try:
            object.__setattr__(self, name, value)
        except Exception:  # noqa: BLE001
            pass
        return value


class _LazyFinder:
    """Importing a submodule of a stand-in loads the real package first."""

    @staticmethod
    def find_spec(name, path=None, target=None):
        top = name.partition(".")[0]
        if top in _LAZY and name not in _LAZY:
            load_heavy(top)
            import importlib.util

            return importlib.util.find_spec(name)
        return None


def load_heavy(name):
    """Replace a stand-in, and the ones it depends on, with the real modules."""
    import sys

    for key in _HEAVY_NEEDS.get(name, (name,)):
        stand_in = _LAZY.pop(key, None)
        if stand_in is None:
            continue
        if sys.modules.get(key) is stand_in:
            del sys.modules[key]
        _LAZY_LOADED.append(key)
        try:
            importlib.import_module(key)
        except Exception:  # noqa: BLE001
            pass
        if key == "duckdb" and _DUCKDB_THREADS[0] is not None:
            limit_duckdb_threads(_DUCKDB_THREADS[0])
    return importlib.import_module(name)


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
    sys.meta_path.insert(0, _LazyFinder)


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
        self.gate = None
        self.not_run = set()

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
        self._trimmed = None
        self._swapped = None
        _UNITS["plan"] = None
        leave_out = (
            test_id is not None and self.gate is not None and not self.gate(test_id)
        )
        _UNITS["count"] = 0
        _UNITS["light"] = False
        plan = None
        if not leave_out:
            passes = PASSES.get(test_id)
            if test_id in ENTRY:
                plan = _entry_plan(test, test_id)
                leave_out = plan is None
            elif passes:
                plan, _UNITS["light"] = passes.pop(0)
            else:
                plan = SLICE.get(test_id)
        if leave_out:
            # unittest looks the method up after this call: give it one marked as skipped.
            def left_out(*args, **kwargs):
                pass

            left_out.__unittest_skip__ = True
            left_out.__unittest_skip_why__ = NOT_RUN
            setattr(test, test._testMethodName, left_out)
            self.not_run.add(id(test))
            return
        _UNITS["plan"] = plan
        # Only at run time: the maps record a unit's lines while it is submitted.
        _UNITS["lazy"] = UNIT_HOOK is None and test_id in UNIT_LISTS
        if test_id in UNIT_LISTS and (STAGE_TRACE is not None or test_id in STAGED):
            if _stage_install(test):
                _STAGE["record"] = STAGED.get(test_id) if STAGE_TRACE is None else None
                _STAGE["entries"] = getattr(type(test), UNIT_LISTS[test_id], None)
                _STAGE["cut"] = 0
                costs = (_STAGE["record"] or {}).get("costs") or ()
                _STAGE["kept_cost"] = sum(
                    cost
                    for unit, cost in enumerate(costs)
                    if plan is None or plan[1] >> unit & 1
                )
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
        if _STAGE["saved"] is not None:
            if _STAGE["differs"]:
                STAGE_DIFFERS[test_id] = _STAGE["differs"]
                self.unsure.add(test_id)  # those units have not been run
                if _STAGE["over"]:
                    STAGE_OVER.add(test_id)
            _stage_remove()
        if getattr(self, "_swapped", None) is not None:
            function, original = self._swapped
            function.__code__ = original
            self._swapped = None
        if id(test) in self.not_run:
            return
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


def run(
    test_ids, limits=None, report=None, timings=None, stop=None, units=None, bare=None, gate=None
):
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
    gate:   optional callable(test_id), asked just before a test starts. False
            leaves the test out: it is not run and not reported.
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
        result.gate = gate
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
