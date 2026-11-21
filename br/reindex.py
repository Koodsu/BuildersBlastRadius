"""Rebuild the code-structure index inside existing maps, without tracing again.

    python -m br.reindex /sg /app/maps

Needed after a change to how code objects are compared (br/pyindex.py). Run
br.basestate afterwards: its fingerprints use the same digests.
"""

from __future__ import annotations

import os
import pickle
import sys

from br import pyindex


def main(argv):
    root = os.path.abspath(argv[1])
    cov_dir = os.path.join(os.path.abspath(argv[2]), "cov")
    count = 0
    for top in ("sqlglot", "tests"):
        for directory, _, names in os.walk(os.path.join(root, top)):
            for name in names:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(directory, name)
                source = os.path.relpath(path, root)
                target = os.path.join(cov_dir, source.replace("/", "__") + ".pkl")
                if not os.path.exists(target):
                    continue
                with open(target, "rb") as handle:
                    entry = pickle.load(handle)
                with open(path, encoding="utf-8") as handle:
                    text = handle.read()
                try:
                    entry["index"] = pyindex.build_index(text, path)
                except SyntaxError:
                    continue
                with open(target, "wb") as handle:
                    pickle.dump(entry, handle, protocol=pickle.HIGHEST_PROTOCOL)
                count += 1
    sys.stderr.write(f"reindex: {count} sources\n")


if __name__ == "__main__":
    main(sys.argv)
