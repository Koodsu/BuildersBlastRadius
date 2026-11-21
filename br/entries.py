"""Build-time: fingerprints of the fixture entries behind tests that work in units.

    python -m br.entries /sg unit_entries.json

A test listed in runner.UNIT_LISTS takes its units of work, one for one, from a
list its class loads from a fixture file. When a patch only edits that file,
the units whose entries still have the fingerprints recorded here do exactly
what they did before, so only the others need to run. Writes
{test id: [fingerprint of each entry, in order]}.
"""

from __future__ import annotations

import importlib
import json
import os
import sys


def main(argv):
    root = os.path.abspath(argv[1])
    out_path = os.path.abspath(argv[2])
    sys.path.insert(0, root)
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    from br import runner

    found = {}
    classes = {}
    for test_id, attribute in sorted(runner.UNIT_LISTS.items()):
        module_name, class_name, _ = runner.split_id(test_id)
        klass = getattr(importlib.import_module(module_name), class_name)
        if klass not in classes:
            klass.setUpClass()
            classes[klass] = True
        entries = getattr(klass, attribute)
        found[test_id] = [runner.entry_digest(entry) for entry in entries]
        print(test_id, len(entries), "entries")
    for klass in classes:
        klass.tearDownClass()
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(found, handle, indent=0, sort_keys=True)
        handle.write("\n")


if __name__ == "__main__":
    main(sys.argv)
