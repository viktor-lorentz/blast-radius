"""Reader for the committed dependency maps (maps/index.json + maps/cov/*.json)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Set


class Maps:
    def __init__(self, root: Path, id_to_number: Dict[str, int]):
        self.root = Path(root)
        idx = json.loads((self.root / "index.json").read_text())
        self.sha = idx["sha"]
        # map-index -> test number (or -1 when the scorer's tests.json does not list it)
        self.num = [id_to_number.get(t, -1) for t in idx["tests"]]
        self._files = idx["files"]
        self._opens = idx["opens"]
        self.pyfiles = set(idx["pyfiles"])
        self._cov: Dict[str, Optional[dict]] = {}

    def _nums(self, idxs) -> Set[int]:
        num = self.num
        return {num[i] for i in idxs if num[i] >= 0}

    def file_tests(self, relfile: str) -> Set[int]:
        return self._nums(self._files.get(relfile, ()))

    def opened_by(self, relpath: str) -> Optional[Set[int]]:
        v = self._opens.get(relpath)
        return None if v is None else self._nums(v)

    def cov(self, relfile: str) -> Optional[dict]:
        if relfile not in self._cov:
            p = self.root / "cov" / (relfile.replace("/", "__") + ".json")
            self._cov[relfile] = json.loads(p.read_text()) if p.exists() else None
        return self._cov[relfile]

    def region_tests(self, relfile: str, start: int, end: int):
        """(tests that executed lines start..end at test time, any_import_time_hit, any_line_hit)."""
        c = self.cov(relfile)
        if c is None:
            return set(), False, False
        sets, lmap = c["s"], c["l"]
        acc: Set[int] = set()
        imp = hit = False
        seen_sets = set()
        for ln in range(start, end + 1):
            code = lmap.get(str(ln))
            if code is None:
                continue
            hit = True
            if code & 1:
                imp = True
            si = code >> 1
            if si not in seen_sets:
                seen_sets.add(si)
                acc.update(sets[si])
        return self._nums(acc), imp, hit


class _Lazy:
    pass


def _attach_semantic_maps():
    """Adds attrs/names lookups to Maps (kept separate so the hot path stays lean)."""

    def _attrs(self):
        if not hasattr(self, "_attrs_cache"):
            p = self.root / "attrs.json"
            self._attrs_cache = json.loads(p.read_text()) if p.exists() else None
        return self._attrs_cache

    def attr_tests(self, key: str) -> Set[int]:
        a = _attrs(self)
        if a is None:
            raise RuntimeError("attrs map missing")
        si = a["a"].get(key)
        return set() if si is None else self._nums(a["s"][si])

    def attr_name_tests(self, attr: str) -> Set[int]:
        a = _attrs(self)
        if a is None:
            raise RuntimeError("attrs map missing")
        suffix = "|" + attr
        acc: Set[int] = set()
        for k, si in a["a"].items():
            if k.endswith(suffix):
                acc.update(a["s"][si])
        return self._nums(acc)

    def attr_installed(self, key: str) -> bool:
        a = _attrs(self)
        if a is None or "installed" not in a:
            return False
        if not hasattr(self, "_installed_set"):
            self._installed_set = set(a["installed"])
        return key in self._installed_set

    def attr_unreliable(self) -> Set[int]:
        a = _attrs(self)
        if a is None or a.get("unreliable") is None:
            raise RuntimeError("attrs reliability unknown")
        return self._nums(a["unreliable"])

    def name_tests(self, name: str) -> Set[int]:
        if not hasattr(self, "_names_cache"):
            self._names_cache = json.loads((self.root / "names.json").read_text())
        acc: Set[int] = set()
        for rel, lines in self._names_cache.get(name, {}).items():
            c = self.cov(rel)
            if c is None:
                continue
            sets, lmap = c["s"], c["l"]
            for ln in lines:
                code = lmap.get(str(ln))
                if code is not None:
                    acc.update(self._nums(sets[code >> 1]))
        return acc

    Maps.attr_tests = attr_tests
    Maps.attr_name_tests = attr_name_tests
    Maps.attr_unreliable = attr_unreliable
    Maps.attr_installed = attr_installed
    Maps.name_tests = name_tests


_attach_semantic_maps()
