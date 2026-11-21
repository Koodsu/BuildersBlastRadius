"""Build-time: which tests actually depend on what a fixture file says.

    python -m br.fixtures /sg /app/maps

A test class may load a fixture in setUpClass for the benefit of one or two of
its tests; every test of the class then counts as having opened the file. Here
each fixture is spoiled in turn (every statement made unparseable) and each
test that opens it is run: the ones that still pass do not depend on it.
Writes fixture_users into meta.pkl.
"""

from __future__ import annotations

import os
import pickle
import shutil
import subprocess
import sys


def spoil(text):
    """Break every statement, keeping the number of lines and statements."""
    out = []
    for line in text.split("\n"):
        if line.strip() and not line.startswith(("--", "#")):
            line = line.replace(";", " ) zz ( ;") if ";" in line else line + " ) zz ("
        out.append(line)
    return "\n".join(out)


def _one(root, dest, tests_path, out_path):
    sys.path.insert(0, dest)
    os.chdir(dest)
    from br import runner

    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    with open(tests_path, "rb") as handle:
        test_ids = pickle.load(handle)
    failed, _, errors = runner.run(test_ids, limits={test_id: 1500.0 for test_id in test_ids})
    with open(out_path, "wb") as handle:
        pickle.dump(sorted(failed), handle)


def main(argv):
    if argv[1] == "--one":
        _one(argv[2], argv[3], argv[4], argv[5])
        return
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    from br import verify

    with open(os.path.join(out_dir, "meta.pkl"), "rb") as handle:
        meta = pickle.load(handle)
    tests = meta["tests"]
    file_tests = {}
    for index, file_index in enumerate(meta["test_file"]):
        file_tests.setdefault(file_index, []).append(index)
    users = {}
    for path, bits in sorted(meta["opens"].items()):
        if not (path.startswith("tests/fixtures/") and path.endswith(".sql")):
            continue
        openers = [i for i in range(len(tests)) if bits >> i & 1]
        file_bits = meta["eager_opens"].get(path, 0)
        for file_index, members in file_tests.items():
            if file_bits >> file_index & 1:
                openers += members
        openers = sorted(set(openers))
        if len(openers) < 3:
            continue  # nothing to narrow down
        with open(os.path.join(root, path), encoding="utf-8", newline="") as handle:
            text = handle.read()
        dest = f"/tmp/fx{os.getpid()}"
        shutil.rmtree(dest, ignore_errors=True)
        verify.make_tree(root, dest, {path: spoil(text)})
        tests_path = os.path.join(out_dir, "_fixture_tests.pkl")
        result_path = os.path.join(out_dir, "_fixture_result.pkl")
        with open(tests_path, "wb") as handle:
            pickle.dump([tests[i] for i in openers], handle)
        if os.path.exists(result_path):
            os.remove(result_path)
        subprocess.call(
            [sys.executable, "-m", "br.fixtures", "--one", root, dest, tests_path, result_path],
            env=dict(os.environ, SKIP_INTEGRATION="0"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(dest, ignore_errors=True)
        if not os.path.exists(result_path):
            continue  # could not tell: every opener stays a candidate
        with open(result_path, "rb") as handle:
            failed = set(pickle.load(handle))
        os.remove(result_path)
        os.remove(tests_path)
        users[path] = sum(1 << i for i in openers if tests[i] in failed)
        sys.stderr.write(
            f"fixtures: {path}: {len(failed)} of {len(openers)} openers depend on it: "
            f"{sorted(t.split('::')[-1] for t in failed)[:6]}\n"
        )
    meta["fixture_users"] = users
    with open(os.path.join(out_dir, "meta.pkl"), "wb") as handle:
        pickle.dump(meta, handle, protocol=pickle.HIGHEST_PROTOCOL)


if __name__ == "__main__":
    main(sys.argv)
