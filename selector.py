"""Blast radius selector.

For each patch: find the tests whose recorded run at the pinned commit touches what the
patch changes (maps/), run those against a patched copy of sqlglot when that is cheaper
than scheduling them, and print the tests that fail.
"""

import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from brx import patching  # noqa: E402

DATA = os.environ.get("BR_DATA", "/data")
SG = os.environ.get("BR_SG", "/sg")
MAPS = os.environ.get("BR_MAPS", os.path.join(os.path.dirname(os.path.abspath(__file__)), "maps"))
TMP = os.environ.get("BR_TMP", "/tmp/br")
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "2.5"))  # scorer CPU seconds per traced second
WALL_BUDGET = float(os.environ.get("BR_WALL", "140"))
VERIFY = os.environ.get("BR_VERIFY", "1") == "1"
DEBUG = os.environ.get("BR_DEBUG", "0") == "1"
MIN_WITNESSES = int(os.environ.get("BR_MIN_WITNESSES", "5"))
HEAVY_SECONDS = 5.0  # recorded seconds above which a test is never run here
BASE_CPU = 0.15  # traced seconds to import sqlglot in a child
START = time.monotonic()


def cpu_now():
    t = os.times()
    return t[0] + t[1] + t[2] + t[3]  # this process and the children it has waited for


def log(*parts):
    sys.stderr.write(" ".join(str(p) for p in parts) + "\n")


def schedule_cost(numbers, millis):
    """Cheapest split: longest first onto the lightest worker, for 1..8 workers."""
    if not numbers:
        return 0, []
    ordered = sorted(numbers, key=lambda n: -millis[n])
    best = None
    for count in range(1, min(8, len(ordered)) + 1):
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for n in ordered:
            i = loads.index(min(loads))
            loads[i] += millis[n]
            workers[i].append(n)
        cost = max(loads) + 2000 * count
        if best is None or cost < best[0]:
            best = (cost, workers)
        if max(loads) == millis[ordered[0]]:
            break  # more workers cannot lower the longest test
    return best


class Maps:
    def __init__(self):
        index = json.load(open(os.path.join(MAPS, "index.json")))
        self.ids = index["tests"]
        self.width = index["width"]
        self.files = index["files"]
        self.opened = index["opened"]
        self.est = index["est"]
        self.sets = open(os.path.join(MAPS, "sets.bin"), "rb")
        probes = json.load(open(os.path.join(MAPS, "probes.json")))
        self.expected = probes["expected"]
        self.unsafe = {path: frozenset(lines) for path, lines in probes["unsafe"].items()}

    def mask(self, set_id):
        self.sets.seek(set_id * self.width)
        return int.from_bytes(self.sets.read(self.width), "little")

    def lines(self, path, wanted):
        entry = self.files.get(path)
        if entry is None:
            return 0
        lines, set_ids = entry
        mask = 0
        seen = set()
        for line, set_id in zip(lines, set_ids):
            if (wanted is patching.ALL or line in wanted) and set_id not in seen:
                seen.add(set_id)
                mask |= self.mask(set_id)
        return mask

    def readers(self, key):
        set_id = self.opened.get(key)
        return self.mask(set_id) if set_id is not None else 0


def analyze(maps, patch_text):
    """-> (candidate mask over map tests, {path: new bytes or None}, {path: lines reached})."""
    changes = {}
    reach = {}
    mask = 0
    for entry in patching.parse_patch(patch_text):
        old_path, new_path = entry["old"], entry["new"]
        if entry["binary"]:
            raise patching.PatchError("binary")
        for path in {old_path, new_path} - {None}:
            if path.startswith("/") or ".." in path.split("/"):
                raise patching.PatchError("path")
            # Anything that lists the folder sees a file appear, vanish or move.
            if old_path != new_path:
                mask |= maps.readers("d:" + os.path.dirname(path))
        old_text = ""
        if old_path is not None:
            with open(os.path.join(SG, old_path), encoding="utf-8", newline="") as handle:
                old_text = handle.read()
            mask |= 0 if old_path.endswith(".py") else maps.readers("f:" + old_path)
        new_text, groups = patching.apply_hunks(old_text, entry["hunks"])
        if old_path is not None and old_path != new_path:
            changes[old_path] = None
            mask |= maps.lines(old_path, patching.ALL) | maps.readers("f:" + old_path)
        if new_path is None:
            continue
        changes[new_path] = new_text.encode("utf-8")
        if old_path is None or old_path != new_path:
            continue
        if new_text == old_text:
            continue
        if new_path.endswith(".py"):
            wanted = patching.reach(old_text, new_text, groups)
            reach[new_path] = wanted
            if wanted is patching.ALL:
                mask |= maps.lines(new_path, patching.ALL) | maps.readers("f:" + new_path)
            elif wanted:
                mask |= maps.lines(new_path, wanted)
    return mask, changes, reach


def build_overlay(root, changes):
    """A copy of the tree made of links, with real files where the patch changed them."""
    real = {""}
    for path in changes:
        parts = path.split("/")
        for depth in range(1, len(parts)):
            real.add("/".join(parts[:depth]))
    for folder in sorted(real, key=len):
        target = os.path.join(root, folder)
        os.makedirs(target, exist_ok=True)
        source = os.path.join(SG, folder)
        if not os.path.isdir(source):
            continue
        for name in os.listdir(source):
            relative = f"{folder}/{name}" if folder else name
            if relative in real or relative in changes or name == ".git":
                continue
            os.symlink(os.path.join(source, name), os.path.join(target, name))
    for path, data in changes.items():
        if data is not None:
            with open(os.path.join(root, path), "wb") as handle:
                handle.write(data)


_warm = False


def warm_libraries():
    """Loaded once in this process so every forked child gets them for free."""
    global _warm
    if not _warm:
        import unittest  # noqa: F401
        import duckdb  # noqa: F401
        import numpy  # noqa: F401
        import pandas  # noqa: F401

        _warm = True


NEEDS_LIBRARIES = ("tests/test_executor.py", "tests/test_optimizer.py", "tests/test_optimize_cross_joins.py", "tests/test_simplify_between.py", "tests/test_lineage.py")


def probe_can_vouch(maps, name, reach, changes):
    """The TPC probes compare plans; they say nothing about code that runs after planning."""
    if "execute_tpc" not in name:
        return True
    for path in changes:
        if path.startswith("tests/fixtures/optimizer/tpc-") and path.endswith(".sql"):
            continue  # the probe reads the queries itself and prints what they turn into
        if not path.endswith(".py") or path.startswith(("sqlglot/executor/", "tests/")) or path == "sqlglot/planner.py":
            return False
        wanted = reach.get(path)
        if wanted is None:
            continue
        if wanted is patching.ALL or wanted & maps.unsafe.get(path, frozenset()):
            return False
    return True


def decide(maps, commit, patch_text, number_of, millis, all_numbers):
    mask, changes, reach = analyze(maps, patch_text)
    candidates = [i for i in range(len(maps.ids)) if mask >> i & 1]
    unknown = [n for test_id, n in number_of.items() if test_id not in maps.index_of]
    if not candidates:
        return [], "no test reaches the change"
    numbers = {i: number_of[maps.ids[i]] for i in candidates if maps.ids[i] in number_of}
    heavy = [i for i in numbers if millis[numbers[i]] >= HEAVY_SECONDS * 1000]
    light = [i for i in numbers if i not in heavy]
    everything = sorted(set(numbers.values()) | set(unknown))
    outside = [p for p in changes if not p.startswith(("sqlglot/", "tests/"))]
    elapsed = time.monotonic() - START
    if not VERIFY or outside or elapsed > WALL_BUDGET:
        return everything, f"scheduled unchecked: {len(everything)} tests (heavy {len(heavy)})"
    probes = [maps.ids[i] for i in heavy if maps.ids[i] in maps.expected and probe_can_vouch(maps, maps.ids[i], reach, changes)]
    kept_heavy = [i for i in heavy if maps.ids[i] not in probes]
    light_numbers = sorted({numbers[i] for i in light} | set(unknown))
    cost_light, _ = schedule_cost(light_numbers, millis)
    estimate = (BASE_CPU + sum(maps.est[i] for i in light)) * CPU_FACTOR
    # Running the light candidates pays only if it is cheaper than scheduling them, and only
    # when no slow test is going out anyway (beside one, the light ones ride for 2 s).
    cost_all, _ = schedule_cost(everything, millis)
    check_light = bool(light) and not unknown and estimate * 1000 < (cost_all if kept_heavy else cost_light)
    if not probes and not check_light:
        return everything, f"scheduled unchecked: {len(everything)} tests (heavy {len(heavy)}), est {estimate:.1f}s vs {cost_light / 1000:.1f}s"
    target = commit.lower()
    light.sort(key=lambda i: (maps.ids[i].split("::")[:2], maps.est[i]))
    ids = [maps.ids[i] for i in light] if check_light else []
    if any(test_id.startswith(NEEDS_LIBRARIES) for test_id in ids):
        warm_libraries()
    from brx import harness

    root = os.path.join(TMP, "ov")
    shutil.rmtree(root, ignore_errors=True)
    build_overlay(root, changes)
    try:
        timeouts = {maps.ids[i]: 20.0 + 30.0 * maps.est[i] * CPU_FACTOR for i in light}
        results, _, status = harness.fork_run(
            root, ids, lambda t: timeouts[t], stall_seconds=60.0, debug=DEBUG,
            probes=probes, expected=maps.expected, stop_at_failure=True,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    failed_probes = [name for name in probes if not results.get(name, (False,))[0]]
    kept = {numbers[i] for i in kept_heavy} | {number_of[name] for name in failed_probes}
    passed = {i for i in light if results.get(maps.ids[i], (False,))[0]}
    if check_light and not failed_probes:
        failing = [i for i in light if maps.ids[i] in results and i not in passed]
        if not failing and len(passed) == len(light):
            if len(light) >= MIN_WITNESSES:
                # A slow test whose plans could not be compared is left out when every other
                # test that runs the changed lines passes. This is a bet, not a proof.
                kept = {number_of[name] for name in failed_probes}
            return sorted(kept), f"all {len(light)} candidates pass; probes ok {len(probes)}; slow kept {len(kept)}"
        chosen = sorted(kept | {numbers[i] for i in light if i not in passed})
        return chosen, f"{len(failing)} failed after {len(passed)} passed; scheduled {len(chosen)} of {len(numbers)}"
    chosen = sorted(kept | set(light_numbers))
    return chosen, f"light unchecked ({len(light)}, est {estimate:.1f}s vs {cost_light / 1000:.1f}s); probes {len(probes)} failed {len(failed_probes)}; slow kept {len(kept)}"


def main():
    out = os.dup(1)
    os.dup2(2, 1)  # nothing but answers may reach stdout
    tests = json.load(open(os.path.join(DATA, "tests.json")))["tests"]
    millis = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}
    number_of = {t["id"]: t["number"] for t in tests}
    all_numbers = sorted(millis)
    _, fallback = schedule_cost(all_numbers, millis)
    commits = sorted(name[:-6] for name in os.listdir(os.path.join(DATA, "commits")) if name.endswith(".patch"))
    try:
        maps = Maps()
        maps.index_of = {test_id: i for i, test_id in enumerate(maps.ids)}
    except Exception as error:
        log("maps failed to load:", repr(error))
        maps = None
    os.makedirs(TMP, exist_ok=True)
    for commit in commits:
        started = cpu_now()
        try:
            if maps is None:
                raise RuntimeError("no maps")
            with open(os.path.join(DATA, "commits", commit + ".patch"), encoding="utf-8", errors="surrogateescape", newline="") as handle:
                patch_text = handle.read()
            chosen, why = decide(maps, commit, patch_text, number_of, millis, all_numbers)
            _, workers = schedule_cost(chosen, millis) if chosen else (0, [])
        except Exception as error:
            workers, why = fallback, f"fallback to every test: {error!r}"
        os.write(out, (json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")) + "\n").encode())
        log(f"{commit}: {sum(len(w) for w in workers)} tests on {len(workers)} workers; {why}; cpu {cpu_now() - started:.2f}s")
    log(f"done in {time.monotonic() - START:.1f}s wall, cpu total {cpu_now():.2f}s")


if __name__ == "__main__":
    main()
