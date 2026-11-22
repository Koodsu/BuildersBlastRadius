"""Reads a unified diff, applies it, and says which lines of the old file the change can reach."""

import ast
import io
import re
import tokenize

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
ALL = "ALL"


class PatchError(Exception):
    pass


def parse_patch(text):
    """-> list of {"old": path|None, "new": path|None, "hunks": [(old_start, [(tag, line)])], "binary": bool}"""
    files = []
    lines = text.split("\n")
    i = 0
    current = None
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            match = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if not match:
                raise PatchError("diff header")
            current = {"old": match.group(1), "new": match.group(2), "hunks": [], "binary": False, "nonl": False}
            files.append(current)
            i += 1
        elif current is None:
            if line.strip():
                raise PatchError("text before first diff")
            i += 1
        elif line.startswith("--- "):
            if line[4:].strip() == "/dev/null":
                current["old"] = None
            i += 1
        elif line.startswith("+++ "):
            if line[4:].strip() == "/dev/null":
                current["new"] = None
            i += 1
        elif line.startswith("rename from "):
            current["old"] = line[len("rename from "):]
            i += 1
        elif line.startswith("rename to "):
            current["new"] = line[len("rename to "):]
            i += 1
        elif line.startswith(("Binary files", "GIT binary patch")):
            current["binary"] = True
            i += 1
        elif line.startswith("@@"):
            match = HUNK.match(line)
            if not match:
                raise PatchError("hunk header")
            old_left = int(match.group(2) or 1)
            new_left = int(match.group(4) or 1)
            body = []
            i += 1
            while (old_left > 0 or new_left > 0) and i < len(lines):
                row = lines[i]
                tag = row[:1] or " "
                if tag == " ":
                    old_left -= 1
                    new_left -= 1
                elif tag == "-":
                    old_left -= 1
                elif tag == "+":
                    new_left -= 1
                elif tag == "\\":
                    if body:
                        body[-1] = (body[-1][0], body[-1][1], True)
                    i += 1
                    continue
                else:
                    raise PatchError("hunk body")
                body.append((tag, row[1:], False))
                i += 1
            if i < len(lines) and lines[i].startswith("\\"):
                if body:
                    body[-1] = (body[-1][0], body[-1][1], True)
                i += 1
            current["hunks"].append((int(match.group(1)), int(match.group(2) or 1), body))
        else:
            i += 1  # index, mode, similarity lines
    if not files:
        raise PatchError("no files")
    return files


def apply_hunks(old_text, hunks):
    """-> (new_text, groups); each group is (removed_old_lines, added_new_lines, old_lines_before)."""
    old = old_text.split("\n")
    old_ends_nl = old_text.endswith("\n")
    if old_ends_nl:
        old.pop()
    new = []
    groups = []
    cursor = 0  # old lines consumed
    new_ends_nl = old_ends_nl
    touched_end = False
    for start, count, body in hunks:
        at = start - 1 if count else start
        if at < cursor:
            raise PatchError("overlapping hunks")
        new.extend(old[cursor:at])
        cursor = at
        group = None
        for tag, text, nonl in body:
            if tag == " ":
                if cursor >= len(old) or old[cursor] != text:
                    raise PatchError("context mismatch")
                new.append(text)
                cursor += 1
                group = None
                if cursor == len(old):
                    new_ends_nl = not nonl
                    touched_end = True
            else:
                if group is None:
                    group = ([], [], cursor)
                    groups.append(group)
                if tag == "-":
                    if cursor >= len(old) or old[cursor] != text:
                        raise PatchError("removed line mismatch")
                    cursor += 1
                    group[0].append(cursor)
                else:
                    new.append(text)
                    group[1].append(len(new))
                    if cursor == len(old):
                        new_ends_nl = not nonl
                        touched_end = True
    new.extend(old[cursor:])
    if not touched_end:
        new_ends_nl = old_ends_nl
    return "\n".join(new) + ("\n" if new_ends_nl and new else ""), groups


def code_lines(source):
    """Lines that hold something other than comments and blank space."""
    found = set()
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            found.update(range(token.start[0], token.end[0] + 1))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return None
    return found


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _index(tree):
    nodes = []

    def visit(node, depth, in_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.stmt, ast.ExceptHandler, ast.Lambda)):
                low = child.lineno
                for decorator in getattr(child, "decorator_list", ()):
                    low = min(low, decorator.lineno)
                child._br_in_function = in_function
                nodes.append((low, child.end_lineno, depth, child))
            visit(child, depth + 1, in_function or isinstance(child, FUNCS))

    visit(tree, 0, False)
    return nodes


def _innermost(nodes, low, high, kinds=None):
    best = None
    for node_low, node_high, depth, node in nodes:
        if node_low <= low and high <= node_high and (kinds is None or isinstance(node, kinds)):
            if best is None or depth >= best[2]:
                best = (node_low, node_high, depth, node)
    return best


def _body_range(low, high, node):
    """Lines that run when the function is called (not when it is defined)."""
    if isinstance(node, ast.Lambda):
        lines = set(range(node.body.lineno, node.body.end_lineno + 1))
        rest = lines - {node.lineno}
        return rest or lines
    return set(range(node.body[0].lineno, high + 1))


def assigned_names(node):
    """Names a class-level or module-level assignment binds, or None if it is not a plain one."""
    if getattr(node, "_br_in_function", True):
        return None
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
        targets = [node.target]
    else:
        return None
    if all(isinstance(target, ast.Name) for target in targets):
        return frozenset(target.id for target in targets)
    return None


def references(source, names):
    """Lines of source that read one of the names (as a variable, an attribute or a string)."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ALL
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            hit = node.attr in names and not isinstance(node.ctx, ast.Store)
        elif isinstance(node, ast.Name):
            hit = node.id in names and not isinstance(node.ctx, ast.Store)
        elif isinstance(node, ast.Constant):
            hit = isinstance(node.value, str) and node.value in names
        else:
            hit = False
        if hit:
            found.update(range(node.lineno, node.end_lineno + 1))
    return found


def reach(old_source, new_source, groups, narrow=None):
    """Old-file lines whose execution a test needs for the change to matter to it.

    Returns a set of line numbers, or ALL when the change cannot be pinned to lines.
    An empty set (and nothing in narrow) means the two versions are the same program.
    narrow collects (lines, names) for changed class-level and module-level assignments:
    such a change matters only to tests that run those lines (they import the module)
    and also run some line that reads one of the names.
    """
    try:
        old_tree = ast.parse(old_source)
    except SyntaxError:
        return ALL
    try:
        new_tree = ast.parse(new_source)
    except (SyntaxError, ValueError):
        return ALL
    if ast.dump(old_tree) == ast.dump(new_tree):
        return set()
    old_code = code_lines(old_source)
    new_code = code_lines(new_source)
    if old_code is None or new_code is None:
        return ALL
    nodes = _index(old_tree)
    out = set()
    for removed, added, before in groups:
        removed_code = [line for line in removed if line in old_code]
        added_code = [line for line in added if line in new_code]
        if not removed_code and not added_code:
            continue
        if not removed_code:
            # Code was only added: it runs when the function around it runs.
            low, high = (before, before + 1) if not removed else (removed[0], removed[-1])
            found = _innermost(nodes, low, high, FUNCS)
            if found is None:
                return ALL
            out |= _body_range(*found[:2], found[3])
            continue
        for line in removed_code:
            found = _innermost(nodes, line, line)
            if found is None:
                return ALL
            low, high, _, node = found
            if isinstance(node, ast.Lambda):
                out |= _body_range(low, high, node)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Not inside any statement of the body: the signature or a decorator.
                out |= _body_range(low, high, node)
            elif isinstance(getattr(node, "body", None), list) and node.body:
                out |= set(range(low, max(low, node.body[0].lineno - 1) + 1))
            else:
                names = assigned_names(node)
                if names and narrow is not None:
                    narrow.append((frozenset(range(low, high + 1)), names))
                else:
                    out |= set(range(low, high + 1))
    return out
