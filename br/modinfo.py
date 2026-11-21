"""Build-time: what a function needs to be compiled on its own.

    python -m br.modinfo /sg /app/maps

Compiling a whole module to see what a one-line edit changed costs up to a
second for the largest files. A function compiled alone comes out the same as
inside its module once the compiler is told two things about the module: which
names it imports at its top level, and which `from __future__` features it
turns on. Writes modinfo.pkl: {source: {"imports": [names], "future": flags}}.
"""

from __future__ import annotations

import os
import pickle
import sys

from br import dual


def main(argv):
    root = os.path.abspath(argv[1])
    out_dir = os.path.abspath(argv[2])
    found = {}
    for top in ("sqlglot", "tests"):
        for directory, _, names in os.walk(os.path.join(root, top)):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                with open(path, "rb") as handle:
                    data = handle.read()
                try:
                    code = compile(data, path, "exec", dont_inherit=True)
                except (SyntaxError, ValueError):
                    continue
                future = 0
                for const in code.co_consts:
                    if hasattr(const, "co_flags"):
                        future |= const.co_flags & dual._FUTURE
                found[os.path.relpath(path, root)] = {
                    "imports": sorted(dual.imported_names(code)),
                    "future": future | (code.co_flags & dual._FUTURE),
                }
    with open(os.path.join(out_dir, "modinfo.pkl"), "wb") as handle:
        pickle.dump(found, handle, protocol=pickle.HIGHEST_PROTOCOL)
    sys.stderr.write(f"modinfo: {len(found)} sources\n")


if __name__ == "__main__":
    main(sys.argv)
