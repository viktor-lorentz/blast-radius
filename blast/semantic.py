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
    def graph(self):
        if self._base is None:
            self._base = json.loads((self.fp_dir / "graph.json").read_text())
        return self._base

    def run_patched(self, patched: Dict[str, str]) -> dict:
        from blast.importgraph import dependents, module_name

        start = {module_name(rel) for rel in patched}
        closure = sorted(dependents(self.graph, start))
        tmp = os.environ.get("TMPDIR", "/tmp")
        paths = []
        try:
            for obj in (patched, closure):
                fd, path = tempfile.mkstemp(prefix="brp", suffix=".json", dir=tmp)
                with os.fdopen(fd, "w") as f:
                    json.dump(obj, f)
                paths.append(path)
            env = dict(os.environ, PYTHONPATH=str(HERE), PYTHONHASHSEED="0", PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run(
                [sys.executable, "-m", "blast.fingerprint", "--compare", str(self.src), paths[0],
                 str(self.fp_dir / "fp_noisy.json"), str(self.fp_dir / "fp_base.json"), paths[1]],
                env=env, capture_output=True, text=True, timeout=60)
            return json.loads(r.stdout)
        finally:
            for p in paths:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _rel(self, filename: str):
        root = str(self.src) + os.sep
        if filename.startswith(root):
            return filename[len(root):]
        # baseline built under a different root: keep the part from sqlglot/ on
        i = filename.find("sqlglot" + os.sep)
        return filename[i:] if i >= 0 else None

    def _fn_tests(self, info, patched=None) -> Set[int]:
        rel = self._rel(info[0])
        if rel is None:
            raise NeedAll(f"function outside source tree: {info[0]}")
        a, b = info[1], info[2]
        if patched and rel in patched:  # line numbers refer to the patched text: map back to the original
            a, b = self._to_old(rel, patched[rel], a, b)
        tests, _imp, _hit = self.maps.region_tests(rel, a, b)
        return tests

    def _to_old(self, rel, new_text, a, b):
        import difflib
        old = (self.src / rel).read_text().split("\n")
        new = new_text.split("\n")
        olds = []
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
            lo, hi = max(j1, a - 1), min(j2, b)
            if lo >= hi:
                continue
            if tag == "equal":
                olds += [i1 + (lo - j1) + 1, i1 + (hi - j1)]
            elif i2 > i1:
                olds += [i1 + 1, i2]
        if not olds:
            raise NeedAll(f"cannot map patched lines {rel}:{a}-{b}")
        return min(olds), max(olds)

    def tests_for(self, patched: Dict[str, str]) -> Set[int]:
        new = self.run_patched(patched)
        if not new.get("ok"):
            raise NeedAll(f"patched import failed: {new.get('error')}")
        if new.get("newly_failed"):
            raise NeedAll(f"modules fail to import: {new['newly_failed'][:3]}")
        changed = new["changed"]
        bfn, nfn = new.get("bfn", {}), new.get("nfn", {})
        out: Set[int] = set()
        m = self.maps
        for k, (in_base, in_new) in changed.items():
            kind = k[0]
            if kind == "K":
                cls = k[2:]
                pre = f"C|{cls}|"
                if not any(x.startswith(pre) and (a != b) for x, (a, b) in changed.items()):
                    raise NeedAll(f"class bases changed: {cls}")
                continue  # added/removed attributes are handled as C| keys
            if k in bfn or k in nfn:
                if k in bfn:
                    out |= self._fn_tests(bfn[k])
                if k in nfn and k not in bfn:
                    if in_base:  # non-function value (e.g. property / staticmethod) became a function
                        # the same code object ran as the old wrapper's getter: everyone who executed it
                        out |= self._fn_tests(nfn[k], patched)
                        attr = k.rsplit("|", 1)[1]
                        if kind == "C" and m.attr_installed(k):
                            out |= m.attr_tests(k)
                        elif kind == "C":
                            out |= m.attr_name_tests(attr)
                        else:
                            out |= m.name_tests(attr)
                    else:
                        raise NeedAll(f"new function object: {k}")
                if kind == "C" and in_base and k not in bfn:
                    out |= m.attr_tests(k)
                continue
            if kind == "C":
                attr = k.rsplit("|", 1)[1]
                if in_base and not m.attr_installed(k):
                    # value we could not instrument (descriptor etc.): anyone touching that name
                    out |= m.attr_name_tests(attr) | m.name_tests(attr)
                elif in_base:
                    out |= m.attr_tests(k)
                else:  # new attribute shadows an inherited one somewhere: anyone reading that name
                    out |= m.attr_name_tests(k.rsplit("|", 1)[1])
                out |= m.attr_unreliable()
            elif kind == "G":
                out |= m.name_tests(k.rsplit("|", 1)[1])
            else:
                raise NeedAll(f"unknown key {k}")
        return out
