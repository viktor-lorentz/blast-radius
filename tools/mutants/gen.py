"""Generate a validation corpus of mutant patches against pristine sqlglot (VALIDATION ONLY, never shipped).

    python tools/mutants/gen.py SRC_DIR OUT_DIR --n 400 --seed 1

Writes OUT_DIR/commits/<id>.patch and OUT_DIR/kinds.json. Ground truth comes from truth.py.
Mimics the practice kinds (code / fixture / comment / multi) and adds unseen kinds.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import random
import re
import string
from pathlib import Path

CMP_SWAPS = [(" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "), (" == ", " != "),
             (" != ", " == "), (" is not ", " is "), (" is ", " is not "), (" is ", " == "),
             (" not in ", " in "), (" in ", " not in ")]
BOOL_SWAPS = [(" and ", " or "), (" or ", " and ")]
CONST_SWAPS = [("True", "False"), ("False", "True")]
COMMENTS = ["# Kept for backwards compatibility", "# Cheap check first", "# Keep in sync with the generator",
            "# TODO: revisit", "# NOTE: order matters here"]
JUNK = ["42 ", "x ", "0 ", "foo ", "( ", "1 + "]


def mk_patch(path: str, old: str, new: str) -> str:
    d = difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                             f"a/{path}", f"b/{path}", n=3)
    body = "".join(d)
    if not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{path} b/{path}\n{body}"


def code_lines(src: str):
    """Line numbers that hold executable code (not comments/docstrings/blank)."""
    tree = ast.parse(src)
    doc_lines = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) \
                    and isinstance(b[0].value.value, str):
                doc_lines.update(range(b[0].lineno, b[0].end_lineno + 1))
    lines = src.split("\n")
    out = []
    for i, l in enumerate(lines, 1):
        s = l.strip()
        if s and not s.startswith("#") and i not in doc_lines:
            out.append(i)
    return out, doc_lines


def try_line_mutation(rng, line: str):
    ops = []
    for a, b in CMP_SWAPS + BOOL_SWAPS:
        if a in line:
            ops.append(("swap", a, b))
    for a, b in CONST_SWAPS:
        if re.search(rf"\b{a}\b", line):
            ops.append(("word", a, b))
    for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
        ops.append(("num", m.start(), m.end()))
    if re.search(r"\bnot ", line):
        ops.append(("dropnot", None, None))
    for m in re.finditer(r"\"([A-Za-z_ ]{2,})\"", line):
        ops.append(("str", m.start(1), m.end(1)))
    if not ops:
        return None
    kind, a, b = rng.choice(ops)
    if kind == "swap":
        idx = [m.start() for m in re.finditer(re.escape(a), line)]
        i = rng.choice(idx)
        return line[:i] + b + line[i + len(a):]
    if kind == "word":
        idx = [m.start() for m in re.finditer(rf"\b{a}\b", line)]
        i = rng.choice(idx)
        return line[:i] + b + line[i + len(a):]
    if kind == "num":
        n = int(line[a:b])
        return line[:a] + str(n + rng.choice([1, -1] if n else [1])) + line[b:]
    if kind == "dropnot":
        return re.sub(r"\bnot ", "", line, count=1)
    if kind == "str":
        s = line[a:b]
        j = rng.randrange(len(s))
        return line[:a] + s[:j] + rng.choice(string.ascii_uppercase) + s[j + 1:] + line[b:]
    return None


def mutate_code(rng, src: str):
    lines_idx, _ = code_lines(src)
    lines = src.split("\n")
    for _ in range(60):
        i = rng.choice(lines_idx)
        new_line = try_line_mutation(rng, lines[i - 1])
        if new_line is None or new_line == lines[i - 1]:
            continue
        new = lines[:]
        new[i - 1] = new_line
        new_src = "\n".join(new)
        try:
            if ast.dump(ast.parse(new_src)) == ast.dump(ast.parse(src)):
                continue
        except SyntaxError:
            continue
        return new_src
    return None


def add_comment(rng, src: str):
    lines_idx, _ = code_lines(src)
    lines = src.split("\n")
    i = rng.choice(lines_idx)
    indent = re.match(r"\s*", lines[i - 1]).group(0)
    new = lines[: i - 1] + [indent + rng.choice(COMMENTS)] + lines[i - 1:]
    return "\n".join(new)


def mutate_fixture(rng, text: str):
    lines = text.split("\n")
    cand = [i for i, l in enumerate(lines) if l.strip() and not l.startswith(("#", "--"))]
    if not cand:
        return None
    i = rng.choice(cand)
    l = lines[i]
    words = [m.start() for m in re.finditer(r"\b\w", l)]
    if not words:
        return None
    j = rng.choice(words)
    lines[i] = l[:j] + rng.choice(JUNK) + l[j:]
    return "\n".join(lines)


def syntax_error(rng, src: str):
    lines_idx, _ = code_lines(src)
    if not lines_idx:
        return None
    lines = src.split("\n")
    i = rng.choice(lines_idx)
    lines[i - 1] = lines[i - 1] + rng.choice([" )", " :", " ="])
    return "\n".join(lines)


def delete_statement(rng, src: str):
    tree = ast.parse(src)
    cands = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.Expr, ast.AugAssign, ast.Return))
             and n.lineno == n.end_lineno and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if not cands:
        return None
    n = rng.choice(cands)
    lines = src.split("\n")
    l = lines[n.lineno - 1]
    indent = re.match(r"\s*", l).group(0)
    lines[n.lineno - 1] = indent + "pass"
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def edit_docstring(rng, src: str):
    _, doc = code_lines(src)
    if not doc:
        return None
    lines = src.split("\n")
    i = rng.choice(sorted(doc))
    l = lines[i - 1]
    if not l.strip() or '"""' in l and len(l.strip()) <= 3:
        return None
    lines[i - 1] = l.rstrip() + " (updated)"
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def remove_decorator(rng, src: str):
    tree = ast.parse(src)
    cands = [d for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.ClassDef))
             for d in n.decorator_list if d.lineno == d.end_lineno]
    if not cands:
        return None
    d = rng.choice(cands)
    lines = src.split("\n")
    del lines[d.lineno - 1]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def swap_args(rng, src: str):
    tree = ast.parse(src)
    cands = [c for c in ast.walk(tree) if isinstance(c, ast.Call) and len(c.args) >= 2
             and c.lineno == c.end_lineno and all(a.lineno == c.lineno for a in c.args[:2])]
    if not cands:
        return None
    c = rng.choice(cands)
    a, b = c.args[0], c.args[1]
    lines = src.split("\n")
    l = lines[c.lineno - 1]
    ta, tb = l[a.col_offset:a.end_col_offset], l[b.col_offset:b.end_col_offset]
    if ta == tb:
        return None
    lines[c.lineno - 1] = l[:a.col_offset] + tb + l[a.end_col_offset:b.col_offset] + ta + l[b.end_col_offset:]
    new = "\n".join(lines)
    try:
        ast.parse(new)
    except SyntaxError:
        return None
    return new


def duplicate_stmt(rng, src: str):
    tree = ast.parse(src)
    cands = [n for n in ast.walk(tree) if isinstance(n, (ast.Expr, ast.AugAssign, ast.Assign))
             and n.lineno == n.end_lineno and not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]
    if not cands:
        return None
    n = rng.choice(cands)
    lines = src.split("\n")
    lines.insert(n.lineno, lines[n.lineno - 1])
    return "\n".join(lines)


def whitespace(rng, src: str):
    lines_idx, _ = code_lines(src)
    lines = src.split("\n")
    i = rng.choice(lines_idx)
    return "\n".join(lines[:i] + [""] + lines[i:])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--n", type=int, default=400)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--only", default="")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    src = Path(a.src)
    out = Path(a.out)
    (out / "commits").mkdir(parents=True, exist_ok=True)
    pkg = sorted(p for p in (src / "sqlglot").rglob("*.py"))
    weights = [max(1, p.stat().st_size) for p in pkg]
    tests_py = sorted(p for p in (src / "tests").rglob("test_*.py"))
    fixtures = sorted(p for p in (src / "tests" / "fixtures").rglob("*.sql"))
    kinds = {}

    def rel(p):
        return str(p.relative_to(src))

    def one_code(fn=mutate_code, pool=None, w=None):
        for _ in range(30):
            p = rng.choices(pool or pkg, weights=w if pool is None else None)[0] if pool is None else rng.choice(pool)
            old = p.read_text()
            try:
                new = fn(rng, old)
            except (IndexError, ValueError):
                continue
            if new and new != old:
                return rel(p), old, new
        return None

    mix = [("code", 50), ("fixture", 18), ("comment", 6), ("multi", 8), ("syntax", 3), ("testfile", 5),
           ("delete", 4), ("docstring", 3), ("whitespace", 2), ("misc_file", 1)]
    if a.seed != 1:
        mix += [("decorator", 3), ("swap_args", 4), ("duplicate", 3), ("helpers", 2)]
    if a.only:
        mix = [(k, w) for k, w in mix if k in a.only.split(",")] + [(k, 1) for k in a.only.split(",") if k not in dict(mix)]
    names, wts = zip(*mix)
    made = 0
    while made < a.n:
        kind = rng.choices(names, weights=wts)[0]
        parts = []
        if kind == "code":
            r = one_code(pool=None, w=weights)
            r and parts.append(r)
        elif kind == "fixture":
            p = rng.choice(fixtures)
            old = p.read_text()
            new = mutate_fixture(rng, old)
            if new:
                parts.append((rel(p), old, new))
        elif kind == "comment":
            r = one_code(fn=add_comment)
            r and parts.append(r)
        elif kind == "multi":
            seen = set()
            for sub in rng.sample(["code", "code", "comment", "fixture"], k=rng.choice([2, 3])):
                if sub == "fixture":
                    p = rng.choice(fixtures)
                    old = p.read_text()
                    new = mutate_fixture(rng, old)
                    r = (rel(p), old, new) if new else None
                else:
                    r = one_code(fn=mutate_code if sub == "code" else add_comment)
                if r and r[0] not in seen:
                    seen.add(r[0])
                    parts.append(r)
        elif kind == "syntax":
            r = one_code(fn=syntax_error)
            r and parts.append(r)
        elif kind == "testfile":
            r = one_code(fn=mutate_code, pool=tests_py)
            r and parts.append(r)
        elif kind == "delete":
            r = one_code(fn=delete_statement)
            r and parts.append(r)
        elif kind == "docstring":
            r = one_code(fn=edit_docstring)
            r and parts.append(r)
        elif kind == "decorator":
            r = one_code(fn=remove_decorator)
            r and parts.append(r)
        elif kind == "swap_args":
            r = one_code(fn=swap_args)
            r and parts.append(r)
        elif kind == "duplicate":
            r = one_code(fn=duplicate_stmt)
            r and parts.append(r)
        elif kind == "helpers":
            r = one_code(fn=mutate_code, pool=[src / "tests" / "helpers.py", src / "tests" / "dialects" / "test_dialect.py"])
            r and parts.append(r)
        elif kind == "iseq":
            def _iseq(rng, src):
                tree = ast.parse(src)
                def safe(n):
                    return (isinstance(n, ast.Constant) and n.value is None) or (
                        isinstance(n, ast.Attribute) and n.attr.isupper() and isinstance(n.value, (ast.Name, ast.Attribute)))
                cands = [c for c in ast.walk(tree) if isinstance(c, ast.Compare) and len(c.ops) == 1
                         and isinstance(c.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq))
                         and (safe(c.left) or safe(c.comparators[0])) and c.lineno == c.end_lineno]
                if not cands:
                    return None
                c = rng.choice(cands)
                lines = src.split("\n")
                l = lines[c.lineno - 1]
                seg = l[c.left.end_col_offset:c.comparators[0].col_offset]
                rep = {" is not ": " != ", " != ": " is not ", " is ": " == ", " == ": " is "}
                for a, b in rep.items():
                    if a in seg:
                        seg2 = seg.replace(a, b, 1)
                        break
                else:
                    return None
                lines[c.lineno - 1] = l[:c.left.end_col_offset] + seg2 + l[c.comparators[0].col_offset:]
                return "\n".join(lines)
            r = one_code(fn=_iseq)
            r and parts.append(r)
        elif kind == "importerr":
            # a module-level statement that raises at import time
            def _imperr(rng, src):
                tree = ast.parse(src)
                tops = [n for n in tree.body if isinstance(n, (ast.Assign, ast.ClassDef, ast.FunctionDef))]
                if not tops:
                    return None
                n = rng.choice(tops)
                lines = src.split("\n")
                lines.insert(n.lineno - 1, "_broken = 1 / 0")
                return "\n".join(lines)
            r = one_code(fn=_imperr)
            r and parts.append(r)
        elif kind == "whitespace":
            r = one_code(fn=whitespace)
            r and parts.append(r)
        elif kind == "misc_file":
            p = src / "README.md"
            old = p.read_text()
            parts.append((rel(p), old, old.replace("SQLGlot", "SQLGlot ", 1)))
        if not parts:
            continue
        cid = f"m{a.seed:02d}{made:04d}"
        text = "".join(mk_patch(path, old, new) for path, old, new in parts)
        (out / "commits" / f"{cid}.patch").write_text(text)
        kinds[cid] = kind
        made += 1
    (out / "kinds.json").write_text(json.dumps(kinds, indent=0))
    print(f"generated {made} mutants into {out}")


if __name__ == "__main__":
    main()
