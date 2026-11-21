"""Build-time (runs in the runtime image): baseline fingerprints + noisy-key list.

    python tools/fp_baseline.py SRC_ROOT OUT_DIR
"""
import json
import os
import subprocess
import sys

root, out = sys.argv[1], sys.argv[2]
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(seed, noisy_file=None):
    env = dict(os.environ, PYTHONHASHSEED=str(seed), PYTHONPATH=here)
    cmd = [sys.executable, "-m", "blast.fingerprint", root, "-"] + ([noisy_file] if noisy_file else [])
    return json.loads(subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stdout)


def flat(r):
    return {k: v for d in r["bymod"].values() for k, v in d.items()}


runs = [run(s) for s in range(1, 9)]
for r in runs:
    r["fp"] = flat(r)
keys = set().union(*(r["fp"] for r in runs))
noisy = sorted(k for k in keys if len({r["fp"].get(k) for r in runs}) > 1)
os.makedirs(out, exist_ok=True)
nf = os.path.join(out, "fp_noisy.json")
with open(nf, "w") as f:
    json.dump(noisy, f)
base = [run(s, nf) for s in range(11, 17)]
for r in base:
    r["fp"] = flat(r)
still = sorted({k for b in base[1:] for k in base[0]["fp"] if base[0]["fp"][k] != b["fp"].get(k)})
with open(os.path.join(out, "fp_base.json"), "w") as f:
    json.dump({"mods": base[0]["mods"], "failed": base[0]["failed"], "bymod": base[0]["bymod"],
               "fn": base[0]["fn"], "dropped": still}, f, separators=(",", ":"))
sys.path.insert(0, here)
from blast.importgraph import build  # noqa: E402

with open(os.path.join(out, "graph.json"), "w") as f:
    json.dump(build(root), f, separators=(",", ":"))
from blast.astdiff import collect_enums  # noqa: E402

with open(os.path.join(out, "enums.json"), "w") as f:
    json.dump(sorted(collect_enums(root)), f)
print(f"baseline: {len(base[0]['fp'])} keys, noisy {len(noisy)}, dropped {len(still)}", file=sys.stderr)
