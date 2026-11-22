"""Runs sqlglot tests in-process, in a forked child, against a patched copy of the tree.

The same code is used offline (to trace which lines each test runs at the pinned
commit) and at decision time (to see which candidate tests a patch breaks).
"""

import importlib
import json
import os
import pickle
import select
import signal
import sys
import time
import unittest

SG = os.environ.get("BR_SG", "/sg")


class TestTimeout(BaseException):
    pass


class InlinePool:
    """Stands in for ProcessPoolExecutor: same pickling of arguments and results, no processes.

    sqlglot's optimizer and executor tests send expressions to spawned workers. Spawned
    workers would import sqlglot again and cost a lot of CPU; the pickling round trip is
    kept because it runs sqlglot code (Expr.__reduce__ goes through sqlglot/serde.py).
    """

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        future = Future()
        try:
            args, kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            result = pickle.loads(pickle.dumps(fn(*args, **kwargs)))
        except TestTimeout:
            raise
        except Exception as error:
            try:
                error = pickle.loads(pickle.dumps(error))
            except Exception as pickling_error:
                error = pickling_error
            future.set_exception(error)
        else:
            future.set_result(result)
        return future

    def shutdown(self, wait=True, cancel_futures=False):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_standins():
    import concurrent.futures as cf
    import concurrent.futures.process as cfp

    cf.ProcessPoolExecutor = InlinePool
    cfp.ProcessPoolExecutor = InlinePool
    duckdb = sys.modules.get("duckdb")
    if duckdb is not None and not getattr(duckdb, "_br_wrapped", False):
        original = duckdb.connect

        def connect(*args, **kwargs):
            # The container's process limit counts threads; one thread also keeps order stable.
            if not args and not kwargs:
                return original(config={"threads": 1})
            return original(*args, **kwargs)

        duckdb.connect = connect
        duckdb._br_wrapped = True


class Result(unittest.TestResult):
    def __init__(self, debug=False):
        super().__init__()
        self.failfast = True  # a failing subtest ends its test: one is enough to fail it
        self.bad = 0
        self.debug = debug

    def _note(self, test, err):
        self.bad += 1
        if self.debug and err:
            sys.stderr.write(f"    FAIL {test}: {err[0].__name__}: {str(err[1])[:300]}\n")

    def addError(self, test, err):
        self._note(test, err)

    def addFailure(self, test, err):
        self._note(test, err)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._note(subtest, err)

    def addUnexpectedSuccess(self, test):
        self._note(test, None)

    def addSuccess(self, test):
        pass

    def addSkip(self, test, reason):
        pass

    def addExpectedFailure(self, test, err):
        pass


def _on_timer(signum, frame):
    raise TestTimeout()


def enter(root):
    if sys.path[0] != root:
        os.chdir(root)
        sys.path.insert(0, root)
        os.environ["PYTHONPATH"] = root
        install_standins()
        signal.signal(signal.SIGPROF, _on_timer)


class StopRun(BaseException):
    pass


def run_tests(root, ids, emit, timeout_for, debug=False):
    """Run tests one by one (grouped by class by the caller); emit(id, ok, cpu_seconds)."""
    enter(root)
    result = Result(debug)
    result._testRunEntered = True  # keeps class fixtures alive between our one-test suites
    broken_modules = set()
    for test_id in ids:
        file, class_name, method = test_id.split("::")
        module_name = file[:-3].replace("/", ".")
        started = time.process_time()
        ok = True
        try:
            signal.setitimer(signal.ITIMER_PROF, timeout_for(test_id))
            if module_name in broken_modules:
                ok = False
            else:
                try:
                    module = importlib.import_module(module_name)
                except TestTimeout:
                    broken_modules.add(module_name)
                    raise
                except BaseException as error:
                    if debug:
                        sys.stderr.write(f"    IMPORT {module_name}: {type(error).__name__}: {str(error)[:300]}\n")
                    broken_modules.add(module_name)
                    module = None
                    ok = False
                if module is not None:
                    klass = getattr(module, class_name, None)
                    if klass is not None and hasattr(klass, method):
                        before = result.bad
                        unittest.TestSuite([klass(method)]).run(result)
                        ok = (
                            result.bad == before
                            and not getattr(klass, "_classSetupFailed", False)
                            and not getattr(result, "_moduleSetUpFailed", False)
                        )
        except TestTimeout:
            ok = False
        except Exception:
            ok = False
        finally:
            signal.setitimer(signal.ITIMER_PROF, 0)
        emit(test_id, ok, time.process_time() - started)


def start_tracing(root):
    """Record every line under root that runs, and every file under root that is opened."""
    lines = {}
    opened = set()
    prefix = root.rstrip("/") + "/"
    mon = sys.monitoring
    tool = mon.COVERAGE_ID
    mon.use_tool_id(tool, "br")

    def on_line(code, line):
        name = code.co_filename
        if name.startswith(prefix):
            entry = lines.get(name)
            if entry is None:
                entry = lines[name] = set()
            entry.add(line)
        return mon.DISABLE

    mon.register_callback(tool, mon.events.LINE, on_line)
    mon.set_events(tool, mon.events.LINE)

    def hook(event, args):
        if event == "open" or event == "os.scandir" or event == "os.listdir":
            path = args[0]
            if isinstance(path, bytes):
                path = path.decode("utf-8", "replace")
            if isinstance(path, str):
                if not path.startswith("/"):
                    path = os.path.join(os.getcwd(), path)
                path = os.path.normpath(path)
                if path.startswith(prefix) or path == root:
                    opened.add(("d:" if event != "open" else "f:") + path[len(prefix):])

    sys.addaudithook(hook)

    def finish():
        mon.set_events(tool, 0)
        return (
            {name[len(prefix):]: sorted(found) for name, found in lines.items()},
            sorted(opened),
        )

    return finish


def fork_run(root, ids, timeout_for, stall_seconds=90.0, trace=False, debug=False, memory_bytes=0,
             probes=(), expected=None, stop_at_failure=False):
    """Fork, run ids in the child, and return ({id: (ok, cpu)}, trace_or_None, status).

    A test still running when the child dies or stalls is reported as failing (a hang is
    a timeout); tests after it are left out of the result so the caller can decide.
    """
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            os.close(read_fd)
            os.dup2(2, 1)
            if memory_bytes:  # off by default: duckdb aborts under an address-space limit
                import resource

                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
            out = os.fdopen(write_fd, "w", buffering=1)
            finish = start_tracing(root) if trace else None

            def emit(test_id, ok, cpu, extra=None):
                out.write(json.dumps([test_id, ok, round(cpu, 5), extra]) + "\n")
                if stop_at_failure and not ok:
                    raise StopRun()

            try:
                if probes:
                    from brx import probes as probe_module

                    enter(root)
                    signal.setitimer(signal.ITIMER_PROF, 120.0)
                    try:
                        probe_module.run(probes, emit, expected)
                    finally:
                        signal.setitimer(signal.ITIMER_PROF, 0)
                run_tests(root, ids, emit, timeout_for, debug)
            except StopRun:
                pass
            if finish is not None:
                out.write(json.dumps({"trace": finish()}) + "\n")
            out.write('"done"\n')
            out.flush()
        except BaseException as error:
            sys.stderr.write(f"child error: {type(error).__name__}: {error}\n")
            code = 1
        finally:
            os._exit(code)
    os.close(write_fd)
    results = {}
    traced = None
    status = "died"
    buffer = b""
    last = time.monotonic()
    while True:
        ready, _, _ = select.select([read_fd], [], [], 1.0)
        if ready:
            chunk = os.read(read_fd, 1 << 16)
            if not chunk:
                break
            buffer += chunk
            last = time.monotonic()
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                item = json.loads(line)
                if item == "done":
                    status = "done"
                elif isinstance(item, dict):
                    traced = item["trace"]
                else:
                    results[item[0]] = (item[1], item[2], item[3])
        elif time.monotonic() - last > stall_seconds:
            status = "stalled"
            break
    os.close(read_fd)
    if status != "done":
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        for test_id in list(probes) + list(ids):
            if test_id not in results:
                results[test_id] = (False, 0.0, None)  # the one in flight
                break
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass
    return results, traced, status
