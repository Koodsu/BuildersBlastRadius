"""Build-time: which sqlglot lines each test class's setUpClass runs.

    python -m br.setups /sg /app/maps

A test that passes without its class's setUpClass is run without it. That is
only right while the change cannot reach the setup itself: a setup the patch
breaks fails every test of its class. This records, per class with a
setUpClass, the lines it executes, so the verifier knows when to keep it.
Writes setups.pkl: {"tests/test_x.py::Class": {"lines": {source: [lines]},
"reads": {class attributes it reads}}}, or None for a setup that could not be
watched.
"""

from __future__ import annotations

import importlib
import os
import pickle
import sys
import unittest


def _one(root, rel_path, out_path):
    from br import basestate, cells, runner, tracer

    tracer.SG = root
    tracer.SGP = root + "/"
    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    tracer._install()
    basestate.import_all({})
    module = importlib.import_module(rel_path[:-3].replace("/", "."))
    tracer.CELLS_ON = True
    cells.instrument_loaded()
    found = {}
    for name, klass in vars(module).items():
        if not (isinstance(klass, type) and issubclass(klass, unittest.TestCase)):
            continue
        if klass.__module__ != module.__name__ or "setUpClass" not in klass.__dict__:
            continue
        tracer.RT.clear()
        tracer.IM.clear()
        cells.READS.clear()
        cells.rearm()
        tracer.MON.restart_events()
        lines = {}
        try:
            klass.setUpClass()
            try:
                klass.tearDownClass()
                klass.doClassCleanups()
            except Exception:  # noqa: BLE001
                pass
        except BaseException:  # noqa: BLE001
            lines = None  # could not be watched: treat the setup as reaching everything
        if lines is not None:
            for mapping in (tracer.RT, tracer.IM):
                for cid, hit in mapping.items():
                    source = tracer.CODES[cid].co_filename[len(tracer.SGP) :]
                    if source.startswith("sqlglot/") and hit:
                        lines.setdefault(source, set()).update(hit)
            lines = {
                "lines": {source: sorted(hit) for source, hit in lines.items()},
                "reads": set(cells.READS),
            }
        found[f"{rel_path}::{name}"] = lines
    with open(out_path, "wb") as handle:
        pickle.dump(found, handle)


def main(argv):
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, root)
    os.chdir(root)
    files = []
    for directory, _, names in os.walk(os.path.join(root, "tests")):
        for name in names:
            if name.startswith("test_") and name.endswith(".py"):
                path = os.path.join(directory, name)
                with open(path, encoding="utf-8") as handle:
                    if "setUpClass" in handle.read():
                        files.append(os.path.relpath(path, root))
    found = {}
    for rel_path in sorted(files):
        out_path = os.path.join(out_dir, "_setup_part.pkl")
        pid = os.fork()
        if pid == 0:
            status = 1
            try:
                _one(root, rel_path, out_path)
                status = 0
            except BaseException as error:  # noqa: BLE001
                sys.stderr.write(f"setups: {rel_path} failed: {error!r}\n")
            finally:
                os._exit(status)
        _, status = os.waitpid(pid, 0)
        if status == 0 and os.path.exists(out_path):
            with open(out_path, "rb") as handle:
                found.update(pickle.load(handle))
            os.remove(out_path)
    with open(os.path.join(out_dir, "setups.pkl"), "wb") as handle:
        pickle.dump(found, handle, protocol=pickle.HIGHEST_PROTOCOL)
    sys.stderr.write(
        "setups: "
        + ", ".join(
            f"{key.split('::')[-1]} "
            f"{'unwatched' if value is None else sum(len(v) for v in value['lines'].values())}"
            f"{'' if value is None else '/' + str(len(value['reads']))}"
            for key, value in sorted(found.items())
        )
        + "\n"
    )


if __name__ == "__main__":
    main(sys.argv)
