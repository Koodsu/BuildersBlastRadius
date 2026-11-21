"""Unified diff parsing and strict application."""

from __future__ import annotations

import re

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


class FilePatch:
    __slots__ = (
        "old_path",
        "new_path",
        "hunks",
        "binary",
        "rename",
        "mode_change",
        "new_file",
        "deleted",
        "new_no_newline",
    )

    def __init__(self):
        self.old_path = None
        self.new_path = None
        self.hunks = []
        self.binary = False
        self.rename = False
        self.mode_change = False
        self.new_file = False
        self.deleted = False
        self.new_no_newline = False

    @property
    def path(self):
        return self.new_path if self.new_path is not None else self.old_path

    def removed_lines(self):
        """Old-file line numbers of every removed line."""
        removed = []
        for old_start, _, _, _, lines in self.hunks:
            number = old_start
            for tag, _ in lines:
                if tag == "-":
                    removed.append(number)
                    number += 1
                elif tag == " ":
                    number += 1
        return removed

    def insert_points(self):
        """Old-file line numbers after which lines were added."""
        points = []
        for old_start, _, _, _, lines in self.hunks:
            number = old_start
            for tag, _ in lines:
                if tag == "+":
                    points.append(number - 1)
                else:
                    number += 1
        return points

    def added_texts(self):
        return [text for _, _, _, _, lines in self.hunks for tag, text in lines if tag == "+"]

    def removed_texts(self):
        return [text for _, _, _, _, lines in self.hunks for tag, text in lines if tag == "-"]


def _strip(path):
    path = path.strip()
    if path.startswith('"') and path.endswith('"'):
        path = path[1:-1]
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        path = path[2:]
    return path


def parse(text: str) -> list:
    """Parse a git-style unified diff into FilePatch objects."""
    patches = []
    current = None
    lines = text.split("\n")
    index = 0
    count = len(lines)
    while index < count:
        line = lines[index]
        if line.startswith("diff --git "):
            current = FilePatch()
            patches.append(current)
            match = re.match(r"^diff --git a/(.*) b/(.*)$", line)
            if match:
                current.old_path = match.group(1)
                current.new_path = match.group(2)
            index += 1
            continue
        if current is None:
            if line.startswith("--- "):
                current = FilePatch()
                patches.append(current)
            else:
                index += 1
                continue
        if line.startswith("--- "):
            current.old_path = _strip(line[4:].split("\t")[0])
            if current.old_path is None:
                current.new_file = True
        elif line.startswith("+++ "):
            current.new_path = _strip(line[4:].split("\t")[0])
            if current.new_path is None:
                current.deleted = True
        elif line.startswith("new file mode"):
            current.new_file = True
        elif line.startswith("deleted file mode"):
            current.deleted = True
        elif line.startswith(("rename from", "rename to", "copy from", "copy to")):
            current.rename = True
        elif line.startswith(("old mode", "new mode")):
            current.mode_change = True
        elif line.startswith(("GIT binary patch", "Binary files ")):
            current.binary = True
        else:
            match = _HUNK.match(line)
            if match:
                old_start = int(match.group(1))
                old_len = int(match.group(2)) if match.group(2) is not None else 1
                new_start = int(match.group(3))
                new_len = int(match.group(4)) if match.group(4) is not None else 1
                body = []
                seen_old = seen_new = 0
                index += 1
                while index < count and (seen_old < old_len or seen_new < new_len):
                    entry = lines[index]
                    tag = entry[:1]
                    if tag == "\\":
                        if body and body[-1][0] in "+ ":
                            current.new_no_newline = True
                        index += 1
                        continue
                    if tag == "":
                        tag = " "  # a stripped blank context line
                    if tag == " ":
                        seen_old += 1
                        seen_new += 1
                    elif tag == "-":
                        seen_old += 1
                    elif tag == "+":
                        seen_new += 1
                    else:
                        raise PatchError(f"unexpected hunk line {entry[:20]!r}")
                    body.append((tag, entry[1:]))
                    index += 1
                    if tag in "+ ":
                        current.new_no_newline = False
                if index < count and lines[index].startswith("\\"):
                    if body and body[-1][0] in "+ ":
                        current.new_no_newline = True
                    index += 1
                if seen_old != old_len or seen_new != new_len:
                    raise PatchError("truncated hunk")
                current.hunks.append((old_start, old_len, new_start, new_len, body))
                continue
        index += 1
    return patches


def apply(base: str, file_patch: FilePatch) -> str:
    """Apply one file's hunks to its base text. Raises PatchError if context does not match."""
    had_newline = base.endswith("\n")
    old = base.split("\n")
    if had_newline or not base:
        old.pop()
    out = []
    cursor = 0
    touched_end = False
    for old_start, old_len, _, _, lines in file_patch.hunks:
        start = old_start - 1 if old_len else old_start
        if start < cursor or start > len(old):
            raise PatchError("hunk out of order or past the end")
        out.extend(old[cursor:start])
        cursor = start
        for tag, text in lines:
            if tag == " ":
                if cursor >= len(old) or old[cursor] != text:
                    raise PatchError("context mismatch")
                out.append(text)
                cursor += 1
            elif tag == "-":
                if cursor >= len(old) or old[cursor] != text:
                    raise PatchError("removed line mismatch")
                cursor += 1
            else:
                out.append(text)
        touched_end = cursor >= len(old)
    out.extend(old[cursor:])
    result = "\n".join(out)
    if not out:
        return ""
    if file_patch.new_no_newline:
        return result
    if had_newline or touched_end or not base:
        result += "\n"
    return result
