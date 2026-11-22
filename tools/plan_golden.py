"""Build-time: TPC-DS plan digests of the pristine tree for blast.plans.

    python tools/plan_golden.py SRC_ROOT OUT_DIR
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from blast import plans  # noqa: E402

src, out = sys.argv[1], sys.argv[2]
golden = plans._run(Path(src), {}, None)
if golden.get("ok") and golden.get("rows"):
    os.makedirs(out, exist_ok=True)
    Path(out, "tpcds_golden.json").write_text(json.dumps({"rows": golden["rows"]}))
