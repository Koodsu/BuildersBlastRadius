"""Merge the tracer's per-test parts into the maps the selector reads.

    python -m br.aggregate /sg /app/maps
"""

from __future__ import annotations

import os
import pickle
import sys

from br import pyindex


def main(argv):
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    parts_dir = os.path.join(out_dir, "parts")

    tests = []
    files = []
    test_file = []
    base_failed = set()
    cpu = []
    cpu_test = []
    cpu_plain = []
    class_setup = {}
    wall = []
    import_cpu = {}
    opens = {}
    eager_opens = {}
    cov = {}  # source file -> {"rt": {line: bits}, ...}
    cell_readers = {}
    key_readers = {}  # cell -> {key fingerprint: [test indexes]}
    whole_readers = {"miss": {}, "all": {}, "len": {}}  # cell -> tests
    reads_unknown = 0
    sliceable = 0
    unsliceable = []

    def slot(source):
        entry = cov.get(source)
        if entry is None:
            entry = cov[source] = {"rt": {}, "imf": {}, "imb": {}, "ef": {}, "eb": {}}
        return entry

    def add(kind, mapping, bit):
        for source, lines in mapping.items():
            target = slot(source)[kind]
            for line in lines:
                target[line] = target.get(line, 0) | bit

    missing = []
    for module_name in sorted(os.listdir(parts_dir)):
        directory = os.path.join(parts_dir, module_name)
        with open(os.path.join(directory, "_file.pkl"), "rb") as handle:
            info = pickle.load(handle)
        timing_path = os.path.join(directory, "_timing.pkl")
        plain = {}
        if os.path.exists(timing_path):
            with open(timing_path, "rb") as handle:
                timing = pickle.load(handle)
            plain = timing["timings"]
            leftover = max(0.0, timing["total"] - sum(plain.values()))
            for test_id in info["tests"]:
                class_setup.setdefault(test_id.rsplit("::", 1)[0], leftover)
        file_index = len(files)
        files.append(info["file"])
        import_cpu[info["file"]] = info["import_cpu"]
        file_bit = 1 << file_index
        add("ef", info["eager_functions"], file_bit)
        add("eb", info["eager_bodies"], file_bit)
        for path in info["eager_opens"]:
            eager_opens[path] = eager_opens.get(path, 0) | file_bit
        for index, test_id in enumerate(info["tests"]):
            test_index = len(tests)
            tests.append(test_id)
            cpu_plain.append(plain.get(test_id))
            test_file.append(file_index)
            bit = 1 << test_index
            part = os.path.join(directory, f"{index}.pkl")
            if not os.path.exists(part):
                # The traced child died: nothing is known about this test.
                missing.append(test_id)
                base_failed.add(test_index)
                reads_unknown |= bit
                cpu.append(0.0)
                cpu_test.append(0.0)
                wall.append(0.0)
                continue
            with open(part, "rb") as handle:
                data = pickle.load(handle)
            if data["failed"]:
                base_failed.add(test_index)
            cpu.append(data["cpu"])
            cpu_test.append(data.get("cpu_test", data["cpu"]))
            wall.append(data["wall"])
            add("rt", data["rt"], bit)
            add("imf", data["imf"], bit)
            add("imb", data["imb"], bit)
            for path in data["opens"]:
                opens[path] = opens.get(path, 0) | bit
            units = data.get("units")
            slice_path = os.path.join(directory, f"{index}.slice")
            if units and os.path.exists(slice_path):
                with open(slice_path) as handle:
                    mode = handle.read().strip()
                if mode in ("none", "skip"):
                    units["mode"] = mode
                    os.makedirs(os.path.join(out_dir, "units"), exist_ok=True)
                    with open(os.path.join(out_dir, "units", f"{test_index}.pkl"), "wb") as handle:
                        pickle.dump(units, handle, protocol=pickle.HIGHEST_PROTOCOL)
                    sliceable |= bit
                else:
                    unsliceable.append(test_id)
            reads = data.get("reads")
            if reads is None:
                reads_unknown |= bit
            else:
                for key in reads:
                    cell_readers[key] = cell_readers.get(key, 0) | bit
                for cell, found in (data.get("key_reads") or {}).items():
                    target = key_readers.setdefault(cell, {})
                    for fingerprint in found:
                        target.setdefault(fingerprint, []).append(test_index)
                for kind in ("miss", "all", "len"):
                    for cell in data.get(kind + "_reads") or ():
                        whole_readers[kind][cell] = whole_readers[kind].get(cell, 0) | bit

    cov_dir = os.path.join(out_dir, "cov")
    os.makedirs(cov_dir, exist_ok=True)
    sources = set(cov)
    for top in ("sqlglot", "tests"):
        for directory, _, names in os.walk(os.path.join(root, top)):
            for name in names:
                if name.endswith(".py"):
                    sources.add(os.path.relpath(os.path.join(directory, name), root))
    indexed = 0
    for source in sorted(sources):
        entry = cov.get(source) or {"rt": {}, "imf": {}, "imb": {}, "ef": {}, "eb": {}}
        path = os.path.join(root, source)
        if source.endswith(".py") and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            try:
                entry["index"] = pyindex.build_index(text, path)
                indexed += 1
            except SyntaxError:
                pass
        with open(os.path.join(cov_dir, source.replace("/", "__") + ".pkl"), "wb") as handle:
            pickle.dump(entry, handle, protocol=pickle.HIGHEST_PROTOCOL)

    meta = {
        "tests": tests,
        "files": files,
        "test_file": test_file,
        "base_failed": sorted(base_failed),
        "missing": missing,
        "cpu": cpu,
        "cpu_test": cpu_test,
        "cpu_plain": cpu_plain,
        "class_setup": class_setup,
        "wall": wall,
        "import_cpu": import_cpu,
        "opens": opens,
        "eager_opens": eager_opens,
        "reads_unknown": reads_unknown,
        "sliceable": sliceable,
    }
    with open(os.path.join(out_dir, "cells.pkl"), "wb") as handle:
        pickle.dump(cell_readers, handle, protocol=pickle.HIGHEST_PROTOCOL)
    with open(os.path.join(out_dir, "keys.pkl"), "wb") as handle:
        pickle.dump(
            {
                "key": {
                    cell: {fp: tuple(found) for fp, found in by_key.items()}
                    for cell, by_key in key_readers.items()
                },
                **whole_readers,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    with open(os.path.join(out_dir, "meta.pkl"), "wb") as handle:
        pickle.dump(meta, handle, protocol=pickle.HIGHEST_PROTOCOL)

    sys.stderr.write(
        f"aggregate: {len(tests)} tests in {len(files)} files, {len(base_failed)} failing at "
        f"base, {len(missing)} untraced, {indexed} sources indexed, cpu {sum(cpu):.1f}s, "
        f"{len(cell_readers)} cells read, {reads_unknown.bit_count()} tests without reads, "
        f"{sliceable.bit_count()} sliceable, not sliceable: {unsliceable}\n"
    )
    for test_index in sorted(base_failed):
        sys.stderr.write(f"aggregate: base failure {tests[test_index]}\n")


if __name__ == "__main__":
    main(sys.argv)
