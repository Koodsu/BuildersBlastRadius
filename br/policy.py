"""Scheduling arithmetic: how to split tests over workers, and when running
candidates ourselves is cheaper than scheduling them.

All costs are in the scorer's seconds. Scheduling a set costs its packing.
Verifying costs the CPU of running the tests. Once something fails a worker is
needed anyway, so from then on a test is only checked while what it would add
to that worker is worth more than the check.

Every CPU cost is the recorded-time estimate times CPU_FACTOR. The factor is 1:
scored at 0.4, this selector brought the simulated seconds of the practice set
from 148.9 down to 103.1 and was charged 89.7 s of CPU for it, where the
version before was charged 11.75 s. Checking is not cheap on the scorer. (The
0.4 came from comparing CPU on two sets of commits that were not alike.)
"""

from __future__ import annotations

import os

WORKER_MS = 2000
MAX_WORKERS = 8

# CPU seconds the scorer charges for work recorded as one second of test time.
CPU_FACTOR = float(os.environ.get("BR_CPU_FACTOR", "1.0"))
# The same for tests whose cost here was measured when the maps were built (the
# ones that hand work to a process pool) instead of taken from tests.json.
OWN_FACTOR = float(os.environ.get("BR_OWN_FACTOR", str(CPU_FACTOR)))

P_FAIL = 0.5  # chance that a change which reaches candidates breaks at least one
EARLY = 0.25  # share of the checking done before a failure shows, when there is one
MARGIN = 0.15  # verifying must look better than scheduling by this much

# Fixed cost of a verifier, by how it gets a patched sqlglot (see verify.py).
OVERHEAD = {mode: cost * CPU_FACTOR for mode, cost in {"hot": 0.08, "leaf": 0.35, "cold": 0.7}.items()}
FILE_COST = 0.02 * CPU_FACTOR

BIG_TEST = 2.5 * CPU_FACTOR  # a test this dear to run here is not verified in full, unless...
BIG_RATIO = 0.6  # ...running it here costs less than this share of its recorded time
DEAR = 1.3  # tests costing more than this many times their recorded time are "dear"

# A giant is a test recorded at PROBE_MIN seconds or more that also costs this
# much CPU to run here. Giants are settled last, on what the other tests showed:
# left out on evidence (see PROBE), run for real, or scheduled.
GIANT_CPU = 3.0
LATE_PASS = 0.8  # chance a giant passes when everything else did
LATE_ALONE = 0.5  # ...when nothing else was run
# ...when something else failed. How many of the others failed says little about
# a giant: over generated edits that test_execute_tpcds runs it passed in about
# six of ten, whether or not other tests failed.
LATE_AFTER_FAILURE = 0.6
# Share of a giant's cost spent before it fails, when it fails: 52 of 54 failing
# runs of test_execute_tpcds had failed within three tenths of a full run, most
# within one tenth.
LATE_EARLY = 0.2
# When a giant that does its work in units (the executor's TPC-DS test: one
# query a unit) is run here, its cheapest units go first, in a pass of their
# own, so that a failure shows early. They are no evidence for the rest: of 19
# generated edits that break that test, 4 passed every one of the cheapest
# units (a fifth of the cost), so a pass there never stands in for the test.
PREFIX_SHARE = 0.2

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

SLICE_MIN = 0.2 * CPU_FACTOR  # tests cheaper than this run whole
STAGES = os.environ.get("BR_STAGES", "1") == "1"  # cut units short after the optimizer
STAGE_SLACK = 2.0  # a staged run is costed at this many times its share: some units run on
STAGE_SHARE_MIN = 0.08  # ...and at no less than this share (the measured one is noisy)
STMT_MIN = 0.02 * CPU_FACTOR  # tests cheaper than this are not cut into statement units
STMT_SAVING = 0.004 * CPU_FACTOR  # a statement slice must leave out at least this much
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

        Returns (verify, keep, estimate, evidence, late): tests to run here in
        order, tests to schedule without running, the expected CPU, giants to
        leave out if everything in `verify` passes, and giants to settle by
        running them once the others are done (see `late_worth`). When all
        three of verify, evidence and late are empty, scheduling everything is
        the better deal.
        """
        slices = slices or {}
        if not indexes:
            return [], indexes, 0.0, [], []
        cost = {i: slice_cost(slices, i, self.cpu[i]) for i in indexes}
        recorded = {i: self.ms[self.to_number[i]] / 1000 for i in indexes}
        if any(sure >> i & 1 for i in indexes):
            # A worker is needed anyway; the others ride along for less than a check
            # costs. A giant does not: it is settled by running it, if that pays.
            late = [i for i in indexes if self.is_giant(i)]
            return [], [i for i in indexes if i not in set(late)], 0.0, [], late
        everything = self._pack_cost(indexes)
        overhead = 0.0 if diffed else OVERHEAD[mode]

        giants = [i for i in indexes if recorded[i] >= PROBE_MIN and cost[i] >= GIANT_CPU]
        # (a giant that a slice makes cheap is checked like any other test)
        big = [
            i
            for i in indexes
            if i not in set(giants) and cost[i] >= BIG_TEST and cost[i] >= BIG_RATIO * recorded[i]
        ]
        apart = set(big) | set(giants)
        rest = [i for i in indexes if i not in apart]
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

        best = (everything, [], indexes, [], [])  # schedule everything
        options = [(rest + big, [])]
        if big:
            options.append((rest, big))
        if dear and cheap:
            options.append((cheap, dear + big))
        for to_run, kept in options:
            if not to_run and not giants:
                continue
            # Left out on evidence only with everything else verified: nothing
            # scheduled on trust, and enough other tests to count as evidence.
            evidence = [
                i
                for i in giants
                if PROBE and not kept and droppable >> i & 1 and len(to_run) >= PROBE_OTHERS
            ]
            late = [i for i in giants if i not in set(evidence)]
            running = self._run_cost(to_run, cost) + overhead
            clean = self._pack_cost(kept) if kept else 0.0
            # What the giants cost when everything else passed: the verifier takes
            # them cheapest first, each weighed with the others still on the schedule.
            extra = PROBE_RISK * len(evidence)
            passing = LATE_PASS if to_run else LATE_ALONE
            waiting = sorted(late, key=cost.get)
            staying = []
            while waiting:
                i = waiting.pop(0)
                scheduled = [self.to_number[j] for j in kept + staying + waiting] + self.unknown
                worth, added = self.late_worth(i, cost[i], scheduled, len(to_run), 0)
                if worth:
                    extra += cost[i] * (passing + (1 - passing) * LATE_EARLY)
                    extra += (1 - passing) * added
                else:
                    staying.append(i)
            if staying:
                # Together, not one by one: two giants share the cost of their workers.
                extra += self._pack_cost(kept + staying) - clean
            expected = (
                running * (1 - P_FAIL + P_FAIL * EARLY)
                + (1 - P_FAIL) * (clean + extra)
                + P_FAIL * everything
            )
            if expected + MARGIN < best[0]:
                best = (expected, list(to_run), list(kept), evidence, late)

        expected, verify, keep, evidence, late = best
        if not verify and not evidence and not late:
            return [], indexes, 0.0, [], []
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
        return verify, keep, self._run_cost(verify, cost) + overhead, evidence, late

    def is_giant(self, index):
        number = self.to_number[index]
        return (
            number is not None
            and self.ms[number] / 1000 >= PROBE_MIN
            and self.cpu[index] >= GIANT_CPU
        )

    def worth_checking(self, index, cost, passed, failed, settled, open_load):
        """After something has failed: is this test still worth running here?

        A worker is needed whatever this test does, so checking it only pays if
        it is likely to pass and leaving it out shortens the busiest worker.
        `settled` is the recorded seconds of what is scheduled for certain,
        `open_load` those of the tests not yet checked, this one included.
        """
        if not failed:
            return True
        recorded = self.ms[self.to_number[index]] / 1000
        passing = (passed + 1) / (passed + failed + 2)
        # Where the schedule is heading if the rest is checked too. A load T split
        # evenly over k workers costs T/k + 2k: one more worker pays once T
        # passes 2k(k+1).
        load = settled + (1 - passing) * open_load
        workers = 1
        while workers < MAX_WORKERS and load > 2 * workers * (workers + 1):
            workers += 1
        return cost * (1 + MARGIN) < passing * recorded / workers

    def late_worth(self, index, cost, scheduled, passed, failed, changed=0.0):
        """Is a giant worth running here, given the tests already settled?

        `scheduled` is the test numbers that will be scheduled whatever the giant
        does. `changed` is the share of its work already known to differ from
        the pinned commit. Returns (worth it, seconds scheduling the giant would add).
        """
        number = self.to_number[index]
        without = pack(list(scheduled), self.ms)[0] / 1000
        added = pack(list(scheduled) + [number], self.ms)[0] / 1000 - without
        if failed:
            passing = LATE_AFTER_FAILURE
        else:
            passing = LATE_PASS if passed else LATE_ALONE
        passing *= 1.0 - changed
        expected = cost * (passing + (1 - passing) * LATE_EARLY)
        return passing * added > expected + MARGIN, added


def _stageable(stage, targets):
    """True if none of the changed lines runs after the optimizer in this test.

    Belt and braces: nothing in the planner or the executor is ever staged.
    """
    back = stage["back"]
    for source, lines in targets.items():
        if source.startswith("sqlglot/executor/") or source == "sqlglot/planner.py":
            return False
        after = back.get(source)
        if after and not set(after).isdisjoint(lines):
            return False
    return bool(targets)


def plan_slices(maps, policy, indexes, full, targets, cell_targets, no_slice):
    """For candidates whose work splits into units, the units a change can reach.

    Returns {test index: (mode, units to keep, unit count, estimated cost)}.
    """
    slices = {}
    # Units of one test are not isolated from each other: sqlglot builds some
    # tables on first use and keeps them (the generators' dispatch tables), so
    # the unit that is seen to read a class attribute is only the first that
    # needed it. A change to class-level state can reach any unit.
    if no_slice or cell_targets:
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
        if not needed:
            continue  # nothing explains why it is a candidate: run it whole
        keep = needed & ((1 << count) - 1)
        costs = units["cost"]
        whole = units["always"] + sum(costs)
        part = units["always"] + sum(costs[unit] for unit in range(count) if keep >> unit & 1)
        rate = policy.cpu[index] / whole if whole > 0 else 1.0
        cost = part * rate
        stage = maps.stages().get(maps.tests[index])
        if stage and STAGES and _stageable(stage, targets):
            # Each unit is cut short after the optimizer unless what it hands on changed.
            # (the test's cost already allows for skipped comparisons: see selector.py)
            cost *= max(stage["share"], STAGE_SHARE_MIN) * STAGE_SLACK
            cost /= stage.get("result_share", 1.0) if any(stage.get("result") or ()) else 1.0
            slices[("staged", index)] = True
        slices[index] = (units["mode"], keep, count, cost)
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
            if saved < STMT_SAVING:
                continue
            # (the statement units to keep, how many there are, CPU left out)
            slices[("s", index)] = (keep, count, min(saved, policy.cpu[index] * 0.98))
    return slices
