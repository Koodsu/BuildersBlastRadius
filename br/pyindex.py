"""Structure of a Python source file: its code objects and statement extents.

Built once from the pinned commit for every file, and again at run time for a
patched file, so the two can be compared without caring about line numbers.
"""

from __future__ import annotations

import ast
import hashlib
import types

CO_OPTIMIZED = 0x1


def _const_key(const):
    if isinstance(const, types.CodeType):
        return ("<code>", const.co_name)
    if isinstance(const, (tuple, frozenset)):
        items = [_const_key(item) for item in const]
        if isinstance(const, frozenset):
            items.sort(key=repr)
        return (type(const).__name__, tuple(items))
    return (type(const).__name__, repr(const))


def own_digest(code: types.CodeType) -> str:
    """Hash of everything a code object does itself, ignoring line numbers and nested code."""
    consts = [_const_key(const) for const in code.co_consts]
    if "__firstlineno__" in code.co_names and not code.co_flags & CO_OPTIMIZED:
        # Python 3.13 stores a class body's first line in its constants.
        marker = ("int", repr(code.co_firstlineno))
        consts = [("<firstlineno>",) if const == marker else const for const in consts]
    payload = repr(
        (
            code.co_code,
            code.co_names,
            code.co_varnames,
            code.co_freevars,
            code.co_cellvars,
            code.co_flags,
            code.co_argcount,
            code.co_kwonlyargcount,
            code.co_posonlyargcount,
            code.co_exceptiontable,
            tuple(consts),
        )
    ).encode()
    return hashlib.blake2b(payload, digest_size=12).hexdigest()


def kind_of(code: types.CodeType) -> str:
    if code.co_name == "<module>":
        return "module"
    if code.co_flags & CO_OPTIMIZED:
        return "function"
    return "class"


def code_tree(code: types.CodeType) -> dict:
    """{path: info} for a module's code object and everything nested in it.

    path is a tuple of (name, n) pairs from the module down, n counting earlier
    siblings with the same name. info has kind, digest, own lines and the
    line span including nested code.
    """
    tree = {}

    def visit(node, path):
        own = {line for _, _, line in node.co_lines() if line is not None}
        low = min(own) if own else node.co_firstlineno
        high = max(own) if own else node.co_firstlineno
        low = min(low, node.co_firstlineno)
        seen = {}
        children = []
        for const in node.co_consts:
            if isinstance(const, types.CodeType):
                index = seen.get(const.co_name, 0)
                seen[const.co_name] = index + 1
                child_path = path + ((const.co_name, index),)
                child_low, child_high = visit(const, child_path)
                children.append(child_path)
                low = min(low, child_low)
                high = max(high, child_high)
        tree[path] = {
            "kind": kind_of(node),
            "digest": own_digest(node),
            "own": sorted(own),
            "first": node.co_firstlineno,
            "low": low,
            "high": high,
            "children": children,
        }
        return low, high

    visit(code, ())
    return tree


_COMPOUND = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Match,
)


def statement_spans(source: str) -> list:
    """Sorted (start, end, is_def) spans: each simple statement, each compound header."""
    spans = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.stmt):
            start = node.lineno
            decorators = getattr(node, "decorator_list", None)
            if decorators:
                start = min([start] + [d.lineno for d in decorators])
            if isinstance(node, _COMPOUND):
                body = getattr(node, "body", None) or getattr(node, "cases", None)
                first = body[0] if body else None
                first_line = getattr(first, "lineno", None)
                if first is not None and getattr(first, "decorator_list", None):
                    first_line = min([first_line] + [d.lineno for d in first.decorator_list])
                end = node.lineno if first_line is None else max(node.lineno, first_line - 1)
                is_def = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            else:
                end = node.end_lineno or node.lineno
                is_def = False
            spans.append((start, end, is_def))
        elif isinstance(node, (ast.ExceptHandler, ast.match_case)):
            body = node.body
            start = getattr(node, "lineno", None) or node.pattern.lineno
            end = max(start, body[0].lineno - 1) if body else start
            spans.append((start, end, False))
    spans.sort()
    return spans


def build_index(source: str, filename: str) -> dict:
    code = compile(source, filename, "exec", dont_inherit=True)
    return {"tree": code_tree(code), "spans": statement_spans(source)}
