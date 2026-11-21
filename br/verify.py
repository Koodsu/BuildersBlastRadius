"""Run candidate tests against a patched copy of the tree, in a forked child.

The child talks to the selector over a pipe, one line per fact, with tests
named by their build-order index:

    K <i>   scheduled without being run
    V <i>   will be run here
    A       the two lists above are complete
    P/F/T <i>   passed / failed / timed out
    D       finished
"""

from __future__ import annotations

import os
import pickle
import resource
import shutil
import sys
import time

from br import analyze, cells
from br.policy import plan_slices


def make_tree(base, dest, patched):
    """A view of `base` at `dest` where the patched files have their new text.

    Directories on the way to a patched file are real; everything else is a
    symlink into the read-only base, so this costs a few dozen syscalls.
    """
    real = {""}
    for rel in patched:
        parts = rel.split("/")[:-1]
        for depth in range(1, len(parts) + 1):
            real.add("/".join(parts[:depth]))
    for directory in sorted(real, key=len):
        source = os.path.join(base, directory) if directory else base
        target = os.path.join(dest, directory) if directory else dest
        os.mkdir(target)
        here = {os.path.basename(rel) for rel in patched if os.path.dirname(rel) == directory}
        try:
            entries = os.listdir(source)
        except OSError:
            entries = []
        for entry in entries:
            rel = f"{directory}/{entry}" if directory else entry
            if rel in real or entry in here:
                continue
            if entry == "__pycache__":
                modules = {name[:-3] for name in here if name.endswith(".py")}
                if modules:
                    cache = os.path.join(target, entry)
                    os.mkdir(cache)
                    for compiled in os.listdir(os.path.join(source, entry)):
                        if compiled.split(".", 1)[0] not in modules:
                            os.symlink(
                                os.path.join(source, entry, compiled),
                                os.path.join(cache, compiled),
                            )
                    continue
            os.symlink(os.path.join(source, entry), os.path.join(target, entry))
    for rel, text in patched.items():
        if text is None:
            continue
        with open(os.path.join(dest, rel), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)


def _name_readers(maps, names, attr):
    """Tests that run a line reading `attr` by name, anywhere."""
    bits = 0
    file_bits = 0
    for rel, lines in names.get(attr, {}).items():
        cov = maps.cov(rel)
        if cov is None:
            continue
        for line in lines:
            bits |= cov["rt"].get(line, 0) | cov["imf"].get(line, 0)
            file_bits |= cov["ef"].get(line, 0)
    return bits | maps.files_to_tests(file_bits)


def diff_candidates(job, maps, log):
    """Import the patched tree and return the tests that read state it changed."""
    from br import basestate

    with open(os.path.join(maps.dir, "state.pkl"), "rb") as handle:
        state = pickle.load(handle)
    if state.get("canary") != basestate.canary():
        raise RuntimeError("fingerprints were made by a different interpreter")
    base = state["fingerprints"]
    tracked = state["tracked"]
    scope = set()
    for module in job.diff_modules:
        scope.update(state["closure"].get(module, [module]))

    broken = {}
    loaded = basestate.import_all(broken)
    bits = 0  # readers of changed cells: these can be run in slices
    full = 0  # candidates found any other way
    targets = set()
    for module in broken:
        if module not in state["broken"]:
            rel = state["module_file"].get(module)
            full |= analyze.users_of(maps, rel) if rel else maps.all_bits
            log(f"{module} no longer imports")

    current, _ = cells.snapshot(
        [name for name in loaded if name in scope], job.translate, loose=state["loose"]
    )
    ignored = state["ignored"]
    changed = [key for key, value in base.items() if key[0] in scope and current.get(key) != value]
    changed += [key for key in current if key not in base and key not in ignored]
    # The statements that were edited name the cells they assign: count those as
    # changed whether or not the fingerprints agree.
    seen = set(changed)
    changed += [key for key in job.forced if key not in seen]
    if not changed:
        if job.unnamed:
            log("class-level change with no visible effect: assuming the worst")
            full |= job.coarse
        return bits, full, targets

    with open(os.path.join(maps.dir, "cells.pkl"), "rb") as handle:
        readers = pickle.load(handle)
    names = None
    full |= maps.meta.get("reads_unknown", 0)
    shown = 0
    for key in changed:
        module, qualname, attr = key
        found = None
        tracked_key = None
        if qualname is not None and attr == "__bases__":
            found = job.coarse
        elif qualname is not None and not (attr.startswith("__") and attr.endswith("__")):
            owner = sys.modules.get(module)
            for part in qualname.split("."):
                owner = getattr(owner, part, None) if owner is not None else None
            if key in base:
                value = vars(owner).get(attr) if isinstance(owner, type) else None
                gone = not isinstance(owner, type) or attr not in vars(owner)
                if key in tracked and (gone or cells.trackable(attr, value)):
                    found = readers.get(key, 0)
                    tracked_key = key
            elif isinstance(owner, type):
                # A new attribute hides the one its bases provided.
                for parent in owner.__mro__[1:]:
                    parent_key = (parent.__module__, parent.__qualname__, attr)
                    if parent_key in tracked:
                        found = (found or 0) | readers.get(parent_key, 0)
                        tracked_key = parent_key
                        break
        if found is None:
            if attr.startswith("__") and attr.endswith("__"):
                found = job.coarse
            else:
                if names is None:
                    with open(os.path.join(maps.dir, "names.pkl"), "rb") as handle:
                        names = pickle.load(handle)
                found = _name_readers(maps, names, attr)
        if shown < 12:
            log(f"cell {module}:{qualname}.{attr} read by {found.bit_count()}")
            shown += 1
        if tracked_key is not None:
            bits |= found
            targets.add(tracked_key)
        else:
            full |= found
    if len(changed) > shown:
        log(f"{len(changed)} cells changed in all")
    return bits, full, targets


def child(base, job, policy, maps, write_fd, memory_bytes, log_fd=None):
    """Body of the forked verifier."""
    dest = f"/tmp/br{os.getpid()}"
    started = time.process_time()

    def log(text):
        if log_fd is not None:
            try:
                os.write(log_fd, f"{job.commit}   {text}\n".encode())
            except OSError:
                pass

    try:
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass
        if log_fd is not None:
            log_fd = os.dup(log_fd)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        make_tree(base, dest, job.patched)
        sys.path.insert(0, dest)
        os.chdir(dest)
        sys.dont_write_bytecode = True

        from br import runner

        runner.install_inline_pools()
        runner.limit_duckdb_threads()

        bits = job.cands
        full = job.full
        cell_targets = set()
        if job.diff_modules:
            try:
                found, found_full, cell_targets = diff_candidates(job, maps, log)
                bits |= found | found_full
                full |= found_full
            except BaseException as error:  # noqa: BLE001
                log(f"state diff failed ({error!r}); assuming the worst")
                bits |= job.coarse
                full |= job.coarse
        indexes = policy.indexes(bits)
        slices, probes = plan_slices(
            maps, policy, indexes, full, job.targets, cell_targets, job.no_slice
        )
        to_verify, keep, estimate, probed = policy.decide(
            indexes,
            slices=slices,
            sure=job.sure,
            probes={i: probe[2] for i, probe in probes.items()},
        )
        lines = [f"K {i}" for i in keep] + [f"V {i}" for i in to_verify] + ["A"]
        os.write(write_fd, ("\n".join(lines) + "\n").encode())
        sliced = [i for i in to_verify if i in slices]
        log(
            f"{len(indexes)} candidates: verify {len(to_verify)} (est {estimate:.1f}s, "
            f"{len(sliced)} in slices), keep {len(keep)}"
        )
        for i in sliced:
            if i in probed:
                continue
            mode, kept, count, cost = slices[i]
            runner.SLICE[policy.tests[i]] = (mode, kept, count)
            log(f"slice {policy.tests[i].split('::')[-1]}: {kept.bit_count()} of {count} units")
        for i in probed:
            kind, payload, cost = probes[i]
            name = policy.tests[i].split("::")[-1]
            if kind == "units":
                runner.SLICE[policy.tests[i]] = payload
                log(f"probe {name}: {payload[1].bit_count()} of {payload[2]} units")
            else:
                runner.PROBE[policy.tests[i]] = payload
                log(f"probe {name}: first {payload:.1f}s")

        test_ids = [policy.tests[i] for i in to_verify]
        index_of = dict(zip(test_ids, to_verify))
        limits = {}
        for i in to_verify:
            if i in probed:
                kind, payload, cost = probes[i]
                limits[policy.tests[i]] = payload if kind == "time" else 3.0 + 4.0 * cost
            else:
                limits[policy.tests[i]] = 3.0 + 4.0 * (
                    slices[i][3] if i in slices else policy.cpu[i]
                )
        state = {"stop": False}

        def report(test_id, status):
            os.write(write_fd, f"{status} {index_of[test_id]}\n".encode())
            if status != "P":
                # One failure settles that a worker is needed; the rest are scheduled.
                state["stop"] = True

        before_run = time.process_time()
        runner.run(test_ids, limits=limits, report=report, stop=state)
        os.write(write_fd, b"D\n")
        log(
            f"cpu: setup+diff {before_run - started:.2f}s, tests "
            f"{time.process_time() - before_run:.2f}s"
        )
    except BaseException as error:  # noqa: BLE001
        log(f"verifier died: {error!r}")
    finally:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        finally:
            os._exit(0)
