"""Dev aid: how often does an expensive test fail when every cheaper test that
runs the same changed statement passes?

    python tools/giant_risk.py <maps_dir> <tests.json> <out.jsonl> <count> [seed] [parallel] [giant]

Mutates single lines that the giant test covers along with at least a dozen
other tests, swaps the mutant into a process that has everything imported,
runs the cheaper tests (cheapest first, stopping at the first failure) and, if
they all pass, the giant itself in full.
"""

import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
ROOT = "/sg"

from br import analyze, runner, verify  # noqa: E402
import mutate  # noqa: E402

GIANTS = {
    "tpcds": "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "maxnodes": "tests/test_parser.py::TestParser::test_max_nodes",
    "genmem": "tests/test_generator.py::TestGenerator::test_generator_memory",
}


class Job:
    pass


def main():
    maps = analyze.Maps(sys.argv[1])
    tests = json.load(open(sys.argv[2]))["tests"]
    out_path = sys.argv[3]
    count = int(sys.argv[4])
    rng = random.Random(int(sys.argv[5]) if len(sys.argv) > 5 else 7)
    parallel = int(sys.argv[6]) if len(sys.argv) > 6 else 3
    giant_id = GIANTS[sys.argv[7] if len(sys.argv) > 7 else "tpcds"]
    seconds = {test["id"]: test["seconds"] for test in tests}
    giant = maps.tests.index(giant_id)
    big = {maps.tests.index(name) for name in GIANTS.values()}

    # Lines the giant runs together with at least 12 other tests.
    spots = []
    for source in sorted(maps.state()["module_file"].values()):
        cov = maps.cov(source)
        if cov is None:
            continue
        for line, bits in cov["rt"].items():
            if bits >> giant & 1 and bits.bit_count() >= 13:
                spots.append((source, line))
    rng.shuffle(spots)
    print(len(spots), "candidate lines", flush=True)

    # Everything imported once; each mutant is swapped into a fork.
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    import unittest  # noqa: F401
    from br import basestate

    runner.install_inline_pools()
    basestate.import_all({})
    import importlib

    for rel in maps.files:
        try:
            importlib.import_module(rel[:-3].replace("/", "."))
        except Exception:
            pass
    runner.limit_duckdb_threads()
    index = verify.build_function_index(ROOT)

    running = {}
    done = 0
    texts = {}
    out = open(out_path, "a")
    for source, line in spots:
        if done >= count:
            break
        if source not in texts:
            texts[source] = open(os.path.join(ROOT, source), encoding="utf-8").read()
        old = texts[source]
        lines = old.split("\n")
        text = lines[line - 1]
        stripped = text.strip()
        if not stripped or stripped.startswith(("#", '"', "'", "import ", "from ", "@", "def ", "class ")):
            continue
        ops = mutate.CODE_OPS[:]
        if os.environ.get("GENTLE"):
            # Edits that often change nothing observable: the cases worth measuring.
            ops = [
                (r" is not ", " != "),
                (r" is ", " == "),
                (r" < ", " <= "),
                (r" <= ", " < "),
                (r" > ", " >= "),
                (r" >= ", " > "),
                (r"\bTrue\b", "False"),
                (r"\bFalse\b", "True"),
                (r" or ", " and "),
            ]
        rng.shuffle(ops)
        patch = None
        for pattern, replacement in ops:
            matches = list(re.finditer(pattern, text))
            if not matches:
                continue
            match = rng.choice(matches)
            new_piece = str(int(match.group(1)) + 1) if replacement is None else replacement
            mutated = lines[:]
            mutated[line - 1] = text[: match.start()] + new_piece + text[match.end():]
            new = "\n".join(mutated)
            try:
                compile(new, source, "exec", dont_inherit=True)
            except SyntaxError:
                continue
            patch = mutate.diff(source, old, new)
            op = f"{pattern} -> {replacement}"
            break
        if patch is None:
            continue
        result = analyze.analyze("m", patch, maps, ROOT)
        if result.fallback or not result.hot_ok or not result.hot or result.diff_modules:
            continue
        if not result.cands >> giant & 1:
            continue
        others = [i for i in range(len(maps.tests)) if result.cands >> i & 1 and i not in big]
        if len(others) < 12:
            continue
        others.sort(key=lambda i: seconds.get(maps.tests[i], 0))
        done += 1

        while len(running) >= parallel:
            finished, _ = os.wait()
            fd = running.pop(finished, None)
            if fd is not None:
                data = os.read(fd, 1 << 20)
                os.close(fd)
                out.write(data.decode())
                out.flush()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            record = {"source": source, "line": line, "op": op, "old": text.strip(), "others": len(others)}
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                job = Job()
                job.hot = result.hot
                verify.apply_hot(job, index)
                state = {"stop": False}
                passed = []

                def report(test_id, status):
                    if status != "P":
                        state["stop"] = True
                        record["first_failure"] = test_id
                        record["status"] = status
                    else:
                        passed.append(test_id)

                ids = [maps.tests[i] for i in others]
                limits = {name: 5.0 + 10 * seconds.get(name, 0) for name in ids}
                started = time.process_time()
                runner.run(ids, limits=limits, report=report, stop=state)
                record["others_cpu"] = round(time.process_time() - started, 2)
                record["others_passed"] = len(passed)
                if not state["stop"]:
                    started = time.process_time()
                    failed, timed_out, errors = runner.run([giant_id], limits={giant_id: 600})
                    record["giant_failed"] = bool(failed or errors)
                    record["giant_cpu"] = round(time.process_time() - started, 2)
            except BaseException as error:  # noqa: BLE001
                record["error"] = repr(error)
            os.write(write_fd, (json.dumps(record) + "\n").encode())
            os._exit(0)
        os.close(write_fd)
        running[pid] = read_fd
        # Collect whatever has finished.
        for other_pid, fd in list(running.items()):
            pass
        while True:
            try:
                finished, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if finished == 0:
                break
            fd = running.pop(finished, None)
            if fd is not None:
                data = os.read(fd, 1 << 20)
                os.close(fd)
                out.write(data.decode())
                out.flush()
    while running:
        finished, _ = os.wait()
        fd = running.pop(finished, None)
        if fd is not None:
            data = os.read(fd, 1 << 20)
            os.close(fd)
            out.write(data.decode())
            out.flush()


if __name__ == "__main__":
    main()
