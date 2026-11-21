"""Run candidate tests against patched sqlglot, in a forked child.

A verifier gets its patched sqlglot in one of three ways, cheapest first:

hot   Only function bodies changed, and none of them runs at import time. The
      child is forked from a server that has already imported sqlglot and the
      test modules; the new code objects are swapped into the live functions.
leaf  The changed modules are ones a bare `import sqlglot` does not load. The
      child is forked from a server that has done just that import, and loads
      everything else from a view of the tree with the patch applied.
cold  Anything else. The child imports the whole patched tree itself.

The child talks to the selector over a pipe, one line per fact, with tests
named by their build-order index:

    I <pid>     the verifier's process id
    X           this mode cannot handle the job after all: retry it cold
    K <i>       scheduled without being run
    V <i>       will be run here
    A           the two lists above are complete
    P/F/T/U <i> passed / failed / timed out / sliced run did not go as planned
    D           finished
"""

from __future__ import annotations

import gc
import importlib
import os
import resource
import shutil
import signal
import sys
import time
import types

from br import analyze, cells
from br.policy import plan_slices, slice_cost

HEAVY = ("tests/test_optimizer.py", "tests/test_executor.py")
CONTINUE_RATIO = float(os.environ.get("BR_CONTINUE", "0.3"))

# Old and new side by side (see br/dual.py). When the edited expression has been
# evaluated often enough, in enough tests, in every candidate that is cheap to
# run, and its old and new form never once differed, the candidates that are
# dear to run are left out: nothing they do can differ either, unless they feed
# the expression values none of the others did.
# Development: run every test that was cut down to some of its statements a second
# time, whole, and log both verdicts.
STMT_AUDIT = os.environ.get("BR_STMT_AUDIT") == "1"
DUAL = os.environ.get("BR_DUAL", "1") == "1"
# Development: "1" runs everything and logs what would be dropped; "2" stops a job
# once it is clear the rule cannot apply, and otherwise runs what it would drop.
DUAL_AUDIT = os.environ.get("BR_DUAL_AUDIT") in ("1", "2")
DUAL_AUDIT_QUICK = os.environ.get("BR_DUAL_AUDIT") == "2"
DUAL_CHEAP = float(os.environ.get("BR_DUAL_CHEAP", "0.25"))  # seconds: dearer tests can be dropped
DUAL_MIN_EVALS = int(os.environ.get("BR_DUAL_EVALS", "300"))
DUAL_MIN_TESTS = int(os.environ.get("BR_DUAL_TESTS", "20"))
# When scheduling everything looked cheaper than checking: the share of that cost
# worth spending on the cheap candidates to find out whether the edit changes nothing.
DUAL_PROBE_SHARE = float(os.environ.get("BR_DUAL_PROBE", "0.12"))


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


def redirect_packages(base, dest):
    """Make already imported sqlglot packages find their submodules, and report
    their location, in the patched view."""
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "sqlglot" or name.startswith("sqlglot.")):
            continue
        path = getattr(module, "__path__", None)
        if path is None:
            continue
        try:
            module.__path__ = [
                dest + entry[len(base) :] if entry.startswith(base) else entry for entry in path
            ]
            location = getattr(module, "__file__", None)
            if location and location.startswith(base):
                module.__file__ = dest + location[len(base) :]
        except Exception:  # noqa: BLE001
            pass
    sys.path[:] = [dest if entry == base else entry for entry in sys.path]
    if dest not in sys.path:
        sys.path.insert(0, dest)
    importlib.invalidate_caches()


def build_function_index(base):
    """{(file, first line, name): [live functions]} for everything defined in sqlglot."""
    index = {}
    prefix = base + "/sqlglot/"
    for item in gc.get_objects():
        if type(item) is types.FunctionType:
            code = item.__code__
            if code.co_filename.startswith(prefix):
                index.setdefault((code.co_filename, code.co_firstlineno, code.co_name), []).append(
                    item
                )
    return index


def apply_hot(job, index):
    """Give every live function that was edited, or encloses an edit, its new code."""
    for filename, first, name, code in job.hot:
        for function in index.get((filename, first, name), ()):
            function.__code__ = code


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


def _function_runners(maps, state, module, qualname, attr):
    """Tests that run the function a changed cell holds, or None if it is not one.

    A function whose defaults or closure changed can only matter to tests that
    call it.
    """
    owner = sys.modules.get(module)
    for part in (qualname.split(".") if qualname else []):
        owner = getattr(owner, part, None) if owner is not None else None
    value = vars(owner).get(attr) if owner is not None and hasattr(owner, "__dict__") else None
    if isinstance(value, (staticmethod, classmethod)):
        value = value.__func__
    if not isinstance(value, types.FunctionType) or value.__module__ != module:
        return None
    rel = state["module_file"].get(module)
    cov = maps.cov(rel) if rel else None
    if cov is None or "index" not in cov:
        return None
    wanted = tuple(part for part in value.__qualname__.split(".") if part != "<locals>")
    matches = [
        info
        for path, info in cov["index"]["tree"].items()
        if tuple(name for name, _ in path) == wanted
    ]
    if len(matches) != 1:
        return None
    # Runs during an import are not counted: what they produced is state, and
    # the state diff reports that on its own.
    bits = 0
    for line in range(matches[0]["low"], matches[0]["high"] + 1):
        bits |= cov["rt"].get(line, 0)
    return bits


def _entry_readers(maps, job, key, value):
    """Tests that read an entry of a class-level dict that the patch changed.

    None when the dict cannot be compared entry by entry.
    """
    from br import basestate

    if os.environ.get("BR_NO_ENTRIES"):
        return None  # for checking entry-level selection against attribute-level
    before = maps.entries().get(key)
    recorded = maps.key_readers()
    if before is None or recorded is None or type(value) is not dict:
        return None
    after = basestate.dict_entries(value, job.translate)
    if after is None:
        return None
    bits = recorded["all"].get(key, 0)
    by_key = recorded["key"].get(key, {})
    for fingerprint, item in before.items():
        if after.get(fingerprint) != item:  # changed or removed
            for test_index in by_key.get(fingerprint, ()):
                bits |= 1 << test_index
    added = [fingerprint for fingerprint in after if fingerprint not in before]
    if added:
        bits |= recorded["miss"].get(key, 0)  # a lookup that used to find nothing
        for fingerprint in added:
            # ...or found it in a table built from this one and something else
            for test_index in by_key.get(fingerprint, ()):
                bits |= 1 << test_index
    if len(after) != len(before):
        bits |= recorded["len"].get(key, 0)
    return bits


def diff_candidates(job, maps, log, everything):
    """Import the patched tree and return the tests that read state it changed.

    Returns (readers of changed attributes, candidates found any other way,
    the changed attributes whose reads are recorded).
    """
    from br import basestate

    state = maps.state()
    if state.get("canary") != basestate.canary():
        raise RuntimeError("fingerprints were made by a different interpreter")
    base = state["fingerprints"]
    tracked = state["tracked"]
    scope = set()
    for module in job.diff_modules:
        scope.update(state["closure"].get(module, [module]))

    broken = {}
    if everything:
        loaded = basestate.import_all(broken)
    else:
        # Only what can hold state derived from the change; the rest is as imported.
        loaded = []
        for name in sorted(scope):
            if name.endswith("__main__"):
                continue
            try:
                importlib.import_module(name)
                loaded.append(name)
            except BaseException as error:  # noqa: BLE001
                broken[name] = repr(error)
    bits = 0
    full = 0
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
    present = set(loaded)
    changed = [
        key
        for key, value in base.items()
        if key[0] in scope and key[0] in present and current.get(key) != value
    ]
    changed += [key for key in current if key not in base and key not in ignored]
    # The statements that were edited name the cells they assign: count those as
    # changed whether or not the fingerprints agree.
    seen = set(changed)
    changed += [key for key in job.forced if key not in seen]
    job.changed_cells = list(changed)
    if not changed:
        if job.unnamed:
            log("class-level change with no visible effect: assuming the worst")
            full |= job.coarse
            job.changed_cells = None  # unknown
        return bits, full, targets

    readers = maps.cell_readers()
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
                    narrowed = _entry_readers(maps, job, key, value)
                    if narrowed is not None:
                        # Slices still go by who read the attribute at all.
                        found &= narrowed
            elif isinstance(owner, type):
                # A new attribute hides the one its bases provided.
                for parent in owner.__mro__[1:]:
                    parent_key = (parent.__module__, parent.__qualname__, attr)
                    if parent_key in tracked:
                        found = (found or 0) | readers.get(parent_key, 0)
                        tracked_key = parent_key
                        break
        if found is None:
            found = _function_runners(maps, state, module, qualname, attr)
        if found is None:
            if attr.startswith("__") and attr.endswith("__"):
                found = job.coarse
            else:
                if names is None:
                    names = maps.names()
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


def child(base, job, mode, policy, maps, write_fd, memory_bytes, log_fd=None, index=None):
    """Body of the forked verifier. Never returns."""
    dest = f"/tmp/br{os.getpid()}"
    started = time.process_time()

    def log(text):
        if log_fd is not None:
            try:
                os.write(log_fd, f"{job.commit}   {text}\n".encode())
            except OSError:
                pass

    try:
        os.write(write_fd, f"I {os.getpid()}\n".encode())
        try:
            resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        except (ValueError, OSError):
            pass
        if log_fd is not None:
            log_fd = os.dup(log_fd)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        sys.dont_write_bytecode = True

        from br import runner

        runner.install_inline_pools()
        inherited = len(runner._LAZY_LOADED)

        dual = None
        if mode == "hot":
            try:
                apply_hot(job, index)
                if DUAL and getattr(job, "dual", None) is not None:
                    from br import dual

                    dual.install()
                    for function in index.get(job.dual[:3], ()):
                        function.__code__ = job.dual[3]
            except Exception as error:  # noqa: BLE001
                log(f"hot swap refused ({error!r}): retrying cold")
                os.write(write_fd, b"X\n")
                return
            if job.reads_source:
                make_tree(base, dest, job.patched)
                redirect_packages(base, dest)
        else:
            make_tree(base, dest, job.patched)
            if mode == "leaf":
                state = maps.state()
                scope = set(job.modules)
                for module in job.modules | job.diff_modules:
                    scope.update(state["closure"].get(module, [module]))
                if any(name in sys.modules for name in scope):
                    log("a changed module is already imported: retrying cold")
                    os.write(write_fd, b"X\n")
                    return
                redirect_packages(base, dest)
            else:
                sys.path.insert(0, dest)
            os.chdir(dest)

        tracked_cells = ()
        if job.plan is not None:
            indexes, to_verify, keep, estimate, dropped, slices = job.plan
        else:
            bits = job.cands
            full = job.full
            cell_targets = set()
            diffed = False
            if job.diff_modules:
                diffed = True
                try:
                    found, found_full, cell_targets = diff_candidates(
                        job, maps, log, everything=mode != "leaf"
                    )
                    bits |= found | found_full
                    full |= found_full
                    tracked_cells = maps.state()["tracked"]
                except BaseException as error:  # noqa: BLE001
                    log(f"state diff failed ({error!r}); assuming the worst")
                    bits |= job.coarse
                    full |= job.coarse
            indexes = policy.indexes(bits)
            slices = plan_slices(
                maps, policy, indexes, full, job.targets, cell_targets, job.no_slice
            )
            to_verify, keep, estimate, dropped = policy.decide(
                indexes,
                mode=mode,
                slices=slices,
                sure=job.sure,
                droppable=bits & ~full if job.targets or cell_targets else 0,
                diffed=diffed,
            )
        dear = []
        cheap_count = len(to_verify)
        if dual is not None:
            cheap = [i for i in to_verify if slice_cost(slices, i, policy.cpu[i]) <= DUAL_CHEAP]
            dear = [i for i in to_verify if slice_cost(slices, i, policy.cpu[i]) > DUAL_CHEAP]
            if DUAL_AUDIT:
                # Measuring: also run what the plan would have scheduled unrun, bar the giants.
                extra = [i for i in keep if policy.ms[policy.to_number[i]] < 6000]
                keep = [i for i in keep if i not in set(extra)]
                dear += extra
            to_verify = cheap + dear
            cheap_count = len(cheap)
        lines = (
            [f"K {i}" for i in keep]
            + [f"E {i}" for i in dropped]
            + [f"V {i}" for i in to_verify]
            + ["A"]
        )
        os.write(write_fd, ("\n".join(lines) + "\n").encode())
        log(
            f"{mode}: {len(indexes)} candidates, verify {len(to_verify)} "
            f"(est {estimate:.1f}s), keep {len(keep)}, drop on evidence {len(dropped)}"
        )
        limits = {}
        cut = 0
        for i in to_verify:
            name = policy.tests[i]
            if i in slices:
                runner.SLICE[name] = slices[i][:3]
            part = slices.get(("s", i))
            if part is not None:
                record = maps.stmts(i)
                if record is not None:
                    runner.STMT[name] = (record, part[0])
                    cut += 1
            limits[name] = 3.0 + 4.0 * slice_cost(slices, i, policy.cpu[i])
            if DUAL_AUDIT or STMT_AUDIT:
                limits[name] = 900.0  # measuring on a busy machine: no false alarms
        if cut:
            log(f"{cut} tests cut down to the statements that reach the change")

        test_ids = [policy.tests[i] for i in to_verify]
        if any(name.startswith(HEAVY) for name in test_ids):
            runner.limit_duckdb_threads()
        index_of = dict(zip(test_ids, to_verify))
        # After a first failure a worker is needed anyway, and checking only pays
        # for tests that cost far less to run here than to schedule.
        state = {
            "stop": False,
            "keep_going": {
                policy.tests[i]
                for i in to_verify
                if slice_cost(slices, i, policy.cpu[i])
                <= max(0.01, CONTINUE_RATIO * policy.ms[policy.to_number[i]] / 1000)
            },
        }

        seen = {"tests": 0, "evals": 0, "differed": 0, "with_evals": 0, "last": (0, 0)}

        def settle():
            """Every cheap candidate has run: can the dear ones be left out?"""
            enough = (
                not state["stop"]
                and seen["differed"] == 0
                and seen["evals"] >= DUAL_MIN_EVALS
                and seen["with_evals"] >= DUAL_MIN_TESTS
            )
            log(
                f"old and new compared {seen['evals']} times in {seen['with_evals']} tests, "
                f"differed {seen['differed']} times: "
                f"{'leaving out' if enough else 'still checking'} {len(dear)} dear tests"
                f"{' and ' + str(len(keep)) + ' unrun ones' if enough and keep else ''}"
            )
            if DUAL_AUDIT_QUICK and not enough:
                state["stop"] = True
                state["keep_going"] = set()
            if not enough or DUAL_AUDIT:
                return
            os.write(write_fd, "".join(f"Q {i}\n" for i in dear + list(keep)).encode())
            state["stop"] = True
            state["keep_going"] = set()

        def report(test_id, status):
            if dual is not None:
                evals, differed = dual.COUNTS
                delta = (evals - seen["last"][0], differed - seen["last"][1])
                seen["last"] = (evals, differed)
                seen["evals"] += delta[0]
                seen["differed"] += delta[1]
                seen["with_evals"] += 1 if delta[0] else 0
                if DUAL_AUDIT:
                    i = index_of[test_id]
                    log(
                        f"audit {i} {'dear' if i in dear_set else 'cheap'} "
                        f"{slice_cost(slices, i, policy.cpu[i]):.3f} {delta[0]} {delta[1]} {status}"
                    )
            os.write(write_fd, f"{status} {index_of[test_id]}\n".encode())
            verdicts[test_id] = status
            if status != "P" and not DUAL_AUDIT and not STMT_AUDIT:
                # One failure settles that a worker is needed; the rest are scheduled.
                # A second one, or a test that hangs, says the change breaks things
                # broadly: checking more would only find more of the same.
                if state["stop"] or status == "T" or getattr(job, "probe", False):
                    state["keep_going"] = set()
                state["stop"] = True
            if DUAL_AUDIT_QUICK and seen["tests"] < cheap_count and (
                status != "P" or seen["differed"]
            ):
                state["stop"] = True  # the rule cannot apply to this job any more
                state["keep_going"] = set()
            seen["tests"] += 1
            if dual is not None and seen["tests"] == cheap_count and (dear or keep):
                settle()

        dear_set = set(dear)
        verdicts = {}

        # A test that passes without its class's setUpClass runs without it, but
        # only while the change cannot reach the setup: a setup that the patch
        # breaks fails every test of its class, and a test left out unrun is only
        # known to be unaffected if nothing it does outside its body can differ.
        # What each setup runs and reads was recorded when the maps were built.
        needy = set()
        setups = maps.setups()
        changed_cells = getattr(job, "changed_cells", None)
        for key in {policy.tests[i].rsplit("::", 1)[0] for i in to_verify}:
            if setups is None:
                needy.add(key)
                continue
            if key not in setups:
                continue  # a class without a setUpClass of its own
            watched = setups[key]
            if watched is None or job.no_slice or not job.targets and job.plan is not None:
                needy.add(key)  # not watched, or not a change to sqlglot's functions
                continue
            if job.plan is None or job.diff_modules:
                # A state diff: skip the setup only if every attribute that
                # changed is one whose reads are recorded and the setup read none.
                if changed_cells is None or any(
                    cell not in tracked_cells or cell in watched["reads"] for cell in changed_cells
                ):
                    needy.add(key)
                    continue
            for source, targets in job.targets.items():
                if not targets.isdisjoint(watched["lines"].get(source, ())):
                    needy.add(key)
                    break
        bare = {name for name in policy.bare if name.rsplit("::", 1)[0] not in needy}
        before_run = time.process_time()
        timings = {} if os.environ.get("BR_DEBUG") else None
        runner.run(test_ids, limits=limits, report=report, stop=state, timings=timings, bare=bare)
        if STMT_AUDIT and runner.STMT:
            cut_ids = [name for name in test_ids if name in runner.STMT and name in verdicts]
            runner.STMT.clear()
            whole = {}
            runner.run(
                cut_ids,
                limits={name: 600.0 for name in cut_ids},
                report=lambda name, status: whole.__setitem__(name, status),
                bare=bare,
            )
            for name in cut_ids:
                log(f"slice {index_of[name]} {verdicts[name]} {whole.get(name, '?')} {name}")
        os.write(write_fd, b"D\n")
        if timings:
            # Estimated against measured cost, dearest first: for tuning the cost model.
            rows = []
            for i in to_verify:
                name = policy.tests[i]
                if name not in timings:
                    continue
                guess = slice_cost(slices, i, policy.cpu[i])
                rows.append((timings[name], guess, policy.ms[policy.to_number[i]] / 1000, name))
            rows.sort(reverse=True)
            total = sum(row[0] for row in rows)
            log(
                f"measured {total:.2f}s for an estimated {sum(row[1] for row in rows):.2f}s, "
                f"recorded {sum(row[2] for row in rows):.2f}s"
            )
            for measured, guess, recorded, name in rows[:8]:
                log(
                    f"  {name.split('::')[-1][:44]:44s} measured {measured:6.2f} "
                    f"est {guess:5.2f} rec {recorded:5.2f}"
                )
        usage = resource.getrusage(resource.RUSAGE_SELF)
        loaded = [
            name for name in ("numpy", "duckdb", "pandas") if name in runner._LAZY_LOADED[inherited:]
        ]
        log(
            f"cpu: setup {before_run - started:.2f}s, tests "
            f"{time.process_time() - before_run:.2f}s, process total "
            f"{usage.ru_utime + usage.ru_stime:.2f}s since fork"
            f"{'; loaded here: ' + ', '.join(loaded) if loaded else ''}"
        )
    except BaseException as error:  # noqa: BLE001
        log(f"verifier died: {error!r}")
    finally:
        try:
            shutil.rmtree(dest, ignore_errors=True)
        finally:
            os._exit(0)


def serve(control_fd, jobs, launch):
    """Fork a verifier for every job number that arrives on the control pipe."""
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)
    buffer = b""
    while True:
        try:
            chunk = os.read(control_fd, 4096)
        except InterruptedError:
            continue
        except OSError:
            os._exit(0)
        if not chunk:
            os._exit(0)
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            job = jobs[int(line)]
            pid = os.fork()
            if pid == 0:
                signal.signal(signal.SIGCHLD, signal.SIG_DFL)
                for other in jobs:
                    if other is not job and other.write_fd is not None:
                        try:
                            os.close(other.write_fd)
                        except OSError:
                            pass
                try:
                    os.close(control_fd)
                except OSError:
                    pass
                launch(job)
                os._exit(0)
            try:
                os.close(job.write_fd)
            except OSError:
                pass
            job.write_fd = None


def core_server(base, jobs, policy, maps, memory_bytes, leaf_control, hot_control, test_modules):
    """Imports sqlglot the way a bare `import sqlglot` does, then serves leaf jobs.

    If there are hot jobs it first forks the server for those, which goes on to
    import everything.
    """
    try:
        sys.path.insert(0, base)
        os.chdir(base)
        sys.dont_write_bytecode = True
        import unittest  # noqa: F401  (sqlglot looks for it when it is imported)

        import sqlglot  # noqa: F401

        if any(job.mode == "hot" for job in jobs):
            pid = os.fork()
            if pid == 0:
                os.close(leaf_control)
                for job in jobs:
                    if job.mode != "hot" and job.write_fd is not None:
                        os.close(job.write_fd)
                        job.write_fd = None
                _full_server(base, jobs, policy, maps, memory_bytes, hot_control, test_modules)
                os._exit(0)
        os.close(hot_control)
        for job in jobs:
            if job.mode != "leaf" and job.write_fd is not None:
                os.close(job.write_fd)
                job.write_fd = None

        def launch(job):
            child(base, job, "leaf", policy, maps, job.write_fd, memory_bytes, log_fd=2)

        serve(leaf_control, jobs, launch)
    finally:
        os._exit(0)


def _full_server(base, jobs, policy, maps, memory_bytes, control, test_modules):
    try:
        from br import basestate, runner

        runner.install_inline_pools()
        basestate.import_all({})
        for name in test_modules:
            try:
                importlib.import_module(name)
            except BaseException:  # noqa: BLE001
                pass
        index = build_function_index(base)
        # Children share these objects; keeping the collector off them avoids
        # copying their pages and re-walking them in every child.
        gc.collect()
        gc.freeze()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        os.write(2, f"full server ready: {usage.ru_utime + usage.ru_stime:.2f}s cpu\n".encode())

        def launch(job):
            child(base, job, "hot", policy, maps, job.write_fd, memory_bytes, log_fd=2, index=index)

        serve(control, jobs, launch)
    finally:
        os._exit(0)
