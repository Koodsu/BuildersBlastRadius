"""Scheduling arithmetic: how to split tests over workers, and when running
candidates ourselves is cheaper than scheduling them.

All costs are in the scorer's seconds. Scheduling a set costs its packing.
Verifying costs the CPU of running the tests, but only as far as the first
failure: once something fails a worker is needed anyway and the rest are
scheduled unverified.
"""

from __future__ import annotations

import os

WORKER_MS = 2000
MAX_WORKERS = 8

P_FAIL = 0.5  # chance that a change which reaches candidates breaks at least one
EARLY = 0.25  # share of the checking done before a failure shows, when there is one
MARGIN = 0.15  # verifying must look better than scheduling by this much

# Fixed cost of a verifier, by how it gets a patched sqlglot (see verify.py).
OVERHEAD = {"hot": 0.08, "leaf": 0.35, "cold": 0.7}
DIFF_COST = {"leaf": 0.15, "cold": 0.8}  # importing and fingerprinting state
FILE_COST = 0.02

BIG_TEST = 2.5  # a test this expensive to run here is not verified in full, unless...
BIG_RATIO = 0.6  # ...running it here costs less than this share of its recorded time
DEAR = 1.3  # tests costing more than this many times their recorded time are "dear"

# Dropping on evidence: a test far too expensive to verify here is left out once
# every other test that runs the same changed statement has been run and has
# passed. This is evidence, not proof. In 13 trial edits where every cheaper
# test passed, the expensive one passed too; edits that broke something were
# often caught only by the dearer of the other tests, which is why all of them
# must be run, not a sample.
PROBE = os.environ.get("BR_PROBE", "0") == "1"
PROBE_OTHERS = int(os.environ.get("BR_PROBE_OTHERS", "12"))  # other tests that must pass
PROBE_MIN = 6.0  # only tests recorded at least this long are worth the risk
PROBE_RISK = float(os.environ.get("BR_PROBE_RISK", "6.0"))  # the risk's price, in seconds

SLICE_MIN = 0.2  # tests cheaper than this run whole
STMT_MIN = 0.02  # tests cheaper than this are not cut into statement units
STMTS = os.environ.get("BR_STMTS", "1") == "1"


def slice_cost(slices, index, whole):
    """What a candidate costs to run here, given the slices planned for it."""
    cost = whole
    if index in slices:
        cost = slices[index][3]
    part = slices.get(("s", index))
    if part is not None:
        cost = max(0.0, min(cost, whole - part[2]))
    return cost


def pack(numbers, ms):
    """Split tests over workers to minimise largest load + 2 s per worker."""
    if not numbers:
        return 0, []
    ordered = sorted(numbers, key=lambda n: -ms[n])
    best = None
    for count in range(1, min(MAX_WORKERS, len(ordered)) + 1):
        loads = [0] * count
        workers = [[] for _ in range(count)]
        for number in ordered:
            slot = loads.index(min(loads))
            loads[slot] += ms[number]
            workers[slot].append(number)
        cost = max(loads) + WORKER_MS * count
        if best is None or cost < best[0]:
            best = (cost, workers)
        if max(loads) == ms[ordered[0]]:
            break  # more workers cannot lower the largest load
    return best


class Policy:
    def __init__(self, tests, to_number, ms, cpu, setup_cost, order, unknown, bare=()):
        self.tests = tests  # build-order test ids
        self.to_number = to_number  # build index -> test number (or None)
        self.ms = ms
        self.cpu = cpu  # build index -> seconds to run it here
        self.setup_cost = setup_cost
        self.order = order  # test id -> position in the suite
        self.unknown = unknown  # test numbers the maps know nothing about
        self.bare = set(bare)  # test ids that pass without their class's setUpClass
        self.bare_index = {i for i, test_id in enumerate(tests) if test_id in self.bare}

    def indexes(self, bits):
        found = []
        index = 0
        while bits:
            if bits & 1 and self.to_number[index] is not None:
                found.append(index)
            bits >>= 1
            index += 1
        found.sort(key=lambda i: self.order[self.tests[i]])
        return found

    def _pack_cost(self, indexes):
        numbers = [self.to_number[i] for i in indexes] + self.unknown
        return pack(numbers, self.ms)[0] / 1000

    def _run_cost(self, group, cost):
        """CPU to run a set of tests: their own time, class fixtures, module imports."""
        classes = {self.tests[i].rsplit("::", 1)[0] for i in group if i not in self.bare_index}
        files = {self.tests[i].split("::", 1)[0] for i in group}
        return (
            sum(cost[i] for i in group)
            + sum(self.setup_cost.get(key, 0.0) for key in classes)
            + FILE_COST * len(files)
        )

    def decide(self, indexes, mode="cold", slices=None, sure=0, droppable=0, diffed=False):
        """Split candidates into those to verify and those to schedule unverified.

        `slices` maps a test's index to (mode, units to keep, unit count, cost)
        when only part of it needs to run. `sure` is a bitset of candidates
        expected to fail whatever else happens. `droppable` is a bitset of
        candidates that may be dropped on evidence (see PROBE). `diffed` says the
        verifier's fixed costs are already spent.

        Returns (verify, keep, estimate, dropped): tests to run here in order,
        tests to schedule without running, the expected CPU, and tests to leave
        out if everything in `verify` passes. `verify` is empty when scheduling
        everything is the better deal.
        """
        slices = slices or {}
        if not indexes or any(sure >> i & 1 for i in indexes):
            # A worker is needed anyway; the others ride along for less than a check costs.
            return [], indexes, 0.0, []
        cost = {i: slice_cost(slices, i, self.cpu[i]) for i in indexes}
        recorded = {i: self.ms[self.to_number[i]] / 1000 for i in indexes}
        everything = self._pack_cost(indexes)
        overhead = 0.0 if diffed else OVERHEAD[mode]

        big = [i for i in indexes if cost[i] >= BIG_TEST and cost[i] >= BIG_RATIO * recorded[i]]
        rest = [i for i in indexes if i not in set(big)]
        # Tests that cost much more to run here than they were recorded at, counting
        # the fixtures their class needs, are better scheduled than checked.
        by_class = {}
        for i in rest:
            by_class.setdefault(self.tests[i].rsplit("::", 1)[0], []).append(i)
        cheap = []
        dear = []
        for key, members in by_class.items():
            setup = self.setup_cost.get(key, 0.0)
            if all(i in self.bare_index for i in members):
                setup = 0.0
            # Tests that need the class's fixtures go last: stopping before them saves it.
            members.sort(key=lambda i: (i not in self.bare_index, cost[i] / (recorded[i] + 0.01)))
            taken = []
            spent = setup
            seen = 0.0
            for i in members:
                if (spent + cost[i]) <= DEAR * (seen + recorded[i]) + 0.05 * (len(taken) + 1):
                    taken.append(i)
                    spent += cost[i]
                    seen += recorded[i]
                else:
                    break
            cheap.extend(taken)
            dear.extend(i for i in members if i not in set(taken))

        best = (everything, [], indexes, [])  # schedule everything
        options = [(rest, [])]
        if dear and cheap:
            options.append((cheap, dear))
        for to_run, to_schedule in options:
            if not to_run:
                continue
            for dropping in (False, True):
                dropped = []
                kept = list(to_schedule)
                extra = 0.0
                if dropping:
                    # Only with everything else verified: nothing scheduled on trust.
                    if not PROBE or to_schedule:
                        continue
                    dropped = [
                        i for i in big if droppable >> i & 1 and recorded[i] >= PROBE_MIN
                    ]
                    to_run = to_run + [i for i in big if i not in set(dropped)]
                    if not dropped or len(to_run) < PROBE_OTHERS:
                        continue
                    extra = PROBE_RISK * len(dropped)
                else:
                    kept += big
                running = self._run_cost(to_run, cost) + overhead
                clean = self._pack_cost(kept) if kept else 0.0
                expected = (
                    running * (1 - P_FAIL + P_FAIL * EARLY)
                    + (1 - P_FAIL) * (clean + extra)
                    + P_FAIL * everything
                )
                if expected + MARGIN < best[0]:
                    best = (expected, list(to_run), kept, dropped)

        expected, verify, keep, dropped = best
        if not verify:
            return [], indexes, 0.0, []
        # Cheapest first, keeping each class together so its fixtures are built once.
        floor = {}
        for i in verify:
            key = self.tests[i].rsplit("::", 1)[0]
            floor[key] = min(floor.get(key, 1e9), cost[i])
        verify.sort(
            key=lambda i: (
                floor[self.tests[i].rsplit("::", 1)[0]]
                + self.setup_cost.get(self.tests[i].rsplit("::", 1)[0], 0.0),
                self.tests[i].rsplit("::", 1)[0],
                cost[i],
            )
        )
        return verify, keep, self._run_cost(verify, cost) + overhead, dropped


def plan_slices(maps, policy, indexes, full, targets, cell_targets, no_slice):
    """For candidates whose work splits into units, the units a change can reach.

    Returns {test index: (mode, units to keep, unit count, estimated cost)}.
    """
    slices = {}
    if no_slice or cell_targets:
        # A change to class-level state is not cut by who read it: values built
        # from it on first use (the generator's dispatch table) are read once per
        # process, so only the first unit to need them is recorded as a reader.
        return slices
    sliceable = maps.meta.get("sliceable", 0)
    for index in indexes:
        if full >> index & 1 or not sliceable >> index & 1 or policy.cpu[index] < SLICE_MIN:
            continue
        units = maps.units(index)
        if units is None:
            continue
        count = units["count"]
        needed = 0
        for source, lines in targets.items():
            by_line = units["lines"].get(source)
            if by_line:
                for line in lines:
                    needed |= by_line.get(line, 0)
        for key in cell_targets:
            needed |= units["reads"].get(key, 0)
        if not needed:
            continue  # nothing explains why it is a candidate: run it whole
        keep = needed & ((1 << count) - 1)
        costs = units["cost"]
        whole = units["always"] + sum(costs)
        part = units["always"] + sum(costs[unit] for unit in range(count) if keep >> unit & 1)
        rate = policy.cpu[index] / whole if whole > 0 else 1.0
        slices[index] = (units["mode"], keep, count, part * rate)
    if STMTS and targets and not cell_targets:
        for index in indexes:
            if full >> index & 1 or policy.cpu[index] < STMT_MIN:
                continue
            record = maps.stmts(index)
            if record is None or record.get("traced", 0) <= 0:
                continue
            count = len(record["units"])
            needed = 0
            for source, lines in targets.items():
                by_line = record["lines"].get(source)
                if by_line:
                    for line in lines:
                        needed |= by_line.get(line, 0)
            if not needed:
                continue  # nothing explains why it is a candidate: run it whole
            keep = (needed | record.get("pool", 0)) & ((1 << count) - 1)
            costs = record["cost"]
            rate = policy.cpu[index] / record["traced"]
            saved = sum(costs[unit] for unit in range(count) if not keep >> unit & 1) * rate
            if saved < 0.004:
                continue
            # (the statement units to keep, how many there are, CPU left out)
            slices[("s", index)] = (keep, count, min(saved, policy.cpu[index] * 0.98))
    return slices
