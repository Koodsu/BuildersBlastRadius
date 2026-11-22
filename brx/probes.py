"""Cheap stand-ins for the four tests that are too slow to run while deciding.

Each probe computes a fingerprint under the patched tree; the selector compares it with
the fingerprint recorded at the pinned commit (maps/probes.json).

- tpcds / tpch (tests/test_executor.py): every query is parsed, pickled, optimized and
  planned exactly as the test does, but the row-by-row execution is cut off and the plan
  is fingerprinted instead, together with the SQL the test sends to duckdb. Same plan and
  same reference SQL give the same comparison, provided the patch touches no line that
  runs inside the execution itself (the selector checks that against maps/probes.json).
- max_nodes, generator_memory: the test's own steps at a smaller size. This one is a
  stand-in, not a proof.
"""

import hashlib
import pickle
import sys
import time

SKIP = {"parent", "dependents", "_hash"}


def canon(obj, memo=None, depth=0):
    if memo is None:
        memo = {}
    if obj is None or isinstance(obj, (bool, int, float, str, bytes)):
        return repr(obj)
    if isinstance(obj, type):
        return f"<class {obj.__module__}.{obj.__qualname__}>"
    key = id(obj)
    if key in memo:
        return f"<ref {memo[key]}>"
    memo[key] = len(memo)
    if isinstance(obj, (list, tuple)):
        return "[" + ",".join(canon(item, memo, depth + 1) for item in obj) + "]"
    if isinstance(obj, dict):
        return "{" + ",".join(canon(k, memo, depth + 1) + ":" + canon(v, memo, depth + 1) for k, v in obj.items()) + "}"
    if isinstance(obj, (set, frozenset)):
        return "{" + ",".join(sorted(canon(item, dict(memo), depth + 1) for item in obj)) + "}"
    if callable(obj) and hasattr(obj, "__qualname__"):
        return f"<fn {getattr(obj, '__module__', '')}.{obj.__qualname__}>"
    fields = {}
    if hasattr(obj, "__dict__"):
        fields.update(vars(obj))
    for klass in type(obj).__mro__:
        for slot in getattr(klass, "__slots__", ()):
            if slot not in fields and hasattr(obj, slot):
                fields[slot] = getattr(obj, slot)
    name = f"{type(obj).__module__}.{type(obj).__qualname__}"
    if not fields:
        text = repr(obj)
        return name if " at 0x" in text else f"{name}:{text}"
    return name + "(" + ",".join(f"{k}={canon(v, memo, depth + 1)}" for k, v in sorted(fields.items()) if k not in SKIP) + ")"


def digest(text):
    return hashlib.md5(text.encode("utf-8", "replace")).hexdigest()[:16]


class Captured(BaseException):
    pass


def tpc(which):
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table
    from tests import helpers

    schema = helpers.TPCH_SCHEMA if which == "tpch" else helpers.TPCDS_SCHEMA
    folder = "tpc-h" if which == "tpch" else "tpc-ds"
    tables = {table: Table(columns=columns, rows=[]) for table, columns in schema.items()}
    saved = PythonExecutor.execute

    def stub(self, plan):
        raise Captured(canon(plan))

    out = []
    try:
        PythonExecutor.execute = stub
        pairs = list(helpers.load_sql_fixture_pairs(f"optimizer/{folder}/{folder}.sql"))
        parsed = [(parse_one(sql), meta, sql) for meta, sql, _ in pairs]
        for expression, meta, sql in parsed:
            try:
                expression, meta = pickle.loads(pickle.dumps((expression, meta)))
                try:
                    # the steps of tests/test_executor.py::mp_execute
                    if not meta.get("execute"):
                        print_ = "none"
                    else:
                        execute(expression, schema=schema, tables={t.name: tables[t.name] for t in find_tables(expression)})
                        print_ = "returned"
                except Captured as captured:
                    print_ = captured.args[0] + "|" + transpile(sql, write="duckdb")[0]
            except Exception as error:
                print_ = f"error {type(error).__name__}: {error}"
            out.append(digest(print_))
    finally:
        PythonExecutor.execute = saved
    return out


def max_nodes():
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    seen = []

    def attempt(sql, **kwargs):
        try:
            seen.append("ok " + digest(canon(parse_one(sql, **kwargs))))
        except ParseError as error:
            seen.append(f"ParseError {error}"[:300])
        except Exception as error:
            seen.append(f"{type(error).__name__} {error}"[:300])

    attempt("SELECT 1", max_nodes=1_000_000)
    seen.append(str(isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select)))
    cols = [f"col{i}" for i in range(500)]
    attempt(f"SELECT {','.join(cols)} FROM t", max_nodes=1_000_000)
    expr = "x" + "=x" * 100
    sql = f"SELECT {','.join(expr for _ in range(120))}"
    attempt(sql, max_nodes=10_000)
    attempt(sql, max_nodes=500)
    attempt(sql, max_nodes=1_000_000)
    attempt("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
    attempt("SELECT 1, 2, 3")
    return [digest("\n".join(seen))]


def generator_memory():
    import gc
    import tracemalloc

    from sqlglot.dialects.dialect import Dialect, Dialects

    seen = []
    for name in Dialects:
        try:
            dialect = Dialect.get_or_raise(name)
            for _ in range(5):
                dialect.generator()
            gc.collect()
            tracemalloc.start()
            try:
                before = tracemalloc.get_traced_memory()[0]
                for _ in range(60):
                    dialect.generator()
                gc.collect()
                after = tracemalloc.get_traced_memory()[0]
            finally:
                tracemalloc.stop()
            seen.append(f"{name} {after - before < 1024}")
        except Exception as error:
            seen.append(f"{name} {type(error).__name__}")
    return [digest("\n".join(str(s) for s in seen))]


PROBES = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": lambda: tpc("tpcds"),
    "tests/test_executor.py::TestExecutor::test_execute_tpch": lambda: tpc("tpch"),
    "tests/test_parser.py::TestParser::test_max_nodes": max_nodes,
    "tests/test_generator.py::TestGenerator::test_generator_memory": generator_memory,
}


def run(names, emit, expected=None):
    """emit(name, same_as_expected, cpu, fingerprint)."""
    sys.setrecursionlimit(max(sys.getrecursionlimit(), 20000))
    for name in names:
        started = time.process_time()
        try:
            found = PROBES[name]()
        except BaseException as error:  # a probe that cannot run says "different"
            found = [f"probe error {type(error).__name__}: {error}"[:200]]
        same = expected is not None and expected.get(name) == found
        emit(name, same, time.process_time() - started, found)
