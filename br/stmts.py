"""Statement-level slices of expensive tests.

A sqlglot test method is mostly a long run of independent checks: hundreds of
`self.validate_identity(...)` lines, or blocks that build an expression and
assert on it. A change to one function is reached by a handful of them. So an
expensive test is split into *units*: groups of its top-level statements that
share no local name with any other group. At build time every unit is run on
its own, in a fresh fork, and the lines it executes are recorded; at run time
only the units that reach a changed line are executed.

A unit that fails in the sliced run counts as a failure of the test, so a slice
that turns out not to stand alone costs a scheduled test, never a miss.

Build:   python -m br.stmts /sg /app/maps [jobs]
Writes:  <maps>/stmts/<test index>.pkl
"""

from __future__ import annotations

import ast
import hashlib
import os
import pickle
import sys
import time

MIN_CPU = 0.03  # tests cheaper than this are run whole
MAX_UNITS = 900


class NotSliceable(Exception):
    pass


def _function_node(tree, class_name, method, first_line):
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef,)) and item.name == method:
                    return item
    # Inherited: find it by where its code says it starts.
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == method:
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            if start == first_line:
                return node
    return None


def _span(node):
    start = node.lineno
    for decorator in getattr(node, "decorator_list", ()):
        start = min(start, decorator.lineno)
    return start, node.end_lineno


class _Names(ast.NodeVisitor):
    """Names a statement mentions, and what it does that reaches beyond itself."""

    def __init__(self):
        self.names = set()  # every ast.Name id
        self.bound = set()  # names it binds or deletes
        self.self_attrs = set()  # attributes of self it mentions
        self.self_stores = False  # assigns to or deletes an attribute of self
        self.outside_store = False  # assigns into an object it did not make
        self.flow = False  # return, yield, global, nonlocal, await
        self.raises = False

    def visit_Name(self, node):
        self.names.add(node.id)
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bound.add(node.id)

    def _root(self, node):
        while isinstance(node, (ast.Attribute, ast.Subscript, ast.Starred)):
            node = node.value
        return node

    def _target(self, target):
        if isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self._target(item)
            return
        if isinstance(target, ast.Starred):
            self._target(target.value)
            return
        if isinstance(target, (ast.Attribute, ast.Subscript)):
            root = self._root(target)
            if isinstance(root, ast.Name) and root.id == "self":
                self.self_stores = True
            else:
                # Stores into a local object are tied to it by name; anything
                # else (a module, a class, a call's result) outlives the statement.
                self.outside_store = True
                if isinstance(root, ast.Name):
                    self.stored_roots.add(root.id)

    stored_roots: set

    def visit_Assign(self, node):
        for target in node.targets:
            self._target(target)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node):
        for target in node.targets:
            self._target(target)
        self.generic_visit(node)

    def visit_Attribute(self, node):
        if isinstance(node.value, ast.Name) and node.value.id == "self":
            self.self_attrs.add(node.attr)
        self.generic_visit(node)

    def visit_Return(self, node):
        self.flow = True
        self.generic_visit(node)

    visit_Yield = visit_YieldFrom = visit_Await = visit_Global = visit_Nonlocal = visit_Return

    def visit_Raise(self, node):
        self.raises = True
        self.generic_visit(node)

    def visit_Import(self, node):
        for alias in node.names:
            self.bound.add((alias.asname or alias.name).split(".")[0])
            self.names.add((alias.asname or alias.name).split(".")[0])

    visit_ImportFrom = visit_Import

    def visit_FunctionDef(self, node):
        self.bound.add(node.name)
        self.names.add(node.name)
        # A return inside a nested function is that function's business.
        inner = _Names()
        inner.stored_roots = set()
        for child in ast.iter_child_nodes(node):
            inner.visit(child)
        self.names |= inner.names
        self.self_attrs |= inner.self_attrs
        self.self_stores |= inner.self_stores
        self.outside_store |= inner.outside_store
        self.stored_roots |= inner.stored_roots

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self.visit_FunctionDef_like(node)

    def visit_FunctionDef_like(self, node):
        inner = _Names()
        inner.stored_roots = set()
        for child in ast.iter_child_nodes(node):
            inner.visit(child)
        self.names |= inner.names
        self.self_attrs |= inner.self_attrs
        self.self_stores |= inner.self_stores
        self.outside_store |= inner.outside_store
        self.stored_roots |= inner.stored_roots

    def visit_ClassDef(self, node):
        self.bound.add(node.name)
        self.names.add(node.name)
        self.visit_FunctionDef_like(node)

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bound.add(node.name)
            self.names.add(node.name)
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.bound.add(node.name)
            self.names.add(node.name)
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs


def _describe(statement):
    names = _Names()
    names.stored_roots = set()
    names.visit(statement)
    return names


# Calls through these names say something about the rest of the test, not only
# about the statement they are in.
_SKIPPERS = ("skipTest", "SkipTest", "skip", "fail")


def analyze(source_text, class_name, method, first_line, klass=None):
    """Split a test method into units.

    Returns a dict with the method's extent, its top-level statements, the
    statements every run keeps (`always`) and the units (lists of statement
    indexes). Raises NotSliceable when it cannot be done safely.
    """
    tree = ast.parse(source_text)
    node = _function_node(tree, class_name, method, first_line)
    if node is None:
        raise NotSliceable("method not found")
    if isinstance(node, ast.AsyncFunctionDef):
        raise NotSliceable("async")
    body = node.body
    if len(body) < 3:
        raise NotSliceable("too few statements")
    spans = [_span(statement) for statement in body]
    for (_, end), (start, _) in zip(spans, spans[1:]):
        if start <= end:
            raise NotSliceable("two statements on a line")
    if spans[0][0] <= node.lineno:
        raise NotSliceable("body on the def line")

    arguments = {a.arg for a in node.args.args + node.args.kwonlyargs + node.args.posonlyargs}
    if node.args.vararg:
        arguments.add(node.args.vararg.arg)
    if node.args.kwarg:
        arguments.add(node.args.kwarg.arg)
    arguments.discard("self")

    described = [_describe(statement) for statement in body]
    local = set(arguments)
    for info in described:
        local |= info.bound
    local.discard("self")

    def is_data(attr):
        """An attribute of self that holds data rather than a method."""
        if klass is None:
            return True
        value = getattr(klass, attr, None)
        return not callable(value)

    always = set()
    keys = []  # per statement: the names that tie it to others
    for index, (statement, info) in enumerate(zip(body, described)):
        if info.flow:
            raise NotSliceable("return, yield or global in the body")
        tied = {name for name in info.names if name in local}
        tied |= {"self." + attr for attr in info.self_attrs if is_data(attr)}
        keys.append(tied)
        pinned = False
        if info.self_stores or info.raises:
            pinned = True
        if info.outside_store and any(root not in local for root in info.stored_roots or {""}):
            pinned = True
        if info.outside_store and not info.stored_roots:
            pinned = True
        for inner in ast.walk(statement):
            if isinstance(inner, ast.Attribute) and inner.attr in _SKIPPERS:
                pinned = True
            if isinstance(inner, ast.Name) and inner.id in ("SkipTest",):
                pinned = True
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
            pinned = True  # the docstring
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            # A bare call on something that is neither self nor a local: its
            # only point is an effect, and the effect may be on shared state.
            root = statement.value.func
            while isinstance(root, (ast.Attribute, ast.Subscript, ast.Call)):
                root = root.func if isinstance(root, ast.Call) else root.value
            if not (isinstance(root, ast.Name) and (root.id == "self" or root.id in local)):
                pinned = True
        if isinstance(statement, (ast.Import, ast.ImportFrom, ast.Pass)):
            pinned = True
        if pinned:
            always.add(index)

    # Statements that share a name are one group.
    parent = list(range(len(body)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    owner = {}
    for index, tied in enumerate(keys):
        for name in tied:
            if name in owner:
                a, b = find(owner[name]), find(index)
                if a != b:
                    parent[b] = a
            else:
                owner[name] = index
    groups = {}
    for index in range(len(body)):
        groups.setdefault(find(index), []).append(index)
    units = []
    for members in groups.values():
        if any(index in always for index in members):
            always.update(members)
        else:
            units.append(members)
    units.sort()
    if len(units) < 2:
        raise NotSliceable("one unit")
    if len(units) > MAX_UNITS:
        raise NotSliceable("too many units")

    lines = source_text.split("\n")
    text = "\n".join(lines[node.lineno - 1 : node.end_lineno])
    return {
        "def_line": node.lineno,
        "end_line": node.end_lineno,
        "first_line": min([node.lineno] + [d.lineno for d in node.decorator_list]),
        "digest": hashlib.sha1(text.encode()).hexdigest(),
        "stmts": [(start, end, statement.col_offset) for (start, end), statement in zip(spans, body)],
        "always": sorted(always),
        "units": units,
    }


def sliced_code(source_lines, record, keep_units):
    """Code object of the method with only `always` and the units in `keep_units` (a bitset)."""
    keep = set(record["always"])
    for number, members in enumerate(record["units"]):
        if keep_units >> number & 1:
            keep.update(members)
    first, last = record["def_line"], record["end_line"]
    out = list(source_lines[first - 1 : last])
    for index, (start, end, indent) in enumerate(record["stmts"]):
        if index in keep:
            continue
        out[start - first] = " " * indent + "pass"
        for line in range(start + 1, end + 1):
            out[line - first] = ""
    source = "\n" * (first - 2) + "if 1:\n" + "\n".join(out) + "\n"
    module = compile(source, record.get("path", "<slice>"), "exec", dont_inherit=True)
    stack = [module]
    while stack:
        code = stack.pop()
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                if const.co_name == record["name"] and const.co_firstlineno == first:
                    return const
                stack.append(const)
    raise NotSliceable("sliced method not found after compiling")


def target_function(klass, method):
    """The plain function behind a test method, under any decorators."""
    import inspect

    value = getattr(klass, method)
    value = getattr(value, "__func__", value)
    return inspect.unwrap(value)


_SOURCES: dict = {}


def source_lines(path):
    lines = _SOURCES.get(path)
    if lines is None:
        with open(path, encoding="utf-8") as handle:
            lines = _SOURCES[path] = handle.read().split("\n")
    return lines


def install(klass, method, record, keep_units, path):
    """Swap a sliced body into a test method. Returns (function, original code)."""
    function = target_function(klass, method)
    original = function.__code__
    if original.co_freevars or original.co_firstlineno != record["first_line"]:
        raise NotSliceable("not the function that was analysed")
    lines = source_lines(path)
    text = "\n".join(lines[record["def_line"] - 1 : record["end_line"]])
    if hashlib.sha1(text.encode()).hexdigest() != record["digest"]:
        raise NotSliceable("the test's source is not the one that was analysed")
    record = dict(record, name=method, path=path)
    code = sliced_code(lines, record, keep_units)
    if code.co_freevars or code.co_argcount != original.co_argcount:
        raise NotSliceable("sliced method has a different shape")
    function.__code__ = code
    return function, original


# --------------------------------------------------------------------------
# Build time
# --------------------------------------------------------------------------


def _attribute(tracer, test_code, record):
    """Make the tracer credit lines to the unit whose statement is running.

    Returns (per_unit, finish): per_unit maps a unit number (-1 for what every
    run keeps) to {source: lines}; finish() must be called after the run.
    """
    unit_of = {}
    for number, members in enumerate(record["units"]):
        for index in members:
            unit_of[index] = number
    line_unit = {}
    for index, (start, end, _) in enumerate(record["stmts"]):
        for line in range(start, end + 1):
            line_unit[line] = unit_of.get(index, -1)
    state = {"unit": -1}
    per_unit = {}
    prefix = len(tracer.SGP)

    def flush():
        target = per_unit.setdefault(state["unit"], {})
        for mapping in (tracer.RT, tracer.IM):
            for cid, lines in mapping.items():
                if lines:
                    name = tracer.CODES[cid].co_filename[prefix:]
                    target.setdefault(name, set()).update(lines)
            mapping.clear()

    def on_line(code, line):
        if code is test_code:
            unit = line_unit.get(line)
            if unit is not None and unit != state["unit"]:
                flush()
                state["unit"] = unit
                tracer.MON.restart_events()
            return None
        return tracer._on_line(code, line)

    tracer.MON.register_callback(tracer.TOOL, tracer.MON.events.LINE, on_line)

    def finish():
        flush()
        tracer.MON.register_callback(tracer.TOOL, tracer.MON.events.LINE, tracer._on_line)

    return per_unit, finish


def _run_slice(test_id, klass, method, record, keep, path, out_path, token_write):
    """In a forked child: run the test with the units in `keep` (None: all of it)."""
    from br import runner, tracer

    status = 1
    try:
        function = target_function(klass, method)
        if keep is not None:
            install(klass, method, record, keep, path)
        tracer.RT.clear()
        tracer.IM.clear()
        tracer.MON.restart_events()
        per_unit, finish = _attribute(tracer, function.__code__, record)
        pool = {}
        started = time.process_time()
        failed, _, errors = runner.run([test_id], limits={test_id: 900.0}, units=pool)
        cpu = time.process_time() - started
        finish()
        with open(out_path, "wb") as handle:
            pickle.dump(
                {
                    "failed": bool(failed) or bool(errors),
                    "cpu": cpu,
                    "lines": per_unit,
                    "pool": pool.get(test_id, 0),
                },
                handle,
                protocol=pickle.HIGHEST_PROTOCOL,
            )
        status = 0
    except BaseException as error:  # noqa: BLE001
        sys.stderr.write(f"stmts: {test_id} keep={keep} crashed: {error!r}\n")
    finally:
        try:
            os.write(token_write, b"x")
        finally:
            os._exit(status)


def _zygote(root, rel_path, entries, out_dir, token_read, token_write):
    """One per test file: import everything once, then fork a child for every run."""
    import importlib

    from br import basestate, runner, tracer

    tracer._install()
    module_name = rel_path[:-3].replace("/", ".")
    try:
        # The verifier's servers have every sqlglot module imported: so does this.
        basestate.import_all({})
        module = importlib.import_module(module_name)
    except BaseException as error:  # noqa: BLE001
        sys.stderr.write(f"stmts: cannot import {module_name}: {error!r}\n")
        os._exit(3)
    scratch = os.path.join(out_dir, "stmt_parts", module_name)
    os.makedirs(scratch, exist_ok=True)

    plans = []
    running = {}

    def reap(block):
        while running:
            try:
                pid, status = os.waitpid(-1, 0 if block else os.WNOHANG)
            except ChildProcessError:
                running.clear()
                return
            if pid == 0:
                return
            if running.pop(pid, None) is not None and os.WIFSIGNALED(status):
                os.write(token_write, b"x")  # it died before giving its token back
            if block:
                return

    for test_index, test_id, plain in entries:
        _, cls, method = runner.split_id(test_id)
        try:
            klass = getattr(module, cls)
            function = target_function(klass, method)
            path = function.__code__.co_filename
            if function.__code__.co_freevars or not path.startswith(root + "/"):
                raise NotSliceable("closure or foreign file")
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            record = analyze(text, cls, method, function.__code__.co_firstlineno, klass)
            original = function.__code__
            try:
                install(klass, method, record, 0, path)  # it must at least compile
            finally:
                function.__code__ = original
        except NotSliceable:
            continue
        except Exception as error:  # noqa: BLE001
            sys.stderr.write(f"stmts: {test_id} not analysed: {error!r}\n")
            continue
        record["id"] = test_id
        record["file"] = path[len(root) + 1 :]
        record["plain"] = plain
        count = len(record["units"])
        outs = {}
        for keep in [None, 0] + [1 << number for number in range(count)]:
            name = "all" if keep is None else str(keep.bit_length())
            out_path = os.path.join(scratch, f"{test_index}.{name}.pkl")
            outs[keep] = out_path
            os.read(token_read, 1)
            pid = os.fork()
            if pid == 0:
                _run_slice(test_id, klass, method, record, keep, path, out_path, token_write)
            running[pid] = True
            reap(False)
        plans.append((test_index, record, outs))
    while running:
        reap(True)

    os.makedirs(os.path.join(out_dir, "stmts"), exist_ok=True)
    for test_index, record, outs in plans:
        count = len(record["units"])
        always_bit = 1 << count
        results = {}
        bad = None
        for keep, out_path in outs.items():
            try:
                with open(out_path, "rb") as handle:
                    results[keep] = pickle.load(handle)
                os.remove(out_path)
            except OSError:
                bad = f"run {keep} left nothing"
                break
            if results[keep]["failed"]:
                bad = f"run {keep} failed"
                break
        if bad:
            sys.stderr.write(f"stmts: {record['id']} not sliceable: {bad}\n")
            continue
        lines = {}
        for keep, result in results.items():
            for unit, by_source in result["lines"].items():
                bit = always_bit if unit < 0 else 1 << unit
                for source, hit in by_source.items():
                    target = lines.setdefault(source, {})
                    for line in hit:
                        target[line] = target.get(line, 0) | bit
        whole = results[None]["cpu"]
        base = results[0]["cpu"]
        record["lines"] = lines
        record["traced"] = whole
        record["always_cpu"] = base
        record["cost"] = [
            max(0.0, results[1 << number]["cpu"] - base) for number in range(count)
        ]
        # Units that hand work to the process pool stay in every run, so the
        # numbering of that work is the one the pool's own slices were planned for.
        record["pool"] = sum(
            1 << number for number in range(count) if results[1 << number]["pool"]
        )
        with open(os.path.join(out_dir, "stmts", f"{test_index}.pkl"), "wb") as handle:
            pickle.dump(record, handle, protocol=pickle.HIGHEST_PROTOCOL)
    os._exit(0)


def main(argv):
    from br import runner, tracer

    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    jobs = int(argv[3]) if len(argv) > 3 else (os.cpu_count() or 2)
    tracer.SG = root
    tracer.SGP = root + "/"
    os.environ.setdefault("SKIP_INTEGRATION", "0")
    sys.path.insert(0, root)
    os.chdir(root)
    with open(os.path.join(out_dir, "meta.pkl"), "rb") as handle:
        meta = pickle.load(handle)
    plain = meta.get("cpu_plain") or [None] * len(meta["tests"])
    by_file = {}
    only = os.environ.get("BR_ONLY")
    for index, test_id in enumerate(meta["tests"]):
        cost = plain[index] if plain[index] is not None else meta["cpu_test"][index]
        if cost < MIN_CPU or index in set(meta["base_failed"]):
            continue
        if only and not any(part in test_id for part in only.split(",")):
            continue
        by_file.setdefault(test_id.split("::", 1)[0], []).append((index, test_id, cost))

    import duckdb  # noqa: F401
    import numpy  # noqa: F401
    import pandas  # noqa: F401
    from pandas.testing import assert_frame_equal  # noqa: F401

    runner.install_inline_pools()
    runner.limit_duckdb_threads()
    token_read, token_write = os.pipe()
    os.write(token_write, b"x" * jobs)
    zygotes = {}
    for rel_path, entries in sorted(by_file.items()):
        pid = os.fork()
        if pid == 0:
            _zygote(root, rel_path, entries, out_dir, token_read, token_write)
        zygotes[pid] = rel_path
    failures = 0
    while zygotes:
        pid, status = os.waitpid(-1, 0)
        rel_path = zygotes.pop(pid, None)
        if rel_path is not None and status != 0:
            failures += 1
            sys.stderr.write(f"stmts: zygote for {rel_path} exited with {status}\n")
    made = len(os.listdir(os.path.join(out_dir, "stmts"))) if os.path.isdir(os.path.join(out_dir, "stmts")) else 0
    sys.stderr.write(
        f"stmts: {sum(len(v) for v in by_file.values())} tests considered in {len(by_file)} "
        f"files, {made} sliceable, {failures} zygote failures\n"
    )


if __name__ == "__main__":
    main(sys.argv)
