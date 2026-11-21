"""Dev aid: run the selector on a labeled practice set and list misses and losses.

    python tools/score_set.py <maps_dir> <set_dir> [selector.py]

Only commits whose answers were checked (tools/fullcheck.py) are counted.
"""
import json
import os
import subprocess
import sys

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, repo)
from br.policy import pack  # noqa: E402

maps, data = sys.argv[1], sys.argv[2]
selector = sys.argv[3] if len(sys.argv) > 3 else os.path.join(repo, "selector.py")
env = dict(os.environ, BR_DATA=data, BR_MAPS=maps, BR_SG="/sg")
with open("/tmp/set_out.jsonl", "wb") as out, open("/tmp/set_err.log", "wb") as err:
    code = subprocess.call([sys.executable, selector], stdout=out, stderr=err, env=env)
answers = json.load(open(os.path.join(data, "answers.json")))["commits"]
tests = json.load(open(os.path.join(data, "tests.json")))["tests"]
ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
name = {t["number"]: t["id"] for t in tests}
log = open("/tmp/set_err.log", encoding="utf-8", errors="replace").read().splitlines()
checked = {c: v for c, v in answers.items() if v.get("checked")}
chosen = {json.loads(line)["commit"]: json.loads(line)["workers"] for line in open("/tmp/set_out.jsonl")}
def confirmed(commit, numbers):
    """Rerun the files of apparently missed tests on the patched tree: do they really fail?"""
    code = (
        "import json,os,sys\n"
        "sys.path.insert(0,%r)\n"
        "from br import patch as patchlib, runner, verify\n"
        "tests=json.load(open(%r))['tests']\n"
        "wanted=%r\n"
        "files={t['file'] for t in tests if t['number'] in wanted}\n"
        "patched={}\n"
        "for fp in patchlib.parse(open(%r).read()):\n"
        "    patched[fp.path]=patchlib.apply(open('/sg/'+fp.path,encoding='utf-8',newline='').read(),fp)\n"
        "dest='/tmp/confirm%%d'%%os.getpid()\n"
        "verify.make_tree('/sg',dest,patched)\n"
        "sys.path.insert(0,dest); os.chdir(dest)\n"
        "runner.install_inline_pools()\n"
        "failed,_,_=runner.run([t['id'] for t in tests if t['file'] in files])\n"
        "print(json.dumps(sorted(t['number'] for t in tests if t['id'] in failed and t['number'] in wanted)))\n"
    ) % (repo, os.path.join(data, "tests.json"), sorted(numbers), os.path.join(data, "commits", commit + ".patch"))
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    try:
        return set(json.loads(done.stdout.strip().splitlines()[-1]))
    except Exception:
        return set(numbers)


misses = 0
ours = best = 0
losses = []
for commit, value in sorted(checked.items()):
    selected = {n for worker in chosen[commit] for n in worker}
    failing = set(value["failing"])
    workers = [w for w in chosen[commit] if w]
    cost = (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) if workers else 0
    perfect = pack(sorted(failing), ms)[0]
    ours += cost
    best += perfect
    losses.append((cost - perfect, commit, value["kind"], len(selected), len(failing)))
    if failing - selected:
        real = confirmed(commit, failing - selected)
        if not real:
            print("not a miss after a clean rerun (label was a stall):", commit,
                  [name[n].split("::")[-1] for n in sorted(failing - selected)][:4])
            failing &= selected
            value["failing"] = sorted(failing)
            continue
        failing = (failing & selected) | real
        misses += 1
        print("MISS", commit, value["kind"], [name[n].split("::")[-1] for n in sorted(failing - selected)][:5],
              "selected", len(selected), "failing", len(failing))
        for line in log:
            if line.startswith(commit):
                print("     ", line[:200])
print(f"exit {code}: {len(checked)} labeled commits, {misses} misses, ours {ours / 1000:.1f}s vs perfect "
      f"{best / 1000:.1f}s (the two giants assumed to pass)")
for loss, commit, kind, selected, failing in sorted(losses, reverse=True)[:12]:
    if loss > 1500:
        why = [line[5:150] for line in log if line.startswith(commit) and ("->" in line or "schedule" in line)]
        print(f"  loss {loss / 1000:5.1f}s {commit} {kind} selected {selected} failing {failing} {why[-1] if why else ''}")
