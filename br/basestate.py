"""Build-time: fingerprint the pinned commit's import-time state and index its names.

    python -m br.basestate /sg /app/maps
"""

from __future__ import annotations

import enum
import importlib
import os
import pickle
import pkgutil
import sys

from br import cells


def canary():
    """Values that must come out the same wherever fingerprints are compared."""
    probe = compile("def f(a, b=1):\n    return [a + b, 'x', (1.5, None)]\n", "<canary>", "exec")
    from br import pyindex

    function = next(const for const in probe.co_consts if hasattr(const, "co_code"))
    return (
        hash(("blast", "radius", 1, 2.5, -1)),
        hash(frozenset({"a", "b"})),
        pyindex.own_digest(function),
        tuple(sys.version_info[:3]),
    )


def module_files(root):
    """{module name: (relative path, is_package)} for everything under root/sqlglot."""
    found = {}
    for directory, _, names in os.walk(os.path.join(root, "sqlglot")):
        for name in names:
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(directory, name), root)
            module = rel[:-3].replace("/", ".")
            is_package = module.endswith(".__init__")
            if is_package:
                module = module[: -len(".__init__")]
            found[module] = (rel, is_package)
    return found


def import_all(broken=None):
    """Import every sqlglot module, in a fixed order. Returns the names that loaded."""
    import sqlglot

    loaded = ["sqlglot"]
    infos = sorted(
        pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot.", onerror=lambda name: None),
        key=lambda info: info.name,
    )
    for info in infos:
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
            loaded.append(info.name)
        except BaseException as error:  # noqa: BLE001
            if broken is not None:
                broken[info.name] = repr(error)
    return loaded


def _probe(root, loose, path):
    """In a fresh interpreter: import everything and write its fingerprints to `path`."""
    sys.path.insert(0, root)
    os.chdir(root)
    import unittest  # noqa: F401

    loaded = import_all({})
    fingerprints, _ = cells.snapshot(loaded, loose=loose)
    with open(path, "wb") as handle:
        pickle.dump(fingerprints, handle)


def _second_opinion(root, loose, out_dir):
    """Fingerprints from another process, to find values that depend on memory layout."""
    import subprocess

    path = os.path.join(out_dir, "_probe.pkl")
    with open(path + ".in", "wb") as handle:
        pickle.dump(loose, handle)
    subprocess.check_call(
        [sys.executable, "-m", "br.basestate", "--probe", root, path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    with open(path, "rb") as handle:
        result = pickle.load(handle)
    os.remove(path)
    os.remove(path + ".in")
    return result


def main(argv):
    if os.environ.get("PYTHONHASHSEED") != "0":
        raise SystemExit("basestate needs PYTHONHASHSEED=0")
    if argv[1] == "--probe":
        with open(argv[3] + ".in", "rb") as handle:
            loose = pickle.load(handle)
        _probe(os.path.abspath(argv[2]), loose, argv[3])
        return
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    sys.path.insert(0, root)
    os.chdir(root)
    import unittest  # noqa: F401  (sqlglot checks for it at import)

    broken = {}
    loaded = import_all(broken)
    fingerprints, opaque = cells.snapshot(loaded)

    # Values built by iterating a set of classes come out in address order. Compare
    # those as bags; ignore whatever still differs between two identical processes.
    loose = set()
    ignored = set()
    for _ in range(2):
        other = _second_opinion(root, set(), out_dir)
        loose |= {key for key, value in fingerprints.items() if other.get(key) != value}
    if loose:
        fingerprints, opaque = cells.snapshot(loaded, loose=loose)
        for _ in range(2):
            other = _second_opinion(root, loose, out_dir)
            ignored |= {key for key in loose if other.get(key) != fingerprints[key]}
        loose -= ignored
        for key in ignored:
            del fingerprints[key]
    # Class attributes whose reads the tracer records (see cells.instrument).
    tracked = set()
    for name in loaded:
        for klass in cells.classes_of(sys.modules[name]):
            if isinstance(klass, enum.EnumMeta):
                continue
            for attr, value in vars(klass).items():
                if cells.trackable(attr, value):
                    tracked.add((name, klass.__qualname__, attr))

    files = module_files(root)
    known = set(files)
    deps = {}
    for module, (rel, is_package) in files.items():
        with open(os.path.join(root, rel), encoding="utf-8") as handle:
            source = handle.read()
        found = cells.imports_of(source, module, is_package, known)
        parent = module.rpartition(".")[0]
        while parent:
            if parent in known:
                found.add(parent)
            parent = parent.rpartition(".")[0]
        deps[module] = found
    # Reverse closure: who ends up importing each module.
    importers = {module: set() for module in files}
    for module, found in deps.items():
        for dep in found:
            importers[dep].add(module)
    closure = {}
    for module in files:
        seen = {module}
        stack = [module]
        while stack:
            for importer in importers[stack.pop()]:
                if importer not in seen:
                    seen.add(importer)
                    stack.append(importer)
        closure[module] = sorted(seen)

    names = {}
    for top in ("sqlglot", "tests"):
        for directory, _, entries in os.walk(os.path.join(root, top)):
            for entry in entries:
                if not entry.endswith(".py"):
                    continue
                path = os.path.join(directory, entry)
                rel = os.path.relpath(path, root)
                with open(path, encoding="utf-8") as handle:
                    code = compile(handle.read(), path, "exec", dont_inherit=True)
                for name, lines in cells.name_lines(code).items():
                    names.setdefault(name, {})[rel] = sorted(lines)

    with open(os.path.join(out_dir, "state.pkl"), "wb") as handle:
        pickle.dump(
            {
                "fingerprints": fingerprints,
                "loose": loose,
                "ignored": ignored,
                "tracked": tracked,
                "opaque": opaque,
                "loaded": loaded,
                "broken": broken,
                "closure": closure,
                "module_file": {module: rel for module, (rel, _) in files.items()},
                "canary": canary(),
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    with open(os.path.join(out_dir, "names.pkl"), "wb") as handle:
        pickle.dump(names, handle, protocol=pickle.HIGHEST_PROTOCOL)
    sys.stderr.write(
        f"basestate: {len(loaded)} modules, {len(fingerprints)} cells, {opaque} opaque values, "
        f"{len(broken)} modules that do not import, {len(names)} names, "
        f"{len(loose)} order-free cells, {len(ignored)} ignored: {sorted(ignored)[:5]}\n"
    )
    for module, error in broken.items():
        sys.stderr.write(f"basestate: cannot import {module}: {error}\n")


if __name__ == "__main__":
    main(sys.argv)
