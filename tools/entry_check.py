"""Dev aid: does selecting by dict entry drop any test that then fails?

    python tools/entry_check.py <maps_dir> <set_dir> [parallel]

For each patch that changes class-level state, the tests selected by "reads the
attribute" but not by "reads a changed entry" are exactly what entry-level
tracking saves. This runs those tests on the patched tree: any failure among
them would have been a miss.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
ROOT = "/sg"

from br import analyze, runner, verify  # noqa: E402


def check(maps, result, write_fd):
    dest = f"/tmp/ec{os.getpid()}"
    verify.make_tree(ROOT, dest, result.patched)
    sys.path.insert(0, dest)
    os.chdir(dest)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    notes = []
    os.environ["BR_NO_ENTRIES"] = "1"
    wide = verify.diff_candidates(result, maps, notes.append, True)
    del os.environ["BR_NO_ENTRIES"]
    narrow = verify.diff_candidates(result, maps, notes.append, True)
    wide_bits = wide[0] | wide[1]
    narrow_bits = narrow[0] | narrow[1]
    dropped = [maps.tests[i] for i in range(len(maps.tests)) if (wide_bits & ~narrow_bits) >> i & 1]
    failed, timed_out, _ = runner.run(dropped, limits={name: 120.0 for name in dropped})
    for name in sorted(timed_out):
        again, _, _ = runner.run([name], limits={name: 900.0})
        if not again:
            failed.discard(name)
    os.write(
        write_fd,
        json.dumps(
            {
                "wide": wide_bits.bit_count(),
                "narrow": narrow_bits.bit_count(),
                "dropped": len(dropped),
                "failed": sorted(failed),
                "cells": [note for note in notes if note.startswith("cell")][-4:],
            }
        ).encode(),
    )
    import shutil

    shutil.rmtree(dest, ignore_errors=True)
    os._exit(0)


def main():
    maps = analyze.Maps(sys.argv[1])
    set_dir = sys.argv[2]
    parallel = int(sys.argv[3]) if len(sys.argv) > 3 else 3
    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401

    running = {}
    totals = {"patches": 0, "with_drops": 0, "dropped": 0, "misses": 0}

    def collect():
        pid, _ = os.wait()
        commit, fd = running.pop(pid)
        data = b""
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            data += chunk
        os.close(fd)
        try:
            record = json.loads(data.decode())
        except Exception:
            print(commit, "NO RESULT", flush=True)
            return
        totals["patches"] += 1
        totals["dropped"] += record["dropped"]
        totals["with_drops"] += bool(record["dropped"])
        if record["failed"]:
            totals["misses"] += 1
            print("ENTRY MISS", commit, record, flush=True)
        else:
            print(commit, "attribute readers", record["wide"], "-> entry readers", record["narrow"],
                  "dropped", record["dropped"], "all pass", flush=True)

    for name in sorted(os.listdir(os.path.join(set_dir, "commits"))):
        commit = name[:-6]
        text = open(os.path.join(set_dir, "commits", name), encoding="utf-8").read()
        result = analyze.analyze(commit, text, maps, ROOT)
        if result.fallback or not result.diff_modules:
            continue
        while len(running) >= parallel:
            collect()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                check(maps, result, write_fd)
            finally:
                os._exit(1)
        os.close(write_fd)
        running[pid] = (commit, read_fd)
    while running:
        collect()
    print(totals, flush=True)


if __name__ == "__main__":
    main()
