"""Blast radius selector.

For each patch: find the tests that could be affected using maps built from
the pinned sqlglot commit, then either run those tests against the patched
tree (when that is cheaper than scheduling them) and report the ones that
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
from br.policy import Policy, pack, plan_slices  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
MAPS = os.environ.get("BR_MAPS", os.path.join(HERE, "maps"))
ROOT = os.environ.get("BR_SG", "/sg")

PARALLEL = 2
WALL_BUDGET = float(os.environ.get("BR_WALL", "120"))  # stop verifying after this many seconds
MEMORY_BYTES = 16 << 30  # address space; duckdb reserves far more than it touches
RSS_LIMIT = 1800 << 20  # resident memory at which a verifier is killed
PAGE = os.sysconf("SC_PAGE_SIZE")
START = time.monotonic()
OPTIMIZER = "tests/test_optimizer.py"


def log(*parts):
    print(*parts, file=sys.stderr, flush=True)


class Job:
    def __init__(self, analysis):
        self.commit = analysis.commit
        self.patched = analysis.patched
        self.cands = analysis.cands
        self.coarse = analysis.coarse
        self.diff_modules = analysis.diff_modules
        self.translate = analysis.translate
        self.forced = analysis.forced
        self.unnamed = analysis.unnamed
        self.full = analysis.full
        self.targets = analysis.targets
        self.no_slice = analysis.no_slice
        self.sure = analysis.sure
        self.pid = None
        self.fd = None
        self.buffer = b""
        self.keep = []
        self.verify = []
        self.states = {}
        self.announced = False
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
    cpu = [traced[i] if plain[i] is None else plain[i] for i in range(len(traced))]
    # The maps were timed on another machine. Tests that do all their work in-process
    # should cost here what they were recorded at, which gives the exchange rate.
    ours = theirs = 0.0
    for i, test_id in enumerate(maps.tests):
        number = to_number[i]
        if number is None or test_id.startswith(("tests/test_optimizer.py", "tests/test_executor.py")):
            continue
        if ms[number] >= 20 and cpu[i] > 0:
            ours += cpu[i]
            theirs += ms[number] / 1000
    scale = min(5.0, max(0.1, theirs / ours)) if ours > 0 and theirs > 0 else 1.0
    cpu = [value * scale for value in cpu]
    setup_cost = {key: value * scale for key, value in (meta.get("class_setup") or {}).items()}
    order = {test["id"]: position for position, test in enumerate(tests)}
    policy = Policy(maps.tests, to_number, ms, cpu, setup_cost, order, unknown)
    log(f"timing scale {scale:.2f}")

    jobs = []
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

        if not result.diff_modules:
            indexes = policy.indexes(result.cands)
            if not indexes and not unknown:
                log(commit, "no candidates:", notes)
                output[commit] = []
                continue
            slices, probes = plan_slices(
                maps, policy, indexes, result.full, result.targets, (), result.no_slice
            )
            to_verify, keep, estimate, _ = policy.decide(
                indexes,
                extra_cost=0.15,
                slices=slices,
                sure=result.sure,
                probes={i: probe[2] for i, probe in probes.items()},
            )
            if not to_verify:
                numbers = [to_number[i] for i in indexes] + unknown
                cost, workers = pack(numbers, ms)
                log(commit, f"schedule {len(numbers)} ({cost / 1000:.1f}s, verify est {estimate:.1f}s):", notes)
                output[commit] = workers
                continue
        elif not (result.cands | result.coarse) and not unknown:
            log(commit, "no candidates:", notes)
            output[commit] = []
            continue
        log(commit, "verify:", notes)
        jobs.append(Job(result))

    log(f"analysis done: {time.process_time():.2f}s cpu")
    if jobs:
        run_jobs(jobs, maps, policy, output)

    for commit in commits:
        print(json.dumps({"commit": commit, "workers": output[commit]}, separators=(",", ":")))
    sys.stdout.flush()
    log(f"done in {time.monotonic() - START:.1f}s wall, {time.process_time():.2f}s own cpu")


def run_jobs(jobs, maps, policy, output):
    to_number = policy.to_number
    ms = policy.ms
    unknown = policy.unknown

    # The optimizer and executor suites need duckdb and pandas. With several jobs
    # likely to run them, import once here instead of once in every child.
    heavy = 0
    for job in jobs:
        bits = job.cands | job.coarse
        if any(
            bits >> i & 1
            for i, test_id in enumerate(maps.tests)
            if test_id.startswith((OPTIMIZER, "tests/test_executor.py"))
        ):
            heavy += 1
    if heavy >= 2:
        try:
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            from pandas.testing import assert_frame_equal  # noqa: F401
        except Exception:  # noqa: BLE001
            pass
    import concurrent.futures.process  # noqa: F401
    import unittest  # noqa: F401

    from br import basestate, cells, runner  # noqa: F401

    log(f"imports done: {time.process_time():.2f}s cpu")
    pending = list(jobs)
    running = {}
    optimizer = [
        to_number[i]
        for i, test_id in enumerate(maps.tests)
        if test_id.startswith(OPTIMIZER) and to_number[i] is not None
    ]

    def finish(job):
        if not job.announced:
            # The verifier never said what it found: assume every possible candidate.
            numbers = [to_number[i] for i in policy.indexes(job.cands | job.coarse)]
            note = "NO ANSWER"
        else:
            numbers = [to_number[i] for i in job.keep]
            numbers += [to_number[i] for i in job.verify if job.states.get(i) != "P"]
            timed_out = any(state == "T" for state in job.states.values())
            note = ""
            if timed_out or not job.done:
                note = "TIMEOUT" if timed_out else "INCOMPLETE"
                # A hang or a dead verifier in the optimizer suite stalls its shared pool.
                if any(maps.tests[i].startswith(OPTIMIZER) for i in job.verify):
                    numbers += optimizer
        numbers = sorted(set(numbers) | set(unknown))
        cost, workers = pack(numbers, ms)
        log(
            job.commit,
            f"-> {len(numbers)} tests ({cost / 1000:.1f}s) after verifying "
            f"{len(job.verify)}, keeping {len(job.keep)} {note}",
        )
        output[job.commit] = workers

    def launch(job):
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            for other in running.values():
                try:
                    os.close(other.fd)
                except OSError:
                    pass
            verify.child(ROOT, job, policy, maps, write_fd, MEMORY_BYTES, log_fd=2)
            os._exit(0)
        os.close(write_fd)
        job.pid = pid
        job.fd = read_fd
        job.deadline = time.monotonic() + 45.0
        running[read_fd] = job

    def reap(job, kill=False):
        if kill:
            try:
                os.kill(job.pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            os.close(job.fd)
        except OSError:
            pass
        try:
            os.waitpid(job.pid, 0)
        except OSError:
            pass
        shutil.rmtree(f"/tmp/br{job.pid}", ignore_errors=True)
        del running[job.fd]
        finish(job)

    while pending or running:
        out_of_time = time.monotonic() - START > WALL_BUDGET
        while pending and len(running) < PARALLEL and not out_of_time:
            launch(pending.pop(0))
        if out_of_time:
            for job in pending:
                finish(job)
                log(job.commit, "out of time: scheduled without verifying")
            pending = []
            for job in list(running.values()):
                reap(job, kill=True)
            break
        if not running:
            continue
        ready, _, _ = select.select(list(running), [], [], 0.25)
        now = time.monotonic()
        for fd in ready:
            job = running[fd]
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
                elif line == b"A":
                    job.announced = True
                    estimate = sum(policy.cpu[i] for i in job.verify)
                    job.deadline = now + 30.0 + 6.0 * estimate
                else:
                    kind, _, value = line.partition(b" ")
                    index = int(value)
                    if kind == b"K":
                        job.keep.append(index)
                    elif kind == b"V":
                        job.verify.append(index)
                    else:
                        job.states[index] = kind.decode()
        for job in list(running.values()):
            if now > job.deadline:
                log(job.commit, "verifier past its deadline")
                reap(job, kill=True)
                continue
            try:
                with open(f"/proc/{job.pid}/statm") as handle:
                    resident = int(handle.read().split()[1]) * PAGE
            except (OSError, ValueError, IndexError):
                continue
            if resident > RSS_LIMIT:
                log(job.commit, "verifier using too much memory")
                reap(job, kill=True)


if __name__ == "__main__":
    main()
