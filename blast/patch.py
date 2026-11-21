"""Minimal, strict unified-diff parser/applier.

Anything unusual (binary, mode-only, rename, bad context) raises PatchError so the
caller can fall back to the conservative answer.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional


class PatchError(Exception):
    pass


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    lines: List[str] = field(default_factory=list)  # each prefixed ' ', '+', '-'


@dataclass
class FilePatch:
    old_path: Optional[str]  # None for new file
    new_path: Optional[str]  # None for deleted file
    hunks: List[Hunk] = field(default_factory=list)
    special: bool = False  # binary / mode / rename / copy ...

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def _strip(p: str) -> Optional[str]:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse(text: str) -> List[FilePatch]:
    lines = text.split("\n")
    out: List[FilePatch] = []
    i = 0
    cur: Optional[FilePatch] = None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch(None, None)
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            if m:
                cur.old_path, cur.new_path = m.group(1), m.group(2)
            else:
                cur.special = True
            out.append(cur)
            i += 1
            continue
        if cur is not None and not cur.hunks:
            if ln.startswith(("rename ", "copy ", "old mode", "new mode", "similarity", "Binary files", "GIT binary patch")):
                cur.special = True
            elif ln.startswith("deleted file mode"):
                cur.new_path = None
            elif ln.startswith("new file mode"):
                cur.old_path = None
        if ln.startswith("--- "):
            if cur is None:
                cur = FilePatch(None, None)
                out.append(cur)
            cur.old_path = _strip(ln[4:])
            i += 1
            if i >= len(lines) or not lines[i].startswith("+++ "):
                raise PatchError("missing +++")
            cur.new_path = _strip(lines[i][4:])
            i += 1
            continue
        m = _HUNK.match(ln)
        if m and cur is not None:
            h = Hunk(int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1),
                     int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1))
            i += 1
            o = n = 0
            while i < len(lines) and (o < h.old_len or n < h.new_len):
                l = lines[i]
                if l.startswith("\\"):
                    i += 1
                    continue
                tag = l[:1]
                if tag == " " or l == "":
                    h.lines.append(" " + l[1:]); o += 1; n += 1
                elif tag == "-":
                    h.lines.append(l); o += 1
                elif tag == "+":
                    h.lines.append(l); n += 1
                else:
                    raise PatchError("bad hunk line")
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
            if o != h.old_len or n != h.new_len:
                raise PatchError("hunk length mismatch")
            cur.hunks.append(h)
            continue
        i += 1
    return out


def apply(old_text: str, fp: FilePatch) -> str:
    """Apply hunks strictly (context must match exactly at the stated position)."""
    old = old_text.split("\n")
    out: List[str] = []
    pos = 0  # index into old
    for h in fp.hunks:
        start = h.old_start - 1 if h.old_len else h.old_start
        if start < pos or start > len(old):
            raise PatchError("hunk out of order")
        out.extend(old[pos:start])
        pos = start
        for l in h.lines:
            tag, body = l[0], l[1:]
            if tag == " ":
                if pos >= len(old) or old[pos] != body:
                    raise PatchError("context mismatch")
                out.append(body); pos += 1
            elif tag == "-":
                if pos >= len(old) or old[pos] != body:
                    raise PatchError("delete mismatch")
                pos += 1
            else:
                out.append(body)
    out.extend(old[pos:])
    return "\n".join(out)
