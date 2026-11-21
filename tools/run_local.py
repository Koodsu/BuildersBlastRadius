"""Dev aid: run the selector on the sample inside a container and score it.

    python tools/run_local.py [maps_dir]
"""
import os
import resource
import subprocess
import sys
import time

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
maps = sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo, "work", "maps")
env = dict(os.environ, BR_DATA=os.path.join(repo, "sample"), BR_MAPS=maps, BR_SG="/sg")
out = os.path.join(repo, "work", "output.jsonl")
started = time.time()
with open(out, "wb") as handle:
    code = subprocess.call([sys.executable, os.path.join(repo, "selector.py")], stdout=handle, env=env)
usage = resource.getrusage(resource.RUSAGE_CHILDREN)
cpu = usage.ru_utime + usage.ru_stime
print(f"exit {code} wall {time.time() - started:.1f}s cpu {cpu:.2f}s", flush=True)
subprocess.call(
    [sys.executable, os.path.join(repo, "score.py"), os.path.join(repo, "sample"),
     os.path.join(repo, "sample", "answers.json"), out, "--cpu-seconds", f"{cpu:.3f}"]
)
