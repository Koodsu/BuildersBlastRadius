"""Dev aid: run the selector on a set inside a container, measure its CPU, score it.

    python tools/run_local.py [maps_dir] [set_dir] [selector.py]

CPU is the whole container's (cgroup), like the scorer's. Because the machine
this runs on is rarely as fast or as idle as the scorer's, a fixed batch of
in-process tests is timed before and after, and the ratio of their CPU time to
their recorded time converts the measurement into the scorer's seconds.
"""
import json
import os
import subprocess
import sys
import time

repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
maps = sys.argv[1] if len(sys.argv) > 1 else os.path.join(repo, "work", "maps")
data = sys.argv[2] if len(sys.argv) > 2 else os.path.join(repo, "sample")
selector = sys.argv[3] if len(sys.argv) > 3 else os.path.join(repo, "selector.py")
BENCH = ("tests/dialects/test_dialect.py", "tests/test_expressions.py", "tests/dialects/test_tsql.py")


def container_cpu():
    with open("/sys/fs/cgroup/cpu.stat") as handle:
        for line in handle:
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    return 0.0


def machine_factor():
    """CPU seconds this machine needs per recorded second of plain tests."""
    code = (
        "import json,sys,time\n"
        "sys.path.insert(0,'/sg'); sys.path.insert(0,%r)\n"
        "from br import runner\n"
        "tests=json.load(open(%r))['tests']\n"
        "ids=[t['id'] for t in tests if t['file'] in %r]\n"
        "rec=sum(t['seconds'] for t in tests if t['file'] in %r)\n"
        "runner.run(ids[:5])\n"
        "tm={}; runner.run(ids, timings=tm)\n"
        "print(sum(tm.values())/rec)\n"
    ) % (os.path.dirname(os.path.dirname(selector)) if selector.endswith("selector.py") and os.path.isdir(os.path.join(os.path.dirname(selector), "br")) is False else os.path.dirname(selector), os.path.join(data, "tests.json"), BENCH, BENCH)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd="/sg")
    try:
        return float(out.stdout.strip().splitlines()[-1])
    except Exception:
        sys.stderr.write(out.stderr[-500:])
        return float("nan")


before = machine_factor()
env = dict(os.environ, BR_DATA=data, BR_MAPS=maps, BR_SG="/sg")
out = "/tmp/output.jsonl"
started = time.time()
cpu_start = container_cpu()
with open(out, "wb") as handle:
    code = subprocess.call([sys.executable, selector], stdout=handle, env=env)
cpu = container_cpu() - cpu_start
wall = time.time() - started
after = machine_factor()
factor = (before + after) / 2
print(
    f"exit {code} wall {wall:.1f}s cpu {cpu:.2f}s here; this machine is {factor:.2f}x the "
    f"recorded times ({before:.2f} before, {after:.2f} after), so about {cpu / factor:.1f}s "
    "at the scorer's speed",
    flush=True,
)
subprocess.call(
    [sys.executable, os.path.join(repo, "score.py"), data, os.path.join(data, "answers.json"), out,
     "--cpu-seconds", f"{cpu / factor:.3f}"]
)
