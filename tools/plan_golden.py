"""Build-time: TPC-DS plan digests and call coverage of the pristine tree for blast.plans.

    python tools/plan_golden.py SRC_ROOT OUT_DIR
"""
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast import plans  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
golden = plans._run(Path(src), {}, None)
if golden.get("ok") and golden.get("rows"):
    os.makedirs(out, exist_ok=True)
    Path(out, "tpcds_golden.json").write_text(json.dumps({"rows": golden["rows"]}))
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
    env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
    recs = {}
    for order in ("fwd", "rev"):
        target = str(Path(out, f"cover_{order}.json"))
        proc = subprocess.run([sys.executable, "-m", "blast.plans", "--cover", str(Path(src).resolve()), target, order],
                              env=env, cwd=src, capture_output=True, text=True, timeout=1200)
        print("cover", order, proc.returncode, proc.stderr[-300:])
        if proc.returncode == 0 and os.path.exists(target):
            recs[order] = json.loads(Path(target).read_text())
        if os.path.exists(target):
            os.unlink(target)
    if len(recs) == 2:
        Path(out, "tpcds_cover.json").write_text(json.dumps(plans.merge_cover(recs["fwd"], recs["rev"])))
