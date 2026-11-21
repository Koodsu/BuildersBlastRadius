"""Dev aid: print each sample commit's candidate set next to the known answer.

    python tools/explore.py <maps_dir> <sqlglot_root> <sample_dir>
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from br import analyze  # noqa: E402


def main():
    maps = analyze.Maps(sys.argv[1])
    root = sys.argv[2]
    sample = sys.argv[3]
    tests = json.load(open(os.path.join(sample, "tests.json")))["tests"]
    answers = json.load(open(os.path.join(sample, "answers.json")))["commits"]
    number = {test["id"]: test["number"] for test in tests}
    seconds = {test["number"]: test["seconds"] for test in tests}
    to_number = [number.get(test_id) for test_id in maps.tests]
    cpu = maps.meta["cpu"]

    for name in sorted(os.listdir(os.path.join(sample, "commits"))):
        commit = name[:-6]
        text = open(os.path.join(sample, "commits", name), encoding="utf-8").read()
        result = analyze.analyze(commit, text, maps, root)
        cands = [i for i in range(len(maps.tests)) if result.cands >> i & 1]
        cand_numbers = {to_number[i] for i in cands if to_number[i] is not None}
        failing = set(answers[commit]["failing"])
        missed = failing - cand_numbers
        print(
            f"{commit} {answers[commit]['kind']:8s} fail={len(failing):3d} "
            f"({sum(seconds[n] for n in failing):6.2f}s) cand={len(cand_numbers):4d} "
            f"rec={sum(seconds[n] for n in cand_numbers):6.2f}s "
            f"cpu={sum(cpu[i] for i in cands):6.2f}s "
            f"{'FALLBACK ' if result.fallback else ''}{'MISS ' + str(sorted(missed)) if missed else ''}"
        )
        for note in result.notes:
            print("      ", note)


if __name__ == "__main__":
    main()
