"""Ground truth for a corpus shard (VALIDATION ONLY). Runs INSIDE the pinned image.

    python truth.py REPO CORPUS_DIR SHARD NSHARDS OUT_JSON

For each patch: apply to a fresh copy of pristine sqlglot, run the full suite, record failing node ids
(failures, errors, timeouts, and every test of a file that cannot be imported/collected).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO, CORPUS, SHARD, NSHARDS, OUT = Path(sys.argv[1]), Path(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]), Path(sys.argv[5])
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PRISTINE = Path("/work/pristine")
WORK = Path("/work/tree")

sys.path.insert(0, str(REPO))
from blast import patch as P  # noqa: E402


def sh(cmd, **kw):
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, check=True, **kw)


def all_test_ids():
    tests = json.loads((REPO / "sample" / "tests.json").read_text())["tests"]
    return [t["id"] for t in tests]


def main():
    Path("/work").mkdir(exist_ok=True)
    if not PRISTINE.exists():
        sh(f"python {REPO}/tools/fetch_src.py {SHA} {PRISTINE}")
    sh(f"pip install -q -r {REPO}/requirements.lock")
    ids = all_test_ids()
    by_file = {}
    for t in ids:
        by_file.setdefault(t.split("::")[0], []).append(t)
    patches = sorted((CORPUS / "commits").glob("*.patch"))
    mine = [p for i, p in enumerate(patches) if i % NSHARDS == SHARD]
    results = {}
    if OUT.exists():
        results = json.loads(OUT.read_text())
    for pf in mine:
        cid = pf.stem
        if cid in results:
            continue
        shutil.rmtree(WORK, ignore_errors=True)
        shutil.copytree(PRISTINE, WORK, symlinks=True)
        try:
            for fp in P.parse(pf.read_text()):
                old = (WORK / fp.old_path).read_text()
                (WORK / fp.new_path).write_text(P.apply(old, fp))
        except Exception as e:  # noqa: BLE001
            print("apply failed", cid, e)
            continue
        xml = Path("/work/junit.xml")
        xml.unlink(missing_ok=True)
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=str(WORK))
        subprocess.run(
            "python -m pytest -n 4 -q -p no:cacheprovider --timeout 300 --continue-on-collection-errors "
            f"--junitxml {xml} tests", shell=True, cwd=WORK, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1800)
        failing = set()
        collected = set()
        if xml.exists():
            root = ET.parse(xml).getroot()
            for tc in root.iter("testcase"):
                cls = tc.get("classname", "")
                name = tc.get("name", "").split(" ")[0].split("[")[0]
                parts = cls.split(".")
                # classname like tests.dialects.test_x.TestX -> tests/dialects/test_x.py::TestX::name
                mod = [p for p in parts if p[:1].islower()]
                klass = [p for p in parts if p[:1].isupper()]
                nodeid = "/".join(mod) + ".py" + ("::" + "::".join(klass) if klass else "") + "::" + name
                bad = any(ch.tag in ("failure", "error") for ch in tc)
                if not klass and not name:  # collection error entry
                    continue
                collected.add(nodeid)
                if bad:
                    failing.add(nodeid)
                for ch in tc:
                    if ch.tag == "error" and ("collect" in (ch.get("message") or "").lower()):
                        failing.update(by_file.get("/".join(mod) + ".py", []))
        # tests that never reported (file failed to import/collect) count as failing
        failing.update(t for t in ids if t not in collected)
        results[cid] = sorted(failing)
        OUT.write_text(json.dumps(results))
        print(cid, len(failing), flush=True)


if __name__ == "__main__":
    main()
