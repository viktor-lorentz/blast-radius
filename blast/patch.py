"""Unified-diff parser/applier for real `git diff` output.

Reads quoted and space-containing paths, mode lines, rename/copy headers, new and
deleted files, binary markers, `index` lines and "No newline at end of file".
Binary and copied files are marked `special`; renames and modes are reported in
fields. Hunks are applied strictly (bad context raises PatchError).
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
    special: bool = False  # binary, copy, or a header we could not read
    old_mode: Optional[str] = None
    new_mode: Optional[str] = None
    rename: bool = False
    copy: bool = False
    binary: bool = False

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


_ESC = {"a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\", '"': '"'}


def _unquote(p: str) -> str:
    """git's C-style quoting: "a/caf\\303\\251.sql" -> a/café.sql."""
    if not (len(p) >= 2 and p[0] == '"' and p[-1] == '"'):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if i + 4 <= len(body) and all(c in "01234567" for c in body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8)); i += 4; continue
            out += _ESC.get(nxt, nxt).encode(); i += 2; continue
        out += ch.encode(); i += 1
    return out.decode("utf-8", "surrogateescape")


def _strip(p: str) -> Optional[str]:
    p = p.rstrip("\r")
    if p.startswith('"'):
        end = p.find('"', 1)
        while end > 0 and p[end - 1] == "\\":
            end = p.find('"', end + 1)
        p = _unquote(p[: end + 1]) if end > 0 else p
    else:
        p = p.split("\t")[0].rstrip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def _header_paths(rest: str):
    """Paths from 'diff --git A B'. Unquoted paths with spaces are ambiguous;
    the halves are taken equal when they can be (mode-only, pure edit)."""
    rest = rest.rstrip("\r")
    if rest.startswith('"'):
        end = rest.find('" ', 1)
        while end > 0 and rest[end - 1] == "\\":
            end = rest.find('" ', end + 1)
        if end < 0:
            return None
        return _strip(rest[: end + 1]), _strip(rest[end + 2:])
    if rest.endswith('"'):
        k = rest.rfind(' "')
        return (_strip(rest[:k]), _strip(rest[k + 1:])) if k > 0 else None
    if rest.startswith("a/"):
        n = len(rest)
        if n % 2 == 1:
            a, b = rest[: n // 2], rest[n // 2 + 1:]
            if rest[n // 2] == " " and b.startswith("b/") and a[2:] == b[2:]:
                return a[2:], b[2:]
        m = re.match(r"a/(\S*) b/(.*)$", rest)
        if m:
            return m.group(1), m.group(2)
    return None


def parse(text: str) -> List[FilePatch]:
    lines = text.split("\n")
    out: List[FilePatch] = []
    i = 0
    cur: Optional[FilePatch] = None
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = FilePatch(None, None)
            hp = _header_paths(ln[len("diff --git "):])
            if hp:
                cur.old_path, cur.new_path = hp
            else:
                cur.special = True  # fixed below by ---/+++ or rename lines when present
            out.append(cur)
            i += 1
            continue
        if cur is not None and not cur.hunks:
            body = ln.rstrip("\r")
            if body.startswith("rename from "):
                cur.rename, cur.old_path = True, _strip(body[12:])
            elif body.startswith("rename to "):
                cur.rename, cur.new_path = True, _strip(body[10:])
            elif body.startswith("copy from "):
                cur.copy, cur.special, cur.old_path = True, True, _strip(body[10:])
            elif body.startswith("copy to "):
                cur.copy, cur.special, cur.new_path = True, True, _strip(body[8:])
            elif body.startswith("old mode "):
                cur.old_mode = body[9:].strip()
            elif body.startswith("new mode "):
                cur.new_mode = body[9:].strip()
            elif body.startswith(("Binary files", "GIT binary patch")):
                cur.binary = cur.special = True
            elif body.startswith("deleted file mode"):
                cur.old_mode = body[18:].strip()
                cur.new_path = None
            elif body.startswith("new file mode"):
                cur.new_mode = body[14:].strip()
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
            if cur.special and not (cur.binary or cur.copy):
                cur.special = False  # header paths were unreadable, ---/+++ gave them
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
