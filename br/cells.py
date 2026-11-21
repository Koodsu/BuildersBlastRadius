"""Import-time state of sqlglot: what it is, who reads it, and how to compare it.

A "cell" is one module global or one class attribute. Two things are built
from the pinned commit:

* which tests read each class attribute at run time (a descriptor records the
  first read in a process and then gets out of the way), and
* a fingerprint of every cell's value.

At run time a patched tree is imported, fingerprinted the same way, and the
cells that differ say which tests can notice a change to class-level or
module-level code.
"""

from __future__ import annotations

import enum
import functools
import re
import sys
import types

from br import pyindex

SKIP_CLASS = {
    "__module__",
    "__qualname__",
    "__doc__",
    "__dict__",
    "__weakref__",
    "__firstlineno__",
    "__static_attributes__",
    "__annotations__",
    "__annotate__",
    "__parameters__",
    "__orig_bases__",
    "__abstractmethods__",
    "__classcell__",
    "_abc_impl",
}
SKIP_MODULE = {
    "__name__",
    "__doc__",
    "__package__",
    "__loader__",
    "__spec__",
    "__file__",
    "__cached__",
    "__builtins__",
    "__path__",
    "__annotations__",
    "__annotate__",
    "__warningregistry__",
}
OPAQUE_MODULES = ("logging", "threading", "_thread", "weakref", "_weakref", "abc", "_abc", "typing")

# ---------------------------------------------------------------- read tracking

READS: set = set()
PAUSED = [0]  # non-zero while a module is being imported: those reads are not recorded
_INSTRUMENTED: set = set()
_CELLS: list = []
TRIGGERED: list = []  # cells read since the last rearm()


def rearm():
    """Put back the cells that were read, so the next stretch of work records its own reads."""
    for cell in TRIGGERED:
        try:
            setattr(cell.owner, cell.name, cell)
        except Exception:  # noqa: BLE001
            pass
    TRIGGERED.clear()


class Cell:
    """Stands in for a class attribute until its first run-time read."""

    __slots__ = ("owner", "name", "value", "key")

    def __init__(self, owner, name, value, key):
        self.owner = owner
        self.name = name
        self.value = value
        self.key = key

    def __get__(self, obj, objtype=None):
        if not PAUSED[0]:
            READS.add(self.key)
            try:
                setattr(self.owner, self.name, self.value)
                TRIGGERED.append(self)
            except Exception:  # noqa: BLE001
                pass
        return self.value


def classes_of(module):
    """Classes defined in a module, including classes nested in them."""
    name = module.__name__
    found = []
    seen = set()
    stack = [value for value in vars(module).values() if isinstance(value, type)]
    while stack:
        klass = stack.pop()
        if id(klass) in seen or getattr(klass, "__module__", None) != name:
            continue
        seen.add(id(klass))
        found.append(klass)
        for value in list(vars(klass).values()):
            if isinstance(value, Cell):
                value = value.value
            if isinstance(value, type):
                stack.append(value)
    return found


def trackable(name, value):
    if name in SKIP_CLASS or (name.startswith("__") and name.endswith("__")):
        return False
    return not hasattr(type(value), "__get__")


def instrument(module):
    for klass in classes_of(module):
        if isinstance(klass, enum.EnumMeta):
            continue
        for name, value in list(vars(klass).items()):
            if isinstance(value, Cell) or not trackable(name, value):
                continue
            key = (module.__name__, klass.__qualname__, name)
            cell = Cell(klass, name, value, key)
            try:
                setattr(klass, name, cell)
            except Exception:  # noqa: BLE001
                continue
            _CELLS.append(cell)


def instrument_loaded(prefix="sqlglot"):
    for name, module in list(sys.modules.items()):
        if module is None or name in _INSTRUMENTED:
            continue
        if name == prefix or name.startswith(prefix + "."):
            _INSTRUMENTED.add(name)
            instrument(module)


def restore_all():
    for cell in _CELLS:
        try:
            if vars(cell.owner).get(cell.name) is cell:
                setattr(cell.owner, cell.name, cell.value)
        except Exception:  # noqa: BLE001
            pass
    _CELLS.clear()
    TRIGGERED.clear()


# ---------------------------------------------------------------- fingerprints

_SCALARS = (int, float, bool, str, bytes, complex, type(None))
_BUILTIN_CALLABLES = (
    types.BuiltinFunctionType,
    types.MethodDescriptorType,
    types.WrapperDescriptorType,
    types.MethodWrapperType,
    types.GetSetDescriptorType,
    types.MemberDescriptorType,
    types.ClassMethodDescriptorType,
)
_MISSING = object()


class Snapshot:
    """Fingerprints values. Needs a fixed PYTHONHASHSEED to compare across processes."""

    def __init__(self, translate=None, loose=False):
        self.loose = loose  # sequences compared as bags: for values built from unordered sets
        self.memo = {}
        self.keep = []
        self.active = set()
        self.translate = translate or {}
        self.code_memo = {}
        self.opaque = 0

    def code(self, code):
        digest = self.code_memo.get(id(code))
        if digest is None:
            digest = pyindex.own_digest(code)
            digest = self.translate.get(digest, digest)
            self.code_memo[id(code)] = digest
            self.keep.append(code)
        return digest

    def fp(self, value):
        kind = type(value)
        if kind in _SCALARS:
            if kind is str or kind is bytes:
                return hash((kind.__name__, value))
            # repr, because hash(-1) == hash(-2) and nan hashes by identity.
            return hash((kind.__name__, repr(value)))
        key = id(value)
        known = self.memo.get(key)
        if known is not None:
            return known
        if key in self.active:
            return hash(("cycle", kind.__qualname__))
        self.active.add(key)
        try:
            result = self._fp(value, kind)
        except RecursionError:
            raise
        except Exception:  # noqa: BLE001
            self.opaque += 1
            result = hash(("opaque", kind.__module__, kind.__qualname__))
        finally:
            self.active.discard(key)
        self.memo[key] = result
        self.keep.append(value)
        return result

    def _fp(self, value, kind):
        fp = self.fp
        if kind is dict:
            return hash(("d", frozenset((fp(k), fp(v)) for k, v in value.items())))
        if kind is list or kind is tuple:
            if self.loose:
                return hash((kind.__name__, tuple(sorted(fp(item) for item in value))))
            return hash((kind.__name__, tuple(fp(item) for item in value)))
        if kind is set or kind is frozenset:
            return hash(("s", frozenset(fp(item) for item in value)))
        if kind is types.FunctionType:
            closure = value.__closure__ or ()
            cells = []
            for cell in closure:
                try:
                    cells.append(fp(cell.cell_contents))
                except ValueError:
                    cells.append(0)
            return hash(
                (
                    "fn",
                    value.__module__,
                    value.__qualname__,
                    self.code(value.__code__),
                    fp(value.__defaults__),
                    fp(value.__kwdefaults__),
                    tuple(cells),
                )
            )
        if isinstance(value, type):
            return hash(("cls", value.__module__, value.__qualname__))
        if kind is types.ModuleType:
            return hash(("mod", value.__name__))
        if kind in _BUILTIN_CALLABLES:
            return hash(("builtin", getattr(value, "__qualname__", kind.__name__)))
        if kind is types.MethodType:
            return hash(("meth", fp(value.__self__), fp(value.__func__)))
        if kind is staticmethod or kind is classmethod:
            return hash((kind.__name__, fp(value.__func__)))
        if kind is property:
            return hash(("prop", fp(value.fget), fp(value.fset), fp(value.fdel)))
        if kind is functools.partial:
            return hash(("partial", fp(value.func), fp(value.args), fp(value.keywords)))
        if isinstance(value, enum.Enum):
            return hash(("enum", kind.__module__, kind.__qualname__, value.name))
        if kind is re.Pattern:
            return hash(("re", value.pattern, value.flags))
        if kind is types.CodeType:
            return hash(("code", self.code(value)))
        if kind is types.MappingProxyType:
            return hash(("d", frozenset((fp(k), fp(v)) for k, v in value.items())))
        if kind is Cell:
            return fp(value.value)
        module = kind.__module__ or ""
        if module.split(".", 1)[0] in OPAQUE_MODULES:
            self.opaque += 1
            return hash(("opaque", module, kind.__qualname__, getattr(value, "name", None)))

        parts = [("obj", module, kind.__qualname__)]
        if isinstance(value, dict):
            parts.append(frozenset((fp(k), fp(v)) for k, v in value.items()))
        elif isinstance(value, (list, tuple)):
            parts.append(tuple(fp(item) for item in value))
        elif isinstance(value, (set, frozenset)):
            parts.append(frozenset(fp(item) for item in value))
        state = getattr(value, "__dict__", None)
        found = state is not None
        if isinstance(state, dict):
            parts.append(frozenset((name, fp(item)) for name, item in state.items()))
        for klass in kind.__mro__:
            slots = klass.__dict__.get("__slots__")
            if not slots:
                continue
            if isinstance(slots, str):
                slots = (slots,)
            for slot in slots:
                if slot in ("__dict__", "__weakref__"):
                    continue
                found = True
                item = getattr(value, slot, _MISSING)
                parts.append((slot, 0 if item is _MISSING else fp(item)))
        if len(parts) == 1 and not found:
            self.opaque += 1
            try:
                parts.append(repr(value) if kind.__repr__ is not object.__repr__ else "")
            except Exception:  # noqa: BLE001
                pass
        return hash(tuple(parts))

    def module(self, module, out, only=None):
        name = module.__name__
        for attr, value in list(vars(module).items()):
            if attr in SKIP_MODULE:
                continue
            if only is None or (name, None, attr) in only:
                out[(name, None, attr)] = self.fp(value)
        for klass in classes_of(module):
            qualname = klass.__qualname__
            out[(name, qualname, "__bases__")] = hash(
                (
                    tuple((base.__module__, base.__qualname__) for base in klass.__bases__),
                    type(klass).__qualname__,
                )
            )
            for attr, value in list(vars(klass).items()):
                if attr in SKIP_CLASS:
                    continue
                if only is not None and (name, qualname, attr) not in only:
                    continue
                if isinstance(value, Cell):
                    value = value.value
                if isinstance(value, enum.Enum) and isinstance(value, klass):
                    out[(name, qualname, attr)] = hash(("member", attr, self.fp(value._value_)))
                else:
                    out[(name, qualname, attr)] = self.fp(value)


def snapshot(module_names, translate=None, loose=()):
    """{(module, class qualname or None, attribute): fingerprint} for loaded modules.

    Cells named in `loose` are fingerprinted with sequences treated as bags.
    """
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(limit, 20000))
    try:
        snap = Snapshot(translate)
        out = {}
        modules = [sys.modules[name] for name in module_names if sys.modules.get(name)]
        for module in modules:
            snap.module(module, out)
        if loose:
            relaxed = Snapshot(translate, loose=True)
            wanted = set(loose)
            names = {key[0] for key in wanted}
            for module in modules:
                if module.__name__ in names:
                    relaxed.module(module, out, only=wanted)
        return out, snap.opaque
    finally:
        sys.setrecursionlimit(limit)


# ---------------------------------------------------------------- static indexes

_NAME_OPS = {
    "LOAD_GLOBAL",
    "LOAD_NAME",
    "LOAD_ATTR",
    "LOAD_METHOD",
    "LOAD_SUPER_ATTR",
    "IMPORT_FROM",
    "IMPORT_NAME",
    "LOAD_DEREF",
    "LOAD_CLASSDEREF",
    "LOAD_FROM_DICT_OR_GLOBALS",
    "LOAD_FROM_DICT_OR_DEREF",
}


def name_lines(code, out=None):
    """{name: set(lines)}: where a module's code reads a global or attribute by name.

    String constants count too, for getattr(x, "NAME") style reads.
    """
    import dis

    if out is None:
        out = {}
    stack = [code]
    while stack:
        current = stack.pop()
        line = current.co_firstlineno
        for instruction in dis.get_instructions(current):
            if instruction.line_number is not None:
                line = instruction.line_number
            if instruction.opname in _NAME_OPS:
                name = instruction.argval
                if isinstance(name, str):
                    out.setdefault(name, set()).add(line)
            elif instruction.opname == "LOAD_CONST":
                value = instruction.argval
                if isinstance(value, str) and value.isidentifier():
                    out.setdefault(value, set()).add(line)
        for const in current.co_consts:
            if isinstance(const, types.CodeType):
                stack.append(const)
    return out


def imports_of(source, module_name, is_package, known):
    """Module names (from `known`) that a module imports while it is being imported.

    Imports inside functions and under `if TYPE_CHECKING:` do not run at import time.
    """
    import ast

    found = set()
    package = module_name if is_package else module_name.rpartition(".")[0]

    def add(name):
        while name:
            if name in known:
                found.add(name)
            name = name.rpartition(".")[0]

    def type_checking(test):
        return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
            isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
        )

    def visit(nodes):
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    if node.level > 1:
                        parts = parts[: len(parts) - (node.level - 1)]
                    base = ".".join(parts + ([base] if base else []))
                add(base)
                for alias in node.names:
                    add(f"{base}.{alias.name}")
            elif isinstance(node, ast.If):
                if not type_checking(node.test):
                    visit(node.body)
                visit(node.orelse)
            else:
                for field in ("body", "orelse", "finalbody", "handlers", "cases"):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        visit(children)

    visit(ast.parse(source).body)
    found.discard(module_name)
    return found
