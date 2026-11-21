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
           f"--junitxml {WORK}/pass1.xml "
           "--cov=sqlglot --cov=tests --cov-context=test --cov-report= tests")
    print("+", cmd, flush=True)
    subprocess.run(cmd, shell=True, cwd=SRC, env=env)  # failures tolerated; maps still valid
    # pass 2: class-attribute read recording (no coverage)
    env2 = dict(env, BR_ATTRS="1")
    cmd2 = ("python -m pytest -p maps_plugin -n 4 -q --no-header -p no:cacheprovider "
            f"--junitxml {WORK}/pass2.xml tests")
    print("+", cmd2, flush=True)
    subprocess.run(cmd2, shell=True, cwd=SRC, env=env2)
    # pass 3: which lazily loaded modules (dialects/generators/parsers) does each test need? (no coverage, no pre-import)
    env3 = dict(env, BR_IMPORTS="1")
    cmd3 = ("python -m pytest -p maps_plugin -n 4 -q --no-header -p no:cacheprovider "
            f"--junitxml {WORK}/pass3.xml tests")
    print("+", cmd3, flush=True)
    subprocess.run(cmd3, shell=True, cwd=SRC, env=env3)


def junit_failed(path):
    import xml.etree.ElementTree as ET

    bad = set()
    if not Path(path).exists():
        return None
    for tc in ET.parse(path).getroot().iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "").split(" ")[0].split("[")[0]
        parts = cls.split(".")
        mod = [p for p in parts if p[:1].islower()]
        klass = [p for p in parts if p[:1].isupper()]
        nodeid = "/".join(mod) + ".py" + ("::" + "::".join(klass) if klass else "") + "::" + name
        if any(ch.tag in ("failure", "error") for ch in tc):
            bad.add(nodeid)
    return bad


def name_index():
    """name -> {file: [lines]} for every Name/Attribute reference inside function or lambda bodies."""
    import ast

    out: dict = defaultdict(lambda: defaultdict(set))
    for root in ("sqlglot", "tests"):
        for p in (SRC / root).rglob("*.py"):
            rel = str(p.relative_to(SRC))
            try:
                tree = ast.parse(p.read_text())
            except SyntaxError:
                continue
            for fn in ast.walk(tree):
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    body = fn.body if isinstance(fn.body, list) else [fn.body]
                    for b in body:
                        for n in ast.walk(b):
                            if isinstance(n, ast.Name):
                                out[n.id][rel].add(n.lineno)
                            elif isinstance(n, ast.Attribute):
                                out[n.attr][rel].add(n.lineno)
    return {k: {f: sorted(v) for f, v in d.items()} for k, d in out.items()}


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
    # lazily imported modules (pass 3): module -> tests that fail if it cannot be imported
    imps: dict[str, set] = defaultdict(set)
    for fn in glob.glob(str(OPENS / "imports-*.json")):
        for mod, phase, owner in json.load(open(fn)):
            if phase == "call":
                imps[mod].add(tid(owner))
            elif phase in ("setup", "teardown", "collect"):
                fl = owner.split("::")[0]
                imps[mod] |= by_file.get(fl, set()) if fl else set(range(len(test_ids)))
            else:
                imps[mod] |= set(range(len(test_ids)))
    (OUT / "imports.json").write_text(json.dumps({k: sorted(v) for k, v in imps.items()}, separators=(",", ":")))
    # class-attribute reads (pass 2)
    attrs: dict[str, set] = defaultdict(set)
    for fn in glob.glob(str(OPENS / "attrs-*.json")):
        for phase, owner, keys in json.load(open(fn)):
            if phase == "call":
                ts = {tid(owner)}
            elif phase in ("setup", "teardown", "collect"):
                fl = owner.split("::")[0]
                ts = by_file.get(fl, set()) if fl else set(range(len(test_ids)))
            else:
                ts = set(range(len(test_ids)))
            for k in keys:
                attrs[k] |= ts
    installed = set()
    for fn in glob.glob(str(OPENS / "installed-*.json")):
        installed |= set(json.load(open(fn)))
    f1, f2 = junit_failed(WORK / "pass1.xml"), junit_failed(WORK / "pass2.xml")
    unreliable = sorted(tid(t) for t in (f2 - f1) if t in test_ids) if f1 is not None and f2 is not None else None
    sets2: dict[tuple, int] = {}
    amap = {}
    for k, v in attrs.items():
        key = tuple(sorted(v))
        if key not in sets2:
            sets2[key] = len(sets2)
        amap[k] = sets2[key]
    (OUT / "attrs.json").write_text(json.dumps(
        {"s": [list(k) for k in sets2], "a": amap, "unreliable": unreliable, "installed": sorted(installed),
         "pass1_failed": sorted(f1 or []), "pass2_failed": sorted(f2 or [])}, separators=(",", ":")))
    (OUT / "names.json").write_text(json.dumps(name_index(), separators=(",", ":")))
    print(f"attrs: {len(amap)} keys read; unreliable tests: {unreliable if unreliable is None else len(unreliable)}")
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
