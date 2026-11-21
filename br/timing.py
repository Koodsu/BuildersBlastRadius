"""Build-time: how long each test takes in the in-process runner, untraced.

    python -m br.timing /sg /app/maps [jobs]

Run on a quiet machine: these numbers decide when verifying beats scheduling.
Rewrites cpu_plain and class_setup in meta.pkl.
"""

from __future__ import annotations

import os
import pickle
import subprocess
import sys
import time


def _one(root, rel_path, out_path):
    import importlib

    sys.path.insert(0, root)
    os.chdir(root)
    from br import runner, tracer

    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    module = importlib.import_module(rel_path[:-3].replace("/", "."))
    by_class = {}
    for cls, method in tracer._discover(module):
        by_class.setdefault(cls, []).append(f"{rel_path}::{cls}::{method}")
    timings = {}
    setup = {}
    for cls, ids in by_class.items():
        started = time.process_time()
        runner.run(ids, timings=timings)
        total = time.process_time() - started
        setup[f"{rel_path}::{cls}"] = max(0.0, total - sum(timings.get(i, 0.0) for i in ids))
    with open(out_path, "wb") as handle:
        pickle.dump({"timings": timings, "setup": setup}, handle)


def _bare(root, rel_path, out_path):
    """Which tests of classes with a setUpClass pass without it."""
    import importlib

    sys.path.insert(0, root)
    os.chdir(root)
    from br import runner, tracer

    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    module = importlib.import_module(rel_path[:-3].replace("/", "."))
    free = []
    for cls, method in tracer._discover(module):
        klass = getattr(module, cls)
        if "setUpClass" not in klass.__dict__:
            continue
        test_id = f"{rel_path}::{cls}::{method}"
        failed, _, errors = runner.run([test_id], bare={test_id}, limits={test_id: 60.0})
        if not failed and not errors:
            free.append(test_id)
    with open(out_path, "wb") as handle:
        pickle.dump(free, handle)


def main(argv):
    if argv[1] == "--one":
        _one(argv[2], argv[3], argv[4])
        return
    if argv[1] == "--bare":
        _bare(argv[2], argv[3], argv[4])
        return
    if argv[1] == "--bare-all":
        root = os.path.abspath(argv[2])
        out_dir = os.path.abspath(argv[3])
        with open(os.path.join(out_dir, "meta.pkl"), "rb") as handle:
            meta = pickle.load(handle)
        free = []
        for index, rel_path in enumerate(meta["files"]):
            path = os.path.join(out_dir, f"_bare_{index}.pkl")
            subprocess.call(
                [sys.executable, "-m", "br.timing", "--bare", root, rel_path, path],
                env=dict(os.environ, SKIP_INTEGRATION="0"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if os.path.exists(path):
                with open(path, "rb") as handle:
                    free += pickle.load(handle)
                os.remove(path)
        meta["no_setup"] = sorted(free)
        with open(os.path.join(out_dir, "meta.pkl"), "wb") as handle:
            pickle.dump(meta, handle, protocol=pickle.HIGHEST_PROTOCOL)
        counts = {}
        for test_id in free:
            counts[test_id.split("::")[1]] = counts.get(test_id.split("::")[1], 0) + 1
        sys.stderr.write(f"timing: tests that need no class setup: {counts}\n")
        return
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    jobs = int(argv[3]) if len(argv) > 3 else 2
    with open(os.path.join(out_dir, "meta.pkl"), "rb") as handle:
        meta = pickle.load(handle)
    env = dict(os.environ, SKIP_INTEGRATION="0")
    running = []
    results = []
    pending = list(meta["files"])

    def reap():
        process, path = running.pop(0)
        process.wait()
        results.append(path)

    for index, rel_path in enumerate(pending):
        path = os.path.join(out_dir, f"_timing_{index}.pkl")
        running.append(
            (
                subprocess.Popen(
                    [sys.executable, "-m", "br.timing", "--one", root, rel_path, path],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                ),
                path,
            )
        )
        if len(running) >= jobs:
            reap()
    while running:
        reap()

    timings = {}
    setup = {}
    for path in results:
        if not os.path.exists(path):
            continue
        with open(path, "rb") as handle:
            data = pickle.load(handle)
        timings.update(data["timings"])
        setup.update(data["setup"])
        os.remove(path)
    meta["cpu_plain"] = [timings.get(test_id) for test_id in meta["tests"]]
    meta["class_setup"] = setup
    with open(os.path.join(out_dir, "meta.pkl"), "wb") as handle:
        pickle.dump(meta, handle, protocol=pickle.HIGHEST_PROTOCOL)
    missing = sum(1 for value in meta["cpu_plain"] if value is None)
    sys.stderr.write(
        f"timing: {len(timings)} tests, {missing} missing, total "
        f"{sum(timings.values()):.1f}s, class setup {sum(setup.values()):.1f}s\n"
    )


if __name__ == "__main__":
    main(sys.argv)
