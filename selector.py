"""Blast radius selector.

For each patch: find the tests that could be affected using maps built from
the pinned sqlglot commit, then either run those tests against the patched
code (when that is cheaper than scheduling them) and report the ones that
fail, or schedule them all. Anything not understood runs everything.
"""

from __future__ import annotations

import json
import os
import sys

if os.environ.get("PYTHONHASHSEED") != "0":
    # State fingerprints are compared with ones made at build time under seed 0.
    os.environ["PYTHONHASHSEED"] = "0"
    os.execv(sys.executable, [sys.executable] + sys.argv)

import select  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402
import time  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from br import analyze, verify  # noqa: E402
from br.policy import CPU_FACTOR, Policy, pack, plan_slices, slice_cost  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
ROOT = os.environ.get("BR_SG", "/sg")

PARALLEL = int(os.environ.get("BR_PARALLEL", "2"))
AUDIT = os.environ.get("BR_DUAL_AUDIT") in ("1", "2")  # development: see br/verify.py
WALL_BUDGET = float(os.environ.get("BR_WALL", "120"))  # stop verifying after this many seconds
MEMORY_BYTES = 16 << 30  # address space; duckdb reserves far more than it touches
RSS_LIMIT = 1800 << 20  # resident memory at which a verifier is killed
GIANT_RSS_LIMIT = 2600 << 20  # ...while it runs a giant: test_max_nodes needs 1.6 GB
PAGE = os.sysconf("SC_PAGE_SIZE")
START = time.monotonic()
ANSWERED: list = []  # set once the answers have been written
OPTIMIZER = "tests/test_optimizer.py"
POOLED = (OPTIMIZER, "tests/test_executor.py")


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


class Job:
    def __init__(self, analysis, mode):
        self.commit = analysis.commit
        self.mode = mode
        self.patched = analysis.patched
        self.cands = analysis.cands
        self.coarse = analysis.coarse
        self.diff_modules = analysis.diff_modules
        self.modules = analysis.modules
        self.translate = analysis.translate
        self.forced = analysis.forced
        self.unnamed = analysis.unnamed
        self.full = analysis.full
        self.targets = analysis.targets
        self.no_slice = analysis.no_slice
        self.sure = analysis.sure
        self.data_bits = analysis.data_bits
        self.code_bits = analysis.code_bits
        self.tests_python = analysis.tests_python
        self.hot = analysis.hot
        self.reads_source = analysis.reads_source
        self.dual = analysis.dual
        self.probe = False  # only checking whether the edit changes anything at all
        self.plan = None  # decided up front when no state diff is needed
        self.retried = False
        self.read_fd = None
        self.write_fd = None
        self.reset()

    def reset(self):
        self.pid = None
        self.own_child = False
        self.buffer = b""
        self.keep = []
        self.evidence = []  # giants left out if everything that is run passes
        self.late = []  # giants scheduled unless run here and seen to pass
        self.giant = False  # running a giant now
        self.verify = []
        self.states = {}
        self.announced = False
        self.retry = False
        self.done = False
        self.deadline = 0.0


def main():
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    ms = {test["number"]: int(test["seconds"] * 1000 + 0.5) for test in tests}
    number_of = {test["id"]: test["number"] for test in tests}
    all_numbers = sorted(ms)

    commits_dir = os.path.join(DATA, "commits")
    commits = sorted(name[:-6] for name in os.listdir(commits_dir) if name.endswith(".patch"))

    everything = pack(all_numbers, ms)[1]
    output = {}

    try:
        maps = analyze.Maps(MAPS)
    except Exception as error:  # noqa: BLE001
        log("no maps, running everything:", repr(error))
        for commit in commits:
            print(json.dumps({"commit": commit, "workers": everything}))
        return

    meta = maps.meta
    to_number = [number_of.get(test_id) for test_id in maps.tests]
    known = {number for number in to_number if number is not None}
    # Tests the maps know nothing about are scheduled whenever anything changed.
    unknown = [number for number in all_numbers if number not in known]
    traced = meta.get("cpu_test") or meta["cpu"]
    plain = meta.get("cpu_plain") or [None] * len(traced)
    ours = [traced[i] if plain[i] is None else plain[i] for i in range(len(traced))]
    # A test that does all its work in-process costs here what it was recorded at.
    # Tests that hand work to a process pool were recorded with that work spread
    # over several cores; in-process they cost our own measurement, converted at
    # the rate the in-process tests give.
    mine = theirs = 0.0
    for i, test_id in enumerate(maps.tests):
        number = to_number[i]
        if number is None or test_id.startswith(POOLED):
            continue
        if ms[number] >= 20 and ours[i] > 0:
            mine += ours[i]
            theirs += ms[number] / 1000
    scale = min(5.0, max(0.1, theirs / mine)) if mine > 0 and theirs > 0 else 1.0
    # ...and all of it at what the scorer charges for a recorded second.
    cpu = []
    for i, test_id in enumerate(maps.tests):
        number = to_number[i]
        if number is not None and not test_id.startswith(POOLED):
            cpu.append(ms[number] / 1000 * CPU_FACTOR)
        else:
            cpu.append(ours[i] * scale * CPU_FACTOR)
    setup_cost = {
        key: value * scale * CPU_FACTOR
        for key, value in (meta.get("class_setup") or {}).items()
    }
    order = {test["id"]: position for position, test in enumerate(tests)}
    policy = Policy(
        maps.tests, to_number, ms, cpu, setup_cost, order, unknown, bare=meta.get("no_setup", ())
    )
    log(f"timing scale {scale:.2f}")

    waiting = []  # analyses that need a verifier
    for commit in commits:
        try:
            with open(os.path.join(commits_dir, commit + ".patch"), encoding="utf-8") as handle:
                text = handle.read()
            result = analyze.analyze(commit, text, maps, ROOT)
        except Exception as error:  # noqa: BLE001
            log(commit, "analysis failed, running everything:", repr(error))
            output[commit] = everything
            continue
        notes = "; ".join(result.notes)
        if result.fallback:
            log(commit, "fallback:", notes)
            output[commit] = everything
            continue
        if not result.patched:
            log(commit, "nothing changed:", notes)
            output[commit] = []
            continue

        outside = [path for path in result.patched if not path.startswith(("sqlglot/", "tests/"))]
        if outside:
            # The patched view only covers sqlglot/ and tests/: do not trust a run of it.
            numbers = [to_number[i] for i in policy.indexes(result.cands | result.coarse)] + unknown
            log(commit, f"schedule {len(numbers)} (files outside the tree view):", notes)
            output[commit] = pack(numbers, ms)[1]
            continue
        if not (result.cands | result.coarse) and not unknown:
            log(commit, "no candidates:", notes)
            output[commit] = []
            continue
        if (
            result.sure
            and not result.diff_modules
            and not any(policy.is_giant(i) for i in policy.indexes(result.cands))
        ):
            numbers = [to_number[i] for i in policy.indexes(result.cands)] + unknown
            cost, workers = pack(numbers, ms)
            log(commit, f"schedule {len(numbers)} ({cost / 1000:.1f}s, sure to fail):", notes)
            output[commit] = workers
            continue
        waiting.append((result, notes))

    # How each remaining commit can get its patched sqlglot (see verify.py).
    core = set()
    closure = {}
    if waiting:
        try:
            state = maps.state()
            core = set(state["core_modules"])
            closure = state["closure"]
        except Exception as error:  # noqa: BLE001
            log("no state map:", repr(error))

    def beyond_core(result):
        """True if nothing the change can reach is loaded by a bare `import sqlglot`."""
        touched = set(result.modules) | set(result.diff_modules)
        if not core or not touched:
            return False
        if any(path.startswith("tests/") and path.endswith(".py") for path in result.patched):
            return False
        reach = set(touched)
        for module in touched:
            reach.update(closure.get(module, [module]))
        return not (reach & core)

    modes = []
    for result, notes in waiting:
        python_only = all(
            path.startswith("sqlglot/") and path.endswith(".py") for path in result.patched
        )
        if result.hot_ok and result.hot and not result.diff_modules and python_only:
            modes.append("hot")
        elif beyond_core(result):
            modes.append("leaf")
        else:
            modes.append("cold")
    # A server only pays for itself with a couple of jobs to share it.
    if modes.count("hot") < 2 and not AUDIT:
        modes = [
            ("leaf" if beyond_core(result) else "cold") if mode == "hot" else mode
            for mode, (result, _) in zip(modes, waiting)
        ]
    if modes.count("hot") == 0 and modes.count("leaf") < 2:
        modes = ["cold" if mode == "leaf" else mode for mode in modes]
    if os.environ.get("BR_MODE") == "cold":
        modes = ["cold"] * len(modes)  # for checking the quick ways against the sure one

    jobs = []
    for (result, notes), mode in zip(waiting, modes):
        commit = result.commit
        job = Job(result, mode)
        if not result.diff_modules:
            indexes = policy.indexes(result.cands)
            slices = plan_slices(
                maps, policy, indexes, result.full, result.targets, (), result.no_slice
            )
            to_verify, keep, estimate, evidence, late = policy.decide(
                indexes,
                mode=mode,
                slices=slices,
                sure=result.sure,
                droppable=result.cands & ~result.full if result.targets else 0,
            )
            if AUDIT and result.dual is not None and mode == "hot":
                # Measuring: run every candidate but the giants, whatever it costs
                # (and the giants too when asked).
                giant = 10**9 if os.environ.get("BR_AUDIT_GIANTS") == "1" else 6000
                to_verify = [i for i in indexes if ms[to_number[i]] < giant]
                keep = [i for i in indexes if ms[to_number[i]] >= giant]
                evidence, late = [], []
            if (
                not to_verify
                and not evidence
                and not late
                and not result.sure
                and result.dual is not None
                and mode == "hot"
                and verify.DUAL
                and not unknown
            ):
                # Scheduling looked cheaper than checking. But when old and new can
                # be compared as they run, the cheap candidates alone may show that
                # the edit changes nothing, and then nothing needs scheduling.
                cheap = [i for i in indexes if slice_cost(slices, i, policy.cpu[i]) <= verify.DUAL_CHEAP]
                rest = [i for i in indexes if i not in set(cheap)]
                spend = sum(slice_cost(slices, i, policy.cpu[i]) for i in cheap)
                everything_cost = pack([to_number[i] for i in indexes], ms)[0] / 1000
                if (
                    rest
                    and len(cheap) >= verify.DUAL_MIN_TESTS
                    and spend <= max(0.5 * CPU_FACTOR, verify.DUAL_PROBE_SHARE * everything_cost)
                ):
                    cheap.sort(
                        key=lambda i: (
                            maps.tests[i].rsplit("::", 1)[0],
                            slice_cost(slices, i, policy.cpu[i]),
                        )
                    )
                    to_verify, keep, estimate = cheap, rest, spend
                    job.probe = True
            if not to_verify and not evidence and not late:
                numbers = [to_number[i] for i in indexes] + unknown
                cost, workers = pack(numbers, ms)
                log(commit, f"schedule {len(numbers)} ({cost / 1000:.1f}s):", notes)
                output[commit] = workers
                continue
            job.plan = (indexes, to_verify, keep, estimate, evidence, late, slices)
        log(commit, f"verify ({mode}):", notes)
        jobs.append(job)

    log(f"analysis done: {time.process_time():.2f}s cpu")
    if jobs:
        verify.WALL_END = START + WALL_BUDGET
        try:
            run_jobs(jobs, maps, policy, output)
        except Exception as error:  # noqa: BLE001
            # Whatever went wrong, every commit still gets an answer.
            log("verification broke down:", repr(error))
        for job in jobs:
            if job.commit not in output:
                numbers = [to_number[i] for i in policy.indexes(job.cands | job.coarse)] + unknown
                output[job.commit] = pack(sorted(set(numbers)), ms)[1]
                log(job.commit, f"scheduled {len(numbers)} unverified after the breakdown")

    lines = [
        json.dumps({"commit": commit, "workers": output.get(commit, everything)}, separators=(",", ":"))
        for commit in commits
    ]
    ANSWERED.append(True)
    sys.stdout.write("\n".join(lines) + ("\n" if lines else ""))
    sys.stdout.flush()
    log(f"done in {time.monotonic() - START:.1f}s wall, {time.process_time():.2f}s own cpu")


def run_jobs(jobs, maps, policy, output):
    to_number = policy.to_number
    ms = policy.ms
    unknown = policy.unknown

    def pooled(job):
        if job.plan is not None:
            return any(
                maps.tests[i].startswith(POOLED) for i in job.plan[1] + job.plan[4] + job.plan[5]
            )
        bits = job.cands | job.coarse
        return any(
            bits >> i & 1 for i, test_id in enumerate(maps.tests) if test_id.startswith(POOLED)
        )

    import concurrent.futures.process  # noqa: F401
    import unittest  # noqa: F401

    from br import basestate, cells, runner  # noqa: F401

    # Maps that every state-diff job reads: with two or more such jobs they are
    # loaded once here instead of once in each verifier.
    if sum(1 for job in jobs if job.plan is None) >= 2:
        try:
            maps.cell_readers()
            maps.entries()
            maps.key_readers()
            maps.setups()
        except Exception as error:  # noqa: BLE001
            log("could not load the state maps ahead of time:", repr(error))

    # The optimizer and executor test modules import duckdb, numpy and pandas, and
    # few of their tests use them: stand-ins load the real thing on first use.
    heavy = maps.heavy()
    if os.environ.get("BR_LAZY", "1") == "1" and heavy is not None:
        runner.install_lazy_heavy()
        # A module that two or more jobs will load is loaded once, here.
        index_of = {test_id: i for i, test_id in enumerate(maps.tests)}
        wanted = {}
        for test_id, names in heavy["tests"].items():
            i = index_of.get(test_id)
            if i is not None:
                for name in names:
                    wanted[name] = wanted.get(name, 0) | 1 << i
        unknown_bits = 0  # tests of those files the map does not know: assume everything
        for i, test_id in enumerate(maps.tests):
            if test_id.split("::", 1)[0] in heavy["files"] and test_id not in heavy["tests"]:
                unknown_bits |= 1 << i
        for name in ("numpy", "duckdb", "pandas"):
            users = 0
            for job in jobs:
                if job.plan is not None:
                    likely = job.plan[1]
                    if job.dual is not None and verify.DUAL:
                        # The dear ones are mostly left out unrun.
                        likely = [
                            i
                            for i in likely
                            if slice_cost(job.plan[6], i, policy.cpu[i]) <= verify.DUAL_CHEAP
                        ]
                    bits = sum(1 << i for i in likely)
                else:
                    bits = job.cands | job.coarse
                if bits & (wanted.get(name, 0) | unknown_bits):
                    users += 1
            # Candidates are a guess at what will run: a job that could use pandas
            # ends up loading it about one time in four, duckdb most of the time.
            needed = 4 if name == "pandas" else 2
            if users >= needed * int(os.environ.get("BR_PRELOAD", "1")):
                try:
                    runner.load_heavy(name)
                    log(f"{name} loaded here for {users} jobs")
                except Exception as error:  # noqa: BLE001
                    log(f"{name} would not load: {error!r}")
    elif sum(1 for job in jobs if pooled(job)) >= 2:
        try:
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            from pandas.testing import assert_frame_equal  # noqa: F401
        except Exception:  # noqa: BLE001
            pass

    # Servers that have sqlglot imported already (see verify.py).
    servers = {}
    server_pid = None
    served = [job for job in jobs if job.mode in ("hot", "leaf")]
    if served:
        test_modules = sorted(
            {
                maps.tests[i].split("::", 1)[0][:-3].replace("/", ".")
                for job in jobs
                if job.mode == "hot" and job.plan is not None
                for i in job.plan[1] + job.plan[4] + job.plan[5]
            }
        )
        for job in served:
            job.read_fd, job.write_fd = os.pipe()
        leaf_read, leaf_write = os.pipe()
        hot_read, hot_write = os.pipe()
        server_pid = os.fork()
        if server_pid == 0:
            os.close(leaf_write)
            os.close(hot_write)
            for job in served:
                os.close(job.read_fd)
            verify.core_server(
                ROOT, jobs, policy, maps, MEMORY_BYTES, leaf_read, hot_read, test_modules
            )
            os._exit(0)
        os.close(leaf_read)
        os.close(hot_read)
        for job in served:
            os.close(job.write_fd)
            job.write_fd = None
        servers = {"leaf": leaf_write, "hot": hot_write}

    log(f"imports done: {time.process_time():.2f}s cpu")
    # Jobs that share a server go first, so the servers can exit early.
    pending = sorted(jobs, key=lambda job: {"hot": 0, "leaf": 1, "cold": 2}[job.mode])
    running = {}
    optimizer = [
        to_number[i]
        for i, test_id in enumerate(maps.tests)
        if test_id.startswith(OPTIMIZER) and to_number[i] is not None
    ]

    poller = select.poll()

    def finish(job):
        if not job.announced:
            # The verifier never said what it found: assume every possible candidate.
            numbers = [to_number[i] for i in policy.indexes(job.cands | job.coarse)]
            note = "NO ANSWER"
        else:
            # "Q": left out because old and new never differed in anything that ran.
            unsettled = [i for i in job.verify if job.states.get(i) not in ("P", "Q")]
            clean = job.done and not unsettled
            spared = [i for i, state in job.states.items() if state == "Q"]
            numbers = [to_number[i] for i in unsettled]
            numbers += [to_number[i] for i in job.keep if not (clean and job.states.get(i) == "Q")]
            timed_out = any(state == "T" for state in job.states.values())
            note = ""
            if spared:
                if clean:
                    note = f"left out {len(spared)} that cannot differ "
                else:
                    numbers += [to_number[i] for i in spared]
            # A giant seen to pass here is left out, and so is one spared with the
            # rest. One that was not run is left out only on evidence: every other
            # test run, all passed, no giant failed.
            giants = job.evidence + job.late
            doubt = not clean or any(job.states.get(i) not in (None, "P", "Q") for i in giants)
            dropped = 0
            for i in giants:
                state = job.states.get(i)
                if state == "P" or (state == "Q" and clean):
                    continue
                if state == "Q":
                    continue  # counted with the spared ones above
                if i in job.late or doubt:
                    numbers.append(to_number[i])
                else:
                    dropped += 1
            if dropped:
                note += f"dropped {dropped} on evidence"
            if timed_out or not job.done:
                note = "TIMEOUT" if timed_out else "INCOMPLETE"
                # A hang or a dead verifier in the optimizer suite stalls its shared
                # pool. A giant that ran out of time here has no part in that.
                hung = any(job.states.get(i) == "T" for i in job.verify)
                cut_short = not job.done and any(i not in job.states for i in job.verify)
                if (hung or cut_short) and any(
                    maps.tests[i].startswith(OPTIMIZER) for i in job.verify
                ):
                    numbers += optimizer
        numbers = sorted(set(numbers) | set(unknown))
        cost, workers = pack(numbers, ms)
        passed = sum(1 for state in job.states.values() if state == "P")
        log(
            job.commit,
            f"-> {len(numbers)} tests ({cost / 1000:.1f}s): {passed} of {len(job.verify)} "
            f"checked and passed, {len(job.keep)} kept {note} [{time.monotonic() - START:.1f}s]",
        )
        output[job.commit] = workers

    def launch(job):
        job.reset()
        if job.mode in servers:
            os.write(servers[job.mode], f"{jobs.index(job)}\n".encode())
        else:
            job.mode = "cold"
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                for other in jobs:
                    if other.read_fd is not None:
                        try:
                            os.close(other.read_fd)
                        except OSError:
                            pass
                for fd in servers.values():
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                verify.child(ROOT, job, "cold", policy, maps, write_fd, MEMORY_BYTES, log_fd=2)
                os._exit(0)
            os.close(write_fd)
            job.pid = pid
            job.own_child = True
            job.read_fd = read_fd
        job.deadline = time.monotonic() + (3600.0 if AUDIT else 45.0 * verify.SLOW)
        running[job.read_fd] = job
        poller.register(job.read_fd, select.POLLIN | select.POLLHUP | select.POLLERR)

    def reap(job, kill=False):
        if kill and job.pid:
            try:
                os.kill(job.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            poller.unregister(job.read_fd)
        except (KeyError, OSError, ValueError):
            pass
        try:
            os.close(job.read_fd)
        except OSError:
            pass
        if job.own_child:
            try:
                os.waitpid(job.pid, 0)
            except OSError:
                pass
        if job.pid:
            shutil.rmtree(f"/tmp/br{job.pid}", ignore_errors=True)
        del running[job.read_fd]
        job.read_fd = None
        if (job.retry or not job.announced) and not job.retried and job.mode != "cold" and not kill:
            # The quick way did not work out: do it the slow, sure way.
            job.retried = True
            job.mode = "cold"
            if job.diff_modules:
                job.plan = None
            pending.insert(0, job)
            return
        finish(job)

    def close_idle_servers():
        for mode in list(servers):
            if not any(job.mode == mode for job in pending) and not any(
                job.mode == mode for job in running.values()
            ):
                try:
                    os.close(servers.pop(mode))
                except OSError:
                    pass

    while pending or running:
        out_of_time = time.monotonic() - START > WALL_BUDGET
        while pending and len(running) < PARALLEL and not out_of_time:
            launch(pending.pop(0))
        if out_of_time:
            for job in pending:
                job.announced = False
                finish(job)
                log(job.commit, "out of time: scheduled without verifying")
            pending = []
            for job in list(running.values()):
                reap(job, kill=True)
            break
        if not running:
            continue
        ready = [fd for fd, _ in poller.poll(250)]  # poll: no limit on descriptor numbers
        now = time.monotonic()
        for fd in ready:
            job = running.get(fd)
            if job is None:
                continue
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                reap(job)
                continue
            job.buffer += chunk
            while b"\n" in job.buffer:
                line, job.buffer = job.buffer.split(b"\n", 1)
                if line == b"D":
                    job.done = True
                elif line == b"X":
                    job.retry = True
                elif line == b"A":
                    job.announced = True
                    # Costs are the scorer's; allow for a much slower machine.
                    estimate = sum(policy.cpu[i] for i in job.verify) / CPU_FACTOR
                    job.deadline = now + (
                        3600.0 if AUDIT else (30.0 + 6.0 * estimate) * verify.SLOW
                    )
                else:
                    kind, _, value = line.partition(b" ")
                    number = int(value)
                    if kind == b"I":
                        job.pid = number
                    elif kind == b"K":
                        job.keep.append(number)
                    elif kind == b"E":
                        job.evidence.append(number)
                    elif kind == b"L":
                        job.late.append(number)
                    elif kind == b"G":
                        # It may have queued behind another verifier's giant.
                        job.giant = True
                        job.deadline = now + (
                            3600.0
                            if AUDIT
                            else (90.0 + 5.0 * policy.cpu[number] / CPU_FACTOR) * verify.SLOW
                        )
                    elif kind == b"V":
                        job.verify.append(number)
                    else:
                        job.states[number] = kind.decode()
        for job in list(running.values()):
            if now > job.deadline:
                log(job.commit, "verifier past its deadline")
                reap(job, kill=True)
                continue
            if not job.pid:
                continue
            try:
                with open(f"/proc/{job.pid}/statm") as handle:
                    resident = int(handle.read().split()[1]) * PAGE
            except (OSError, ValueError, IndexError):
                continue
            if resident > (GIANT_RSS_LIMIT if job.giant else RSS_LIMIT):
                log(job.commit, "verifier using too much memory")
                reap(job, kill=True)
        close_idle_servers()

    for fd in servers.values():
        try:
            os.close(fd)
        except OSError:
            pass
    if server_pid:
        try:
            os.waitpid(server_pid, 0)
        except OSError:
            pass


def last_resort():
    """Something unforeseen broke before any answer was printed: run everything."""
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    ms = {test["number"]: int(test["seconds"] * 1000 + 0.5) for test in tests}
    everything = pack(sorted(ms), ms)[1]
    for name in sorted(os.listdir(os.path.join(DATA, "commits"))):
        if name.endswith(".patch"):
            print(json.dumps({"commit": name[:-6], "workers": everything}, separators=(",", ":")))
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        log("selector broke down:", repr(error))
        if not ANSWERED:
            last_resort()
