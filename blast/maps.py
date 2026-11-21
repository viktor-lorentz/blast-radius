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
