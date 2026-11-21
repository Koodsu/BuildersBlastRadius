"""Dev aid: make practice patches against the pinned sqlglot tree.

    python tools/mutate.py <sqlglot_root> <out_dir> <count> [seed]

Writes <out_dir>/commits/<id>.patch and <out_dir>/kinds.json. The true failing
tests come from tools/groundtruth.py, which runs the real suite on each patch.
"""

import difflib
import json
import os
import random
import re
import sys

CODE_OPS = [
    (r" is not ", " is "),
    (r" is ", " == "),
    (r" == ", " != "),
    (r" != ", " == "),
    (r" and ", " or "),
    (r" or ", " and "),
    (r" < ", " <= "),
    (r" <= ", " < "),
    (r" > ", " >= "),
    (r" >= ", " > "),
    (r"\bTrue\b", "False"),
    (r"\bFalse\b", "True"),
    (r"\bif not ", "if "),
    (r"\bif (?!not )", "if not "),
    (r" \+ ", " - "),
    (r" - ", " + "),
    (r"(?<![\w.])(\d+)(?![\w.])", None),  # number + 1
    (r"\bnot ", ""),
]
COMMENTS = [
    "# Cheap check first",
    "# Kept for backwards compatibility",
    "# Keep in sync with the generator",
    "# Intentionally not cached",
    "# Mirrors the behavior of the reference implementation",
]


def py_files(root):
    found = []
    for directory, _, names in os.walk(os.path.join(root, "sqlglot")):
        for name in names:
            if name.endswith(".py"):
                found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def fixture_files(root):
    found = []
    for directory, _, names in os.walk(os.path.join(root, "tests", "fixtures")):
        for name in names:
            if name.endswith(".sql"):
                found.append(os.path.relpath(os.path.join(directory, name), root))
    return sorted(found)


def diff(rel, old, new):
    lines = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{rel}", f"b/{rel}", n=3
    )
    return f"diff --git a/{rel} b/{rel}\n" + "".join(lines)


def mutate_code(root, rng, files):
    for _ in range(200):
        rel = rng.choice(files)
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = old.split("\n")
        index = rng.randrange(len(lines))
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", '"', "'", "import ", "from ", "@")):
            continue
        ops = CODE_OPS[:]
        rng.shuffle(ops)
        for pattern, replacement in ops:
            matches = list(re.finditer(pattern, line))
            if not matches:
                continue
            match = rng.choice(matches)
            if replacement is None:
                new_text = str(int(match.group(1)) + 1)
            else:
                new_text = replacement
            lines2 = lines[:]
            lines2[index] = line[: match.start()] + new_text + line[match.end() :]
            new = "\n".join(lines2)
            try:
                a = compile(old, rel, "exec", dont_inherit=True)
                b = compile(new, rel, "exec", dont_inherit=True)
            except SyntaxError:
                continue
            if a.co_code == b.co_code and repr(a.co_consts) == repr(b.co_consts):
                continue
            return diff(rel, old, new)
    raise RuntimeError("no mutation found")


ENTRY = re.compile(r"^(\s{8,})([\w.\"' ]+?):\s*(.+),\s*$")


def mutate_entry(root, rng, files):
    """Edit one entry of a dict literal inside a class body: the commonest class-level edit."""
    for _ in range(400):
        rel = rng.choice(files)
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = old.split("\n")
        index = rng.randrange(len(lines))
        match = ENTRY.match(lines[index])
        if not match:
            continue
        indent, key, value = match.groups()
        choice = rng.random()
        mutated = lines[:]
        neighbour = ENTRY.match(lines[index + 1]) if index + 1 < len(lines) else None
        if choice < 0.3:
            del mutated[index]  # drop the entry
        elif choice < 0.65 and neighbour and neighbour.group(3) != value:
            mutated[index] = f"{indent}{key}: {neighbour.group(3)},"  # the next entry's value
        else:
            changed = None
            for pattern, replacement in rng.sample(CODE_OPS, len(CODE_OPS)):
                found = list(re.finditer(pattern, value))
                if found:
                    hit = rng.choice(found)
                    piece = str(int(hit.group(1)) + 1) if replacement is None else replacement
                    changed = value[: hit.start()] + piece + value[hit.end():]
                    break
            if changed is None or changed == value:
                continue
            mutated[index] = f"{indent}{key}: {changed},"
        new = "\n".join(mutated)
        try:
            a = compile(old, rel, "exec", dont_inherit=True)
            compile(new, rel, "exec", dont_inherit=True)
        except SyntaxError:
            continue
        # Only entries of class-level dicts: the module's own constants must differ.
        b = compile(new, rel, "exec", dont_inherit=True)
        if repr(a.co_consts) == repr(b.co_consts) and a.co_code == b.co_code:
            pass
        return diff(rel, old, new)
    raise RuntimeError("no entry found")


def mutate_fixture(root, rng, files):
    for _ in range(200):
        rel = rng.choice(files)
        old = open(os.path.join(root, rel), encoding="utf-8").read()
        lines = old.split("\n")
        index = rng.randrange(len(lines))
        line = lines[index]
        if not line.strip() or line.startswith(("--", "#")):
            continue
        words = line.split(" ")
        position = rng.randrange(len(words))
        choice = rng.random()
        if choice < 0.4:
            words.insert(position, rng.choice(["42", "0", "x", "NULL"]))
        elif choice < 0.7 and len(words) > 1:
            del words[position]
        else:
            words[position] = words[position] + rng.choice(["42", "a", ""])
            if choice > 0.9:
                words[position] = words[position][:-1][:-1]
        lines2 = lines[:]
        lines2[index] = " ".join(words)
        new = "\n".join(lines2)
        if new != old:
            return diff(rel, old, new)
    raise RuntimeError("no mutation found")


def mutate_comment(root, rng, files):
    for _ in range(50):
        try:
            return _mutate_comment(root, rng, files)
        except (RuntimeError, ValueError):
            continue
    raise RuntimeError("no comment spot")


def _mutate_comment(root, rng, files):
    rel = rng.choice(files)
    old = open(os.path.join(root, rel), encoding="utf-8").read()
    lines = old.split("\n")
    for _ in range(200):
        index = rng.randrange(1, len(lines))
        if not lines[index].strip():
            continue
        indent = lines[index][: len(lines[index]) - len(lines[index].lstrip())]
        lines2 = lines[:index] + [indent + rng.choice(COMMENTS)] + lines[index:]
        new = "\n".join(lines2)
        try:
            compile(new, rel, "exec", dont_inherit=True)
        except SyntaxError:
            continue
        return diff(rel, old, new)
    raise RuntimeError("no comment spot")


def main():
    root, out_dir, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
    rng = random.Random(int(sys.argv[4]) if len(sys.argv) > 4 else 1)
    os.makedirs(os.path.join(out_dir, "commits"), exist_ok=True)
    code_files = py_files(root)
    fixtures = fixture_files(root)
    kinds = {}
    alphabet = "23456789abcdefghjkmnpqrstuvwxyz"
    only_entries = os.environ.get("ENTRIES") == "1"
    for _ in range(count):
        commit = "".join(rng.choice(alphabet) for _ in range(4))
        roll = rng.random()
        if only_entries:
            kind, text = "code", mutate_entry(root, rng, code_files)
        elif roll < 0.55:
            kind, text = "code", mutate_code(root, rng, code_files)
        elif roll < 0.75:
            kind, text = "fixture", mutate_fixture(root, rng, fixtures)
        elif roll < 0.85:
            kind, text = "comment", mutate_comment(root, rng, code_files)
        else:
            kind = "multi"
            parts = [mutate_comment(root, rng, code_files)]
            parts.append(mutate_code(root, rng, code_files) if rng.random() < 0.6 else mutate_comment(root, rng, code_files))
            if rng.random() < 0.7:
                parts.append(mutate_fixture(root, rng, fixtures))
            seen = set()
            unique = []
            for part in parts:
                name = part.split("\n", 1)[0]
                if name not in seen:
                    seen.add(name)
                    unique.append(part)
            text = "".join(sorted(unique))
        kinds[commit] = kind
        with open(os.path.join(out_dir, "commits", commit + ".patch"), "w", encoding="utf-8") as handle:
            handle.write(text)
    with open(os.path.join(out_dir, "kinds.json"), "w") as handle:
        json.dump(kinds, handle, indent=1)


if __name__ == "__main__":
    main()
