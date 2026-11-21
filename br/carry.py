"""Copy the timing, class-setup and fixture-dependence data from one set of maps
to another traced from the same commit, so a re-trace does not need them redone.

    python -m br.carry /old/maps /new/maps
"""

from __future__ import annotations

import os
import pickle
import sys

FIELDS = ("cpu_plain", "class_setup", "no_setup", "fixture_users")


def main(argv):
    with open(os.path.join(argv[1], "meta.pkl"), "rb") as handle:
        old = pickle.load(handle)
    path = os.path.join(argv[2], "meta.pkl")
    with open(path, "rb") as handle:
        new = pickle.load(handle)
    if old["tests"] != new["tests"] or old["files"] != new["files"]:
        raise SystemExit("carry: the two maps do not list the same tests in the same order")
    for field in FIELDS:
        if field in old:
            new[field] = old[field]
    with open(path, "wb") as handle:
        pickle.dump(new, handle, protocol=pickle.HIGHEST_PROTOCOL)
    sys.stderr.write(f"carry: copied {[field for field in FIELDS if field in old]}\n")


if __name__ == "__main__":
    main(sys.argv)
