"""Import-time semantic diff: which module globals / class attributes does a patch change, and which tests
read them. Runs the patched import in a fresh interpreter (blast.fingerprint) and compares to the baseline
fingerprints computed at image build time in the same environment."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Set

HERE = Path(__file__).resolve().parent.parent


class NeedAll(Exception):
    pass


class Semantic:
    def __init__(self, maps, fp_dir: Path, src: Path):
        self.maps = maps
        self.fp_dir = Path(fp_dir)
        self.src = Path(src)
        self._base = None

    @property
    def base(self):
        if self._base is None:
            self._base = json.loads((self.fp_dir / "fp_base.json").read_text())
        return self._base

    def run_patched(self, patched: Dict[str, str]) -> dict:
        fd, path = tempfile.mkstemp(prefix="brp", suffix=".json", dir=os.environ.get("TMPDIR", "/tmp"))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(patched, f)
            env = dict(os.environ, PYTHONPATH=str(HERE), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run(
                [sys.executable, "-m", "blast.fingerprint", str(self.src), path, str(self.fp_dir / "fp_noisy.json")],
                env=env, capture_output=True, text=True, timeout=60)
            return json.loads(r.stdout)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _rel(self, filename: str):
        root = str(self.src) + os.sep
        if filename.startswith(root):
            return filename[len(root):]
        # baseline built under a different root: keep the part from sqlglot/ on
        i = filename.find("sqlglot" + os.sep)
        return filename[i:] if i >= 0 else None

    def _fn_tests(self, info) -> Set[int]:
        rel = self._rel(info[0])
        if rel is None:
            raise NeedAll(f"function outside source tree: {info[0]}")
        tests, _imp, _hit = self.maps.region_tests(rel, info[1], info[2])
        return tests

    def tests_for(self, patched: Dict[str, str]) -> Set[int]:
        new = self.run_patched(patched)
        base = self.base
        if not new.get("ok"):
            raise NeedAll(f"patched import failed: {new.get('error')}")
        newly_failed = set(new.get("failed", [])) - set(base.get("failed", []))
        if newly_failed:
            raise NeedAll(f"modules fail to import: {sorted(newly_failed)[:3]}")
        bfp, nfp = base["fp"], new["fp"]
        bfn, nfn = base.get("fn", {}), new.get("fn", {})
        dropped = set(base.get("dropped", []))
        changed = [k for k in set(bfp) | set(nfp)
                   if k not in dropped and bfp.get(k) != nfp.get(k)]
        out: Set[int] = set()
        m = self.maps
        for k in changed:
            kind = k[0]
            if kind == "K":
                cls = k[2:]
                pre = f"C|{cls}|"
                battrs = {x for x in bfp if x.startswith(pre)}
                nattrs = {x for x in nfp if x.startswith(pre)}
                if battrs == nattrs:
                    raise NeedAll(f"class bases changed: {cls}")
                continue  # added/removed attributes are handled as C| keys
            if k in bfn or k in nfn:
                if k in bfn:
                    out |= self._fn_tests(bfn[k])
                if k in nfn and k not in bfn:
                    raise NeedAll(f"new function object: {k}")
                if kind == "C" and k in bfp and k in nfp and k not in bfn:
                    out |= m.attr_tests(k)
                continue
            if kind == "C":
                if k in bfp:
                    out |= m.attr_tests(k)
                else:  # new attribute shadows an inherited one somewhere: anyone reading that name
                    out |= m.attr_name_tests(k.rsplit("|", 1)[1])
                out |= m.attr_unreliable()
            elif kind == "G":
                name = k.rsplit("|", 1)[1]
                out |= m.name_tests(name)
            else:
                raise NeedAll(f"unknown key {k}")
        return out
