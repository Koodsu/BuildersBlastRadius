"""Offline: trace every test alone at the pinned commit. Run inside the image.

    python tools/trace.py trace <tests.json> <out_dir> <shard> <shards>
    python tools/trace.py time  <tests.json> <out_dir>
    python tools/trace.py build <tests.json> <out_dir> <maps_dir>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def load_ids(path):
    return [t["id"] for t in json.load(open(path))["tests"]]


def warm():
    import unittest  # noqa
    import duckdb  # noqa
    import numpy  # noqa
    import pandas  # noqa


def trace(tests_json, out_dir, shard, shards):
    from brx import harness

    warm()
    ids = load_ids(tests_json)
    with open(os.path.join(out_dir, f"trace{shard}.jsonl"), "w") as out:
        for index, test_id in enumerate(ids):
            if index % shards != shard:
                continue
            results, traced, status = harness.fork_run(
                harness.SG, [test_id], lambda _: 900.0, stall_seconds=900.0, trace=True
            )
            ok, cpu = results.get(test_id, (False, 0.0, None))[:2]
            lines, opened = traced or ({}, [])
            out.write(json.dumps({"id": test_id, "ok": ok, "status": status, "lines": lines, "opened": opened}) + "\n")
            out.flush()
            if not ok:
                sys.stderr.write(f"NOT OK at pinned: {test_id} ({status})\n")


def timing(tests_json, out_dir):
    from brx import harness

    warm()
    ids = load_ids(tests_json)
    by_file = {}
    for test_id in ids:
        by_file.setdefault(test_id.split("::")[0], []).append(test_id)
    est = {}
    for file, group in by_file.items():
        results, _, status = harness.fork_run(harness.SG, group, lambda _: 900.0, stall_seconds=900.0)
        for test_id in group:
            ok, cpu = results.get(test_id, (False, 0.0, None))[:2]
            est[test_id] = cpu
            if not ok:
                sys.stderr.write(f"NOT OK in file run: {test_id} ({status})\n")
    json.dump(est, open(os.path.join(out_dir, "time.json"), "w"))


def build(tests_json, out_dir, maps_dir):
    ids = load_ids(tests_json)
    number = {test_id: i for i, test_id in enumerate(ids)}
    by_line = {}
    by_open = {}
    bad = []
    seen = 0
    for name in sorted(os.listdir(out_dir)):
        if not name.startswith("trace"):
            continue
        for row in open(os.path.join(out_dir, name)):
            row = json.loads(row)
            seen += 1
            bit = 1 << number[row["id"]]
            if not row["ok"]:
                bad.append(row["id"])
            for file, lines in row["lines"].items():
                entry = by_line.setdefault(file, {})
                for line in lines:
                    entry[line] = entry.get(line, 0) | bit
            for path in row["opened"]:
                if path.endswith(".pyc") or "__pycache__" in path:
                    continue
                by_open[path] = by_open.get(path, 0) | bit
    assert seen == len(ids), (seen, len(ids))
    width = (len(ids) + 7) // 8
    set_ids = {}
    blobs = []

    def set_id(mask):
        found = set_ids.get(mask)
        if found is None:
            found = set_ids[mask] = len(blobs)
            blobs.append(mask.to_bytes(width, "little"))
        return found

    files = {}
    for file, entry in sorted(by_line.items()):
        lines = sorted(entry)
        files[file] = [lines, [set_id(entry[line]) for line in lines]]
    opened = {path: set_id(mask) for path, mask in sorted(by_open.items())}
    est = json.load(open(os.path.join(out_dir, "time.json")))
    os.makedirs(maps_dir, exist_ok=True)
    with open(os.path.join(maps_dir, "sets.bin"), "wb") as out:
        out.write(b"".join(blobs))
    json.dump(
        {"tests": ids, "width": width, "files": files, "opened": opened, "est": [round(est.get(i, 0.0), 5) for i in ids], "bad": bad},
        open(os.path.join(maps_dir, "index.json"), "w"),
        separators=(",", ":"),
    )
    print(f"{len(ids)} tests, {len(files)} files, {sum(len(v[0]) for v in files.values())} lines, {len(blobs)} sets, {len(bad)} not ok at pinned: {bad[:20]}")


def probes(maps_dir):
    """Fingerprints at the pinned commit, and the lines the TPC probes cannot vouch for."""
    from brx import harness
    from brx import probes as probe_module

    warm()
    names = list(probe_module.PROBES)
    results, _, status = harness.fork_run(harness.SG, [], lambda _: 900.0, stall_seconds=900.0, probes=names)
    again, _, _ = harness.fork_run(harness.SG, [], lambda _: 900.0, stall_seconds=900.0, probes=names)
    assert status == "done"
    for name in names:
        assert results[name][2] == again[name][2], name
        print(name, round(results[name][1], 3), "s", len(results[name][2]), "prints")
    # Lines that run at import, or inside the execution the TPC probes cut off.
    pid_out = os.path.join(maps_dir, "probes.json")
    read_fd, write_fd = os.pipe()
    if os.fork() == 0:
        os.close(read_fd)
        import importlib

        harness.enter(harness.SG)
        mon = sys.monitoring
        tool = mon.COVERAGE_ID
        mon.use_tool_id(tool, "br")
        sets = {"import": set(), "front": set(), "back": set()}
        state = ["import"]
        prefix = harness.SG + "/"

        def on_line(code, line):
            if code.co_filename.startswith(prefix):
                sets[state[0]].add((code.co_filename[len(prefix):], line))
            return mon.DISABLE

        def switch(to):
            state[0] = to
            mon.restart_events()

        mon.register_callback(tool, mon.events.LINE, on_line)
        mon.set_events(tool, mon.events.LINE)
        importlib.import_module("tests.test_executor")
        from sqlglot.executor.python import PythonExecutor

        original = PythonExecutor.execute

        def wrapped(self, plan):
            switch("back")
            try:
                return original(self, plan)
            finally:
                switch("front")

        PythonExecutor.execute = wrapped
        switch("front")
        done = []
        harness.run_tests(harness.SG, [n for n in names if "execute_tpc" in n], lambda *a: done.append(a), lambda _: 3000.0)
        mon.set_events(tool, 0)
        unsafe = {}
        for file, line in sets["import"] | sets["back"]:
            unsafe.setdefault(file, []).append(line)
        with os.fdopen(write_fd, "w") as out:
            json.dump({"ok": [d[:2] for d in done], "unsafe": {f: sorted(v) for f, v in unsafe.items()},
                       "counts": {k: len(v) for k, v in sets.items()}}, out)
        os._exit(0)
    os.close(write_fd)
    traced = json.load(os.fdopen(read_fd))
    os.wait()
    print(traced["ok"], traced["counts"])
    assert all(ok for _, ok in traced["ok"]) and len(traced["ok"]) == 2
    json.dump({"expected": {name: results[name][2] for name in names}, "unsafe": traced["unsafe"],
               "cpu": {name: round(results[name][1], 3) for name in names}},
              open(pid_out, "w"), separators=(",", ":"))


if __name__ == "__main__":
    if sys.argv[1] == "probes":
        probes(sys.argv[2])
        sys.exit(0)
    mode = sys.argv[1]
    if mode == "trace":
        trace(sys.argv[2], sys.argv[3], int(sys.argv[4]), int(sys.argv[5]))
    elif mode == "time":
        timing(sys.argv[2], sys.argv[3])
    else:
        build(sys.argv[2], sys.argv[3], sys.argv[4])
