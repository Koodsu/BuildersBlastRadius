"""Build-time: which tests load duckdb, numpy or pandas.

    python -m br.heavy /sg /app/maps [jobs]

The optimizer and executor test modules import all three and few of their
tests use them, so the verifier puts stand-ins in their place (see
runner.install_lazy_heavy). This records, for every test of a module that
imports one, which of them it makes real, run the way the verifier runs it:
alone, and without its class's setUpClass when it is known not to need it.
Writes heavy.pkl: {test id: [module names]}.
"""

from __future__ import annotations

import importlib
import json
import os
import pickle
import sys

NAMES = ("numpy", "pandas", "duckdb")


def _users(root):
    """Test files whose source imports one of the heavy modules."""
    found = []
    for directory, _, names in os.walk(os.path.join(root, "tests")):
        for name in names:
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            path = os.path.join(directory, name)
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            if any(f"import {module}" in text or f"from {module}" in text for module in NAMES):
                found.append(os.path.relpath(path, root))
    return sorted(found)


def main(argv):
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    jobs = int(argv[3]) if len(argv) > 3 else 2
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, root)
    os.chdir(root)
    with open(os.path.join(out_dir, "meta.pkl"), "rb") as handle:
        meta = pickle.load(handle)
    bare = set(meta.get("no_setup", ()))
    files = set(_users(root))

    from br import runner

    runner.install_inline_pools()
    runner.install_lazy_heavy()
    runner.limit_duckdb_threads()
    for rel in sorted(files):
        importlib.import_module(rel[:-3].replace("/", "."))
    todo = [test_id for test_id in meta["tests"] if test_id.split("::", 1)[0] in files]
    found = {}
    running = {}

    def collect():
        pid, _ = os.wait()
        test_id, read_fd = running.pop(pid)
        data = b""
        while True:
            chunk = os.read(read_fd, 65536)
            if not chunk:
                break
            data += chunk
        os.close(read_fd)
        try:
            found[test_id] = json.loads(data)
        except ValueError:
            found[test_id] = list(NAMES)  # it died: assume it needs everything

    for test_id in todo:
        while len(running) >= jobs:
            collect()
        read_fd, write_fd = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, 1)
                os.dup2(devnull, 2)
                runner.run([test_id], bare=bare, limits={test_id: 600.0})
                real = [name for name in NAMES if name not in runner._LAZY]
                os.write(write_fd, json.dumps(real).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        running[pid] = (test_id, read_fd)
    while running:
        collect()
    with open(os.path.join(out_dir, "heavy.pkl"), "wb") as handle:
        pickle.dump({"files": sorted(files), "tests": found}, handle, protocol=pickle.HIGHEST_PROTOCOL)
    counts = {name: sum(1 for value in found.values() if name in value) for name in NAMES}
    sys.stderr.write(f"heavy: {len(found)} tests in {len(files)} files; they load {counts}\n")


if __name__ == "__main__":
    main(sys.argv)
