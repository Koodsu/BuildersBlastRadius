"""Dev aid: label practice patches by running the real suite on each.

    python tools/groundtruth.py <sqlglot_root> <set_dir> <tests.json> [parallel]

Reads <set_dir>/commits/*.patch, writes <set_dir>/answers.json in the kit's shape.
"""

import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from br import patch as patchlib  # noqa: E402


def label(root, set_dir, commit, tests):
    work = f"/tmp/gt/{commit}"
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(root, work, symlinks=True, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    text = open(os.path.join(set_dir, "commits", commit + ".patch"), encoding="utf-8").read()
    for file_patch in patchlib.parse(text):
        path = os.path.join(work, file_patch.path)
        with open(path, encoding="utf-8", newline="") as handle:
            base = handle.read()
        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write(patchlib.apply(base, file_patch))
    out = f"/tmp/gt/{commit}.json"
    env = dict(os.environ, GT_OUT=out, PYTHONPATH=f"{work}:{HERE}", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONHASHSEED", None)
    return subprocess.Popen(
        [sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider", "-p", "gt_plugin",
         "--timeout", "1500", "--tb=no", "-x" if False else "-q"],
        cwd=work, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    ), out, work


def main():
    root, set_dir, tests_path = sys.argv[1], sys.argv[2], sys.argv[3]
    parallel = int(sys.argv[4]) if len(sys.argv) > 4 else 3
    tests = json.load(open(tests_path))["tests"]
    number = {test["id"]: test["number"] for test in tests}
    by_file = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test["number"])
    kinds = json.load(open(os.path.join(set_dir, "kinds.json")))
    answers_path = os.path.join(set_dir, "answers.json")
    answers = json.load(open(answers_path))["commits"] if os.path.exists(answers_path) else {}
    todo = [c for c in sorted(kinds) if c not in answers]
    running = []
    os.makedirs("/tmp/gt", exist_ok=True)

    def collect(entry):
        commit, process, out, work = entry
        process.wait()
        failing = set()
        try:
            data = json.load(open(out))
            for test_id in data["failed"]:
                if test_id in number:
                    failing.add(number[test_id])
            for file in data["broken_files"]:
                failing.update(by_file.get(file, []))
        except Exception as error:  # noqa: BLE001
            print(commit, "no result:", error, flush=True)
            failing = set(number.values())
        answers[commit] = {"failing": sorted(failing), "kind": kinds[commit]}
        shutil.rmtree(work, ignore_errors=True)
        with open(answers_path, "w") as handle:
            json.dump({"commits": answers}, handle, indent=1)
        print(commit, kinds[commit], len(failing), flush=True)

    for commit in todo:
        process, out, work = label(root, set_dir, commit, tests)
        running.append((commit, process, out, work))
        if len(running) >= parallel:
            collect(running.pop(0))
    while running:
        collect(running.pop(0))


if __name__ == "__main__":
    main()
