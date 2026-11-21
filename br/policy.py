"""Scheduling arithmetic: how to split tests over workers, and when running
candidates ourselves is cheaper than scheduling them."""

from __future__ import annotations

import os

WORKER_MS = 2000
MAX_WORKERS = 8
VERIFY_MARGIN = 0.3  # verifying must beat scheduling by this many seconds
BIG_TEST = 3.0  # a test this expensive to run here is scheduled instead, unless...
BIG_RATIO = 0.6  # ...running it here costs less than this share of its recorded time
IMPORT_COST = 0.15
FILE_COST = 0.03

# Probing: a test too expensive to verify can be judged on part of itself, once
# enough cheaper tests that run the same changed code have all passed. This is
# evidence, not proof, so it is off unless asked for.
PROBE = os.environ.get("BR_PROBE", "0") == "1"
PROBE_OTHERS = 12  # cheaper candidates that must be verified alongside
PROBE_SECONDS = 2.0  # how much of the expensive test to run


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
    def __init__(self, tests, to_number, ms, cpu, setup_cost, order, unknown):
        self.tests = tests  # build-order test ids
        self.to_number = to_number  # build index -> test number (or None)
        self.ms = ms
        self.cpu = cpu  # build index -> seconds to run it here
        self.setup_cost = setup_cost
        self.order = order  # test id -> position in the suite
        self.unknown = unknown  # test numbers the maps know nothing about

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

    def decide(self, indexes, extra_cost=0.0, slices=None, sure=0, probes=None):
        """Split candidates into those to verify and those to schedule unverified.

        `slices` maps a test's index to (mode, units to keep, unit count, estimated
        cost) when only part of the test needs to run. `sure` is a bitset of
        candidates expected to fail whatever else happens.

        `probes` maps a test's index to the cost of running part of it (see PROBE).

        Returns (verify, keep, estimate, probed). `verify` is empty when
        scheduling everything is the better deal; otherwise it is in the order
        to run them. The verifier stops at the first failure and the rest are
        scheduled, so a commit that breaks things costs little more than
        scheduling it outright. `probed` are the tests in `verify` that will
        only be run in part.
        """
        slices = slices or {}
        probes = probes or {}
        if any(sure >> i & 1 for i in indexes):
            # A worker is needed anyway; the others ride along for less than a check costs.
            return [], indexes, 0.0, set()
        cost = {i: slices[i][3] if i in slices else self.cpu[i] for i in indexes}
        numbers = [self.to_number[i] for i in indexes] + self.unknown
        schedule_cost = pack(numbers, self.ms)[0] / 1000
        big = []
        small = []
        for i in indexes:
            recorded = self.ms[self.to_number[i]] / 1000
            if cost[i] >= BIG_TEST and cost[i] >= BIG_RATIO * recorded:
                big.append(i)
            else:
                small.append(i)
        probed = set()
        if PROBE and big and len(small) >= PROBE_OTHERS:
            for i in list(big):
                if i in probes:
                    big.remove(i)
                    small.append(i)
                    cost[i] = probes[i]
                    probed.add(i)
        if not small:
            return [], indexes, 0.0, set()
        classes = {}
        for i in small:
            key = self.tests[i].rsplit("::", 1)[0]
            classes[key] = min(classes.get(key, 1e9), cost[i])
        files = {self.tests[i].split("::", 1)[0] for i in small}
        estimate = (
            extra_cost
            + FILE_COST * len(files)
            + sum(self.setup_cost.get(key, 0.0) for key in classes)
            + sum(cost[i] for i in small)
        )
        kept = [self.to_number[i] for i in big] + self.unknown
        kept_cost = pack(kept, self.ms)[0] / 1000
        if kept_cost + estimate + VERIFY_MARGIN >= schedule_cost:
            return [], indexes, estimate, set()
        # Cheapest first, keeping each class together so its fixtures are built once.
        small.sort(
            key=lambda i: (
                i in probed,
                classes[self.tests[i].rsplit("::", 1)[0]] + self.setup_cost.get(
                    self.tests[i].rsplit("::", 1)[0], 0.0
                ),
                self.tests[i].rsplit("::", 1)[0],
                cost[i],
            )
        )
        return small, big, estimate, probed


SLICE_MIN = 0.2  # tests cheaper than this run whole


def plan_slices(maps, policy, indexes, full, targets, cell_targets, no_slice):
    """For candidates whose work splits into units, the units a change can reach.

    Returns {test index: (mode, units to keep, unit count, estimated cost)}.
    """
    slices = {}
    if no_slice:
        return slices, {}
    probes = {}  # index -> (kind, payload, cost): part of an expensive test, as evidence
    sliceable = maps.meta.get("sliceable", 0)
    for index in indexes:
        if PROBE and not full >> index & 1 and policy.cpu[index] >= BIG_TEST:
            probes[index] = ("time", PROBE_SECONDS, PROBE_SECONDS)
    for index in indexes:
        if not sliceable >> index & 1 or full >> index & 1 or policy.cpu[index] < SLICE_MIN:
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
        estimate = part * rate
        slices[index] = (units["mode"], keep, count, estimate)
        if PROBE and estimate >= BIG_TEST:
            # The cheapest units that reach the change, up to the probe's budget.
            chosen = 0
            spent = units["always"] * rate
            for unit in sorted((u for u in range(count) if keep >> u & 1), key=costs.__getitem__):
                if chosen and spent + costs[unit] * rate > PROBE_SECONDS and chosen.bit_count() >= 5:
                    break
                chosen |= 1 << unit
                spent += costs[unit] * rate
            probes[index] = ("units", (units["mode"], chosen, count), spent)
    return slices, probes
