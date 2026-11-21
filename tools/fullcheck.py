"""Dev aid: label practice patches with the in-process runner instead of pytest.

    python tools/fullcheck.py <set_dir> <tests.json> [parallel] [kinds]

For every patch, imports the patched tree in a fresh process and runs every
test except the two that take half a minute each (assumed to pass), then writes
<set_dir>/answers.json. Much faster than tools/groundtruth.py; it checks that
the selector's candidates are not missing anything, not that the runner agrees
with pytest.
"""

import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
ROOT = "/sg"
SKIP = (
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
)

from br import patch as patchlib  # noqa: E402
from br import runner, verify  # noqa: E402


def label(set_dir, commit, tests, write_fd):
    text = open(os.path.join(set_dir, "commits", commit + ".patch"), encoding="utf-8").read()
    patched = {}
    for file_patch in patchlib.parse(text):
        with open(os.path.join(ROOT, file_patch.path), encoding="utf-8", newline="") as handle:
            patched[file_patch.path] = patchlib.apply(handle.read(), file_patch)
    dest = f"/tmp/fc{os.getpid()}"
    verify.make_tree(ROOT, dest, patched)
    sys.path.insert(0, dest)
    os.chdir(dest)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    ids = [test["id"] for test in tests if test["id"] not in SKIP]
    number = {test["id"]: test["number"] for test in tests}
    limits = {test["id"]: 20.0 + 20 * test["seconds"] for test in tests}
    failed, timed_out, errors = runner.run(ids, limits=limits)
    # A timeout may only mean the machine stalled: such tests get a second, patient try.
    for test_id in sorted(timed_out):
        again, _, _ = runner.run([test_id], limits={test_id: 600.0})
        if not again:
            failed.discard(test_id)
    os.write(write_fd, json.dumps(sorted(number[i] for i in failed)).encode())
    import shutil

    shutil.rmtree(dest, ignore_errors=True)
    os._exit(0)


def main():
    set_dir, tests_path = sys.argv[1], sys.argv[2]
    parallel = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    kinds_wanted = set(sys.argv[4].split(",")) if len(sys.argv) > 4 else None
    tests = json.load(open(tests_path))["tests"]
    kinds = json.load(open(os.path.join(set_dir, "kinds.json")))
    answers_path = os.path.join(set_dir, "answers.json")
    answers = {}
    if os.path.exists(answers_path):
        answers = {
            commit: value
            for commit, value in json.load(open(answers_path))["commits"].items()
            if value.get("checked")
        }
    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401

    todo = [c for c in sorted(kinds) if c not in answers and (not kinds_wanted or kinds[c] in kinds_wanted)]
    running = {}

    def collect():
        pid, status = os.wait()
        commit, fd, started = running.pop(pid)
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
        os.close(fd)
        try:
            failing = json.loads(data.decode())
        except Exception:
            failing = None
        if failing is None:
            print(commit, kinds[commit], "NO RESULT", status, flush=True)
            return
        answers[commit] = {"failing": failing, "kind": kinds[commit], "checked": True}
        full = {c: {"failing": [], "kind": k} for c, k in kinds.items()}
        full.update(answers)
        with open(answers_path, "w") as handle:
            json.dump({"commits": full}, handle, indent=1)
        print(commit, kinds[commit], len(failing), f"{time.time() - started:.0f}s", flush=True)

    for commit in todo:
        while len(running) >= parallel:
            collect()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                label(set_dir, commit, tests, write_fd)
            finally:
                os._exit(1)
        os.close(write_fd)
        running[pid] = (commit, read_fd, time.time())
    while running:
        collect()


if __name__ == "__main__":
    main()
