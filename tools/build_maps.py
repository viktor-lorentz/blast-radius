"""Build the dependency maps. Runs INSIDE the pinned python:3.13-slim image (see .github/workflows/maps.yml).

    python /repo/tools/build_maps.py /repo /repo/maps

1. fetch pristine sqlglot at the pinned commit
2. run its whole test-suite under coverage with one context per test, tracing file opens
3. write compact maps:  maps/index.json  and  maps/cov/<file>.json
"""
from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
REPO = Path(sys.argv[1])
OUT = Path(sys.argv[2])
WORK = Path("/work")
SRC = WORK / "src"
OPENS = WORK / "opens"


def sh(cmd, **kw):
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, check=True, **kw)


def run_suite():
    WORK.mkdir(exist_ok=True)
    sh(f"python {REPO}/tools/fetch_src.py {SHA} {SRC}")
    sh(f"pip install -q -r {REPO}/requirements.lock coverage pytest-cov")
    env = dict(os.environ, BR_SRC_ROOT=str(SRC), BR_OPENS_DIR=str(OPENS),
               PYTHONPATH=f"{REPO}/tools:{SRC}", PYTHONDONTWRITEBYTECODE="1")
    env.pop("COVERAGE_CORE", None)
    cmd = ("python -m pytest -p maps_plugin -n 4 -q --no-header -p no:cacheprovider "
           "--cov=sqlglot --cov=tests --cov-context=test --cov-report= tests")
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, cwd=SRC, env=env)  # failures tolerated; maps still valid


def main():
    run_suite()
    import coverage

    data_file = SRC / ".coverage"
    cov = coverage.Coverage(data_file=str(data_file))
    cov.load()
    data = cov.get_data()

    test_ids: dict[str, int] = {}
    by_file: dict[str, set] = defaultdict(set)

    def tid(nodeid: str) -> int:
        if nodeid not in test_ids:
            test_ids[nodeid] = len(test_ids)
            by_file[nodeid.split("::")[0]].add(test_ids[nodeid])
        return test_ids[nodeid]

    parsed = {}
    for f in data.measured_files():
        rel = os.path.relpath(f, SRC)
        if rel.startswith(".."):
            continue
        parsed[rel] = data.contexts_by_lineno(f)
        for ctxs in parsed[rel].values():
            for c in ctxs:
                if "|" in c:
                    tid(c.rsplit("|", 1)[0])

    cov_dir = OUT / "cov"
    cov_dir.mkdir(parents=True, exist_ok=True)
    for old in cov_dir.glob("*.json"):
        old.unlink()
    for rel, lines in parsed.items():
        sets: dict[tuple, int] = {}
        lmap = {}
        for ln, ctxs in lines.items():
            ts = set()
            imp = 0
            for c in ctxs:
                if "|" not in c:
                    imp = 1
                    continue
                node, phase = c.rsplit("|", 1)
                i = tid(node)
                if phase == "run":
                    ts.add(i)
                else:  # setup/teardown (setUpClass, fixtures): every test of that file may break
                    ts |= by_file[node.split("::")[0]]
            key = tuple(sorted(ts))
            if key not in sets:
                sets[key] = len(sets)
            lmap[str(ln)] = (sets[key] << 1) | imp
        (cov_dir / (rel.replace("/", "__") + ".json")).write_text(
            json.dumps({"s": [list(k) for k in sets], "l": lmap}, separators=(",", ":")))

    # opened data files
    opens: dict[str, set] = defaultdict(set)
    for fn in glob.glob(str(OPENS / "opens-*.json")):
        for rel, phase, owner in json.load(open(fn)):
            if phase == "call":
                opens[rel].add(tid(owner))
            elif phase in ("setup", "teardown", "collect"):
                fl = owner.split("::")[0]
                opens[rel] |= by_file.get(fl, set()) if fl else set(range(len(test_ids)))
            else:
                opens[rel] |= set(range(len(test_ids)))
    pyfiles = sorted(str(p.relative_to(SRC)) for p in (SRC / "sqlglot").rglob("*.py"))
    idx = {
        "sha": SHA,
        "tests": [k for k, _ in sorted(test_ids.items(), key=lambda kv: kv[1])],
        "files": {k: sorted(v) for k, v in by_file.items()},
        "opens": {k: sorted(v) for k, v in opens.items()},
        "pyfiles": pyfiles,
    }
    (OUT / "index.json").write_text(json.dumps(idx, separators=(",", ":")))
    print(f"maps: {len(test_ids)} tests, {len(parsed)} files, {len(opens)} opened data files")


if __name__ == "__main__":
    main()
