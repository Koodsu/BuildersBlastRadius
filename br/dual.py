"""Old and new side by side: does an edited expression ever evaluate differently?

When a patch changes one expression inside one function, the function can be
given code that evaluates the new expression (as the patched program would)
and, each time, the old one as well. A test during which the two never differ
ran exactly as it does without the patch, so it passes. Counting evaluations
and differences per test tells the verifier how much it has seen of the change
actually mattering.

Only expressions that are safe to evaluate twice are handled: names,
attributes, constants, comparisons, boolean and arithmetic operators, and
calls to a few functions known to have no effects.
"""

from __future__ import annotations

import __future__
import ast
import re

from br import pyindex

# A function compiled on its own must see the `from __future__` lines of its module.
_FUTURE = 0
for _name in __future__.all_feature_names:
    _FUTURE |= getattr(__future__, _name).compiler_flag

SAFE_BUILTINS = frozenset(
    (
        "len", "isinstance", "issubclass", "bool", "int", "str", "float", "abs", "min", "max",
        "type", "tuple", "frozenset", "getattr", "hasattr", "callable", "id", "repr", "ord", "chr",
    )
)
SAFE_METHODS = frozenset(
    (
        "get", "startswith", "endswith", "lower", "upper", "strip", "lstrip", "rstrip",
        "isdigit", "isalpha", "isalnum", "isupper", "islower", "isspace", "isidentifier",
        "is_type", "text", "count", "find", "index", "keys", "values", "items", "casefold",
    )
)
_PRIVATE = re.compile(r"^__\w*[^_]_?$|^__\w*[^_]$")

COUNTS = [0, 0]  # evaluations, evaluations where old and new differed


def _dual(new_thunk, old_thunk, truth):
    """Evaluate the new expression, compare it with the old one, return the new value."""
    COUNTS[0] += 1
    try:
        new = new_thunk()
    except Exception as error:
        try:
            old_thunk()
        except Exception as other:
            if type(other) is not type(error):
                COUNTS[1] += 1
        else:
            COUNTS[1] += 1
        raise
    try:
        old = old_thunk()
        if truth:
            same = bool(old) == bool(new)
        else:
            same = old is new or (type(old) is type(new) and bool(old == new))
        if not same:
            COUNTS[1] += 1
    except Exception:  # noqa: BLE001
        COUNTS[1] += 1
    return new


def _safe(node):
    """True if evaluating the expression a second time cannot change anything."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name):
        return isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Attribute):
        return isinstance(node.ctx, ast.Load) and _safe(node.value)
    if isinstance(node, ast.Compare):
        return _safe(node.left) and all(_safe(item) for item in node.comparators)
    if isinstance(node, ast.BoolOp):
        return all(_safe(item) for item in node.values)
    if isinstance(node, ast.UnaryOp):
        return _safe(node.operand)
    if isinstance(node, ast.BinOp):
        return _safe(node.left) and _safe(node.right)
    if isinstance(node, ast.IfExp):
        return _safe(node.test) and _safe(node.body) and _safe(node.orelse)
    if isinstance(node, ast.Subscript):
        return isinstance(node.ctx, ast.Load) and _safe(node.value) and _safe(node.slice)
    if isinstance(node, ast.Slice):
        return all(part is None or _safe(part) for part in (node.lower, node.upper, node.step))
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return isinstance(getattr(node, "ctx", ast.Load()), ast.Load) and all(
            _safe(item) for item in node.elts
        )
    if isinstance(node, ast.Dict):
        return all(key is not None and _safe(key) for key in node.keys) and all(
            _safe(value) for value in node.values
        )
    if isinstance(node, ast.Call):
        if node.keywords and any(keyword.arg is None for keyword in node.keywords):
            return False
        if not all(_safe(arg) for arg in node.args):
            return False
        if not all(_safe(keyword.value) for keyword in node.keywords):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in SAFE_BUILTINS
        if isinstance(func, ast.Attribute):
            return func.attr in SAFE_METHODS and _safe(func.value)
        return False
    return False


def _replaceable(node):
    """An expression whose text can be swapped for a call without changing its meaning."""
    if not isinstance(node, ast.expr):
        return False
    if isinstance(node, (ast.Starred, ast.Slice, ast.FormattedValue)):
        return False
    return isinstance(getattr(node, "ctx", None) or ast.Load(), ast.Load)


def _differ(old, new):
    """None if the two trees are the same, else the smallest pair of matching
    nodes that holds every difference between them."""
    if type(old) is not type(new):
        return (old, new)
    inner = []
    for field in old._fields:
        a = getattr(old, field, None)
        b = getattr(new, field, None)
        if isinstance(a, list) or isinstance(b, list):
            if not (isinstance(a, list) and isinstance(b, list)) or len(a) != len(b):
                return (old, new)
            for item_a, item_b in zip(a, b):
                if isinstance(item_a, ast.AST) and isinstance(item_b, ast.AST):
                    found = _differ(item_a, item_b)
                    if found:
                        inner.append(found)
                elif item_a != item_b:
                    return (old, new)
        elif isinstance(a, ast.AST) and isinstance(b, ast.AST):
            found = _differ(a, b)
            if found:
                inner.append(found)
        elif isinstance(a, ast.AST) or isinstance(b, ast.AST):
            return (old, new)
        elif a != b or type(a) is not type(b):
            return (old, new)
    if not inner:
        return None
    if isinstance(old, ast.JoinedStr):
        return (old, new)  # text inside an f-string cannot be swapped piecemeal
    if len(inner) == 1 and _replaceable(inner[0][0]) and _replaceable(inner[0][1]):
        return inner[0]
    return (old, new)


def _offset(lines, line, column):
    """Character position in "\\n".join(lines) of a (1-based line, utf-8 column)."""
    text = lines[line - 1]
    within = len(text.encode("utf-8")[:column].decode("utf-8", "ignore"))
    return sum(len(item) + 1 for item in lines[: line - 1]) + within


def _function(tree, name):
    node = tree.body[-1] if tree.body else None  # after the line of imports
    if isinstance(node, ast.ClassDef) and node.body:
        node = node.body[0]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
        return node
    return None


def _code_named(module_code, name, first_line):
    stack = [module_code]
    while stack:
        code = stack.pop()
        for const in code.co_consts:
            if hasattr(const, "co_code"):
                if const.co_name == name and const.co_firstlineno == first_line:
                    return const
                stack.append(const)
    return None


def compile_alone(lines, def_line, end_line, name, filename, owner, imported, future):
    """Code object of the function at lines[def_line-1:end_line], compiled by itself.

    `owner` is the class it is a method of, if any; `imported` the names its
    module imports at the top level; `future` the module's __future__ flags.
    None if it cannot be compiled this way.
    """
    text = lines[def_line - 1 : end_line]
    if not text:
        return None
    head = text[0].lstrip()
    if not (head.startswith("def ") or head.startswith("async def ")):
        return None
    prefix = _prefix(text[0] == head, def_line, owner, imported)
    if prefix is None:
        return None
    try:
        module = compile(
            prefix + "\n".join(text) + "\n", filename, "exec", flags=future, dont_inherit=True
        )
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    return _code_named(module, name, def_line)


def _prefix(top_level, def_line, owner, imported):
    """What goes before a function's own text so that it compiles as in its module."""
    # The compiler treats a call through a name the module imports differently,
    # so the names are imported here too, on the first line, where nothing runs them.
    imports = ("import " + ", ".join(sorted(imported))) if imported else ""
    if top_level and def_line >= 2:
        return imports + "\n" * (def_line - 1)  # a module-level function
    if not top_level and owner and owner.isidentifier() and def_line >= 3:
        # A method: inside a class of the same name it gets the same private
        # names and the same `__class__` cell for super().
        return imports + "\n" * (def_line - 2) + f"class {owner}:\n"
    return None


def subtree(code):
    """{path below a code object: (digest, code)}, paths as pyindex.code_tree names them."""
    found = {}

    def visit(node, path):
        found[path] = (pyindex.own_digest(node), node)
        seen = {}
        for const in node.co_consts:
            if hasattr(const, "co_code"):
                index = seen.get(const.co_name, 0)
                seen[const.co_name] = index + 1
                visit(const, path + ((const.co_name, index),))

    visit(code, ())
    return found


WHY = [""]  # why the last build() gave up, for the curious


def imported_names(module_code):
    """Names a module binds with import statements at its top level."""
    import dis

    names = set()
    pending = False
    for instruction in dis.get_instructions(module_code):
        if instruction.opname in ("IMPORT_NAME", "IMPORT_FROM"):
            pending = True
        elif pending and instruction.opname in ("STORE_NAME", "STORE_GLOBAL"):
            if instruction.argval.isidentifier():
                names.add(instruction.argval)
            pending = False
        elif instruction.opname not in ("POP_TOP", "LOAD_ATTR"):
            pending = False
    return names


def build(
    base_lines, new_lines, def_line, end_line, name, filename, new_code, owner=None, imported=()
):
    WHY[0] = ""
    return _build(
        base_lines, new_lines, def_line, end_line, name, filename, new_code, owner, imported
    )


def _no(reason):
    WHY[0] = reason
    return None


def _build(base_lines, new_lines, def_line, end_line, name, filename, new_code, owner, imported):
    """Code for the edited function that also evaluates the expression as it was.

    base_lines/new_lines are the two versions of the file (same line count
    within the function). Returns a code object, or None when the edit is not
    one expression that is safe to evaluate twice.
    """
    old_text = base_lines[def_line - 1 : end_line]
    new_text = new_lines[def_line - 1 : end_line]
    if len(old_text) != len(new_text) or not old_text:
        return _no("extent")
    head = new_text[0].lstrip()
    if not (head.startswith("def ") or head.startswith("async def ")):
        return _no("not a def")
    prefix = _prefix(new_text[0] == head, def_line, owner, imported)
    if prefix is None:
        return _no("nested")
    try:
        old_tree = ast.parse(prefix + "\n".join(old_text) + "\n")
        new_tree = ast.parse(prefix + "\n".join(new_text) + "\n")
    except (SyntaxError, ValueError):
        return None
    old_fn = _function(old_tree, name)
    new_fn = _function(new_tree, name)
    if old_fn is None or new_fn is None:
        return _no("function not found")
    # Compiled on its own the function must be what it is inside its module:
    # no private names to mangle, no closure over a class or an outer function.
    flags = new_code.co_flags & _FUTURE
    try:
        alone = _code_named(
            compile(
                prefix + "\n".join(new_text) + "\n", filename, "exec", flags=flags, dont_inherit=True
            ),
            name,
            def_line,
        )
    except (SyntaxError, ValueError):
        return None
    if alone is None or alone.co_freevars != new_code.co_freevars:
        return _no("closure")
    if pyindex.own_digest(alone) != pyindex.own_digest(new_code):
        return _no("not the same code when compiled alone")

    pair = _differ(old_fn, new_fn)
    if pair is None:
        return _no("no difference")
    old_expr, new_expr = pair
    if not (_replaceable(old_expr) and _replaceable(new_expr)):
        return _no("not one expression")
    if not (_safe(old_expr) and _safe(new_expr)):
        return _no("not safe to evaluate twice")
    body_start = new_fn.body[0].lineno if new_fn.body else None
    if body_start is None or new_expr.lineno < body_start:
        return _no("in the signature")  # evaluated when the function is defined
    # Inside a nested function or lambda the swap would not reach copies made earlier.
    parents = {}
    for node in ast.walk(new_fn):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    walker = parents.get(new_expr)
    while walker is not None and walker is not new_fn:
        if isinstance(walker, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            return _no("inside a nested function")
        walker = parents.get(walker)
    parent = parents.get(new_expr)
    truth = (
        isinstance(parent, (ast.If, ast.While, ast.Assert, ast.IfExp))
        and parent.test is new_expr
        or isinstance(parent, ast.UnaryOp)
        and isinstance(parent.op, ast.Not)
        or isinstance(parent, ast.comprehension)
        and new_expr in parent.ifs
    )

    lines = (prefix + "\n".join(new_text) + "\n").split("\n")
    start = _offset(lines, new_expr.lineno, new_expr.col_offset)
    stop = _offset(lines, new_expr.end_lineno, new_expr.end_col_offset)
    source = "\n".join(lines)
    try:
        old_source = ast.unparse(old_expr)
    except Exception:  # noqa: BLE001
        return _no("old expression has no source")
    swapped = (
        source[:start]
        + "__br_dual__(lambda: ("
        + source[start:stop]
        + "), lambda: ("
        + old_source
        + "), "
        + ("True" if truth else "False")
        + ")"
        + source[stop:]
    )
    try:
        code = _code_named(
            compile(swapped, filename, "exec", flags=flags, dont_inherit=True), name, def_line
        )
    except (SyntaxError, ValueError):
        return None
    if code is None or code.co_freevars != new_code.co_freevars:
        return _no("swapped code has another closure")
    if code.co_argcount != new_code.co_argcount:
        return _no("swapped code has another signature")
    return code


def install():
    """Make the comparison helper reachable from any module."""
    import builtins

    builtins.__br_dual__ = _dual
    COUNTS[0] = COUNTS[1] = 0
