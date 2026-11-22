"""Corpus c6 (VALIDATION ONLY, never shipped): file-level and no-op shapes as real `git diff` output.

    python3 tools/mutants/gen_c6.py SRC_DIR WORK_REPO OUT_DIR --seed 6

SRC_DIR is pristine sqlglot. WORK_REPO is a scratch directory that becomes a git repo of it.
Shapes: new / deleted / renamed / copied files, mode-only and binary changes, config and
unknown files, quoted paths, no newline at EOF, CRLF, and semantic no-ops (annotation-only,
local renames, import reordering, reformatting, pass / parentheses / trailing commas).
Ground truth comes from truth.py (git apply inside the pinned image).
"""
from __future__ import annotations

import argparse
import ast
import io
import json
import random
import re
import shutil
import subprocess
import tokenize
from pathlib import Path

SRC = REPO = OUT = None


def git(*args, check=True):
    out = subprocess.run(["git", "-C", str(REPO), *args], check=check, capture_output=True).stdout
    return out.decode("utf-8", "surrogateescape")  # bytes: keep CRLF exactly


def setup_repo():
    if not (REPO / ".git").exists():
        shutil.rmtree(REPO, ignore_errors=True)
        shutil.copytree(SRC, REPO, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"))
        git("init", "-q")
        git("config", "user.email", "c6@example.invalid")
        git("config", "user.name", "c6")
        git("config", "core.autocrlf", "false")
        git("add", "-A")
        git("commit", "-qm", "pristine")


def reset():
    git("reset", "-q", "--hard")
    git("clean", "-fdq")


def emit(cid, kind, detail, diff_args=("-M",)):
    git("add", "-A")
    d = git("diff", "--cached", *diff_args)
    reset()
    if not d.strip():
        return False
    (OUT / "commits" / f"{cid}.patch").write_bytes(d.encode("utf-8", "surrogateescape"))
    KINDS[cid] = {"kind": kind, "detail": detail}
    return True


KINDS: dict = {}


def p(rel):
    return REPO / rel


def rd(rel):
    return p(rel).read_text()


def wr(rel, text):
    p(rel).parent.mkdir(parents=True, exist_ok=True)
    p(rel).write_text(text)


def pymods(prefix="sqlglot/"):
    return sorted(str(f.relative_to(REPO)) for f in REPO.glob("sqlglot/**/*.py")
                  if "__pycache__" not in str(f) and str(f.relative_to(REPO)).startswith(prefix))


# ---------------------------------------------------------------- semantic no-ops
def funcs(tree):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def replace_spans(src, spans):
    """spans: (lineno, col, end_col, text) on single lines; 1-based lines, byte-free (ascii assumed)."""
    lines = src.split("\n")
    for ln, c0, c1, txt in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
        s = lines[ln - 1]
        lines[ln - 1] = s[:c0] + txt + s[c1:]
    return "\n".join(lines)


def ascii_ok(src):
    return all(ord(ch) < 128 for ch in src)


def rename_local(rng, src):
    tree = ast.parse(src)
    cands = []
    for f in funcs(tree):
        params = {a.arg for a in ast.walk(f.args) if isinstance(a, ast.arg)}
        inner = [n for n in ast.walk(f) if n is not f and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))]
        inner_names = {n.id for i in inner for n in ast.walk(i) if isinstance(n, ast.Name)}
        decl = {x for n in ast.walk(f) if isinstance(n, (ast.Global, ast.Nonlocal)) for x in n.names}
        uses_locals = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("locals", "vars", "eval", "exec") for n in ast.walk(f))
        if uses_locals:
            continue
        stores = {n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        for name in sorted(stores - params - inner_names - decl):
            nodes = [n for n in ast.walk(f) if isinstance(n, ast.Name) and n.id == name]
            if any(n.lineno != n.end_lineno for n in nodes):
                continue
            cands.append((f, name, nodes))
    if not cands:
        return None
    f, name, nodes = rng.choice(cands)
    new = name + rng.choice(["_val", "_tmp", "_local", "2"])
    if re.search(rf"\b{re.escape(new)}\b", src):
        return None
    out = replace_spans(src, [(n.lineno, n.col_offset, n.end_col_offset, new) for n in nodes])
    # f-strings: name inside f"{name}" is a Name node too, so it is covered.
    return out, f"{f.name}:{name}->{new}"


def annotation_only(rng, src, future_required=True):
    has_future = "from __future__ import annotations" in src
    if future_required and not has_future:
        return None
    tree = ast.parse(src)
    cands = []
    for f in funcs(tree):
        if f.returns is not None and f.returns.lineno == f.returns.end_lineno:
            cands.append(("ret", f.returns))
        for a in f.args.args + f.args.kwonlyargs:
            if a.annotation is not None and a.annotation.lineno == a.annotation.end_lineno:
                cands.append(("arg", a.annotation))
        for n in ast.walk(f):
            if isinstance(n, ast.AnnAssign) and n.value is not None and n.annotation.lineno == n.annotation.end_lineno:
                cands.append(("local", n.annotation))
    if not cands:
        return None
    what, node = rng.choice(cands)
    seg = ast.get_source_segment(src, node)
    alt = rng.choice([f"t.Optional[{seg}]", "t.Any", f"{seg} | None", "object"])
    if "t.Any" in alt and not re.search(r"^import typing as t", src, re.M):
        alt = "object"
    return replace_spans(src, [(node.lineno, node.col_offset, node.end_col_offset, alt)]), f"{what}:{seg}->{alt}"


def import_reorder(rng, src):
    lines = src.split("\n")
    tree = ast.parse(src)
    body = [n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom)) and n.lineno == n.end_lineno
            and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")]
    pairs = [(a, b) for a, b in zip(body, body[1:]) if b.lineno == a.lineno + 1]
    if not pairs:
        return None
    a, b = rng.choice(pairs)
    lines[a.lineno - 1], lines[b.lineno - 1] = lines[b.lineno - 1], lines[a.lineno - 1]
    return "\n".join(lines), f"swap L{a.lineno}"


def reformat(rng, src):
    tree = ast.parse(src)
    lines = src.split("\n")
    ops = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Return) and n.value is not None and n.lineno == n.end_lineno and not isinstance(n.value, ast.Tuple):
            ops.append(("paren", n.value))
        if isinstance(n, ast.Call) and n.args and n.lineno == n.end_lineno and not n.keywords and not any(isinstance(a, ast.Starred) for a in n.args):
            ops.append(("wrap", n))
        if isinstance(n, (ast.List, ast.Tuple)) and len(n.elts) >= 2 and n.lineno == n.end_lineno:
            seg = ast.get_source_segment(src, n)
            if seg and seg[0] in "([" and not seg[-2:-1] == ",":
                ops.append(("comma", n))
        if isinstance(n, (ast.FunctionDef,)) and n.body and n.body[-1].end_lineno:
            ops.append(("pass", n))
    rng.shuffle(ops)
    for kind, n in ops:
        if kind == "paren":
            seg = ast.get_source_segment(src, n)
            return replace_spans(src, [(n.lineno, n.col_offset, n.end_col_offset, f"({seg})")]), "paren"
        if kind == "comma":
            seg = ast.get_source_segment(src, n)
            return replace_spans(src, [(n.lineno, n.col_offset, n.end_col_offset, seg[:-1] + "," + seg[-1])]), "comma"
        if kind == "pass":
            last = n.body[-1]
            ind = len(lines[last.lineno - 1]) - len(lines[last.lineno - 1].lstrip())
            if lines[last.end_lineno - 1].rstrip().endswith((":", "\\")):
                continue
            lines.insert(last.end_lineno, " " * ind + "pass")
            return "\n".join(lines), "pass"
        if kind == "wrap":
            line = lines[n.lineno - 1]
            ind = len(line) - len(line.lstrip())
            func = ast.get_source_segment(src, n.func)
            args = [ast.get_source_segment(src, a) for a in n.args]
            if None in args or func is None or n.end_col_offset > len(line):
                continue
            before, after = line[:n.col_offset], line[n.end_col_offset:]
            body = [before + func + "("] + [" " * (ind + 4) + a + "," for a in args] + [" " * ind + ")" + after]
            lines[n.lineno - 1:n.lineno] = body
            out = "\n".join(lines)
            try:
                ast.parse(out)
            except SyntaxError:
                lines = src.split("\n")
                continue
            return out, "wrap"
    return None


def quotes(rng, src):
    toks = [t for t in tokenize.generate_tokens(io.StringIO(src).readline) if t.type == tokenize.STRING
            and re.fullmatch(r"'[A-Za-z_ ]*'", t.string) and t.start[0] == t.end[0]]
    if not toks:
        return None
    k = rng.sample(toks, min(len(toks), rng.randint(1, 4)))
    return replace_spans(src, [(t.start[0], t.start[1], t.end[1], '"' + t.string[1:-1] + '"') for t in k]), f"quotes x{len(k)}"


def no_op(rng, fn, kind, pool, n, start):
    made = 0
    tries = 0
    while made < n and tries < n * 30:
        tries += 1
        rel = rng.choice(pool)
        src = rd(rel)
        if not ascii_ok(src):
            continue
        try:
            r = fn(rng, src)
        except (SyntaxError, ValueError, tokenize.TokenError):
            r = None
        if not r or r[0] == src:
            continue
        try:
            ast.parse(r[0])
        except SyntaxError:
            continue
        wr(rel, r[0])
        if emit(f"m06{start + made:04d}", kind, f"{rel} {r[1]}"):
            made += 1
    return made


# ---------------------------------------------------------------- file-level shapes
NEW_PY = {
    "sqlglot/helpers_extra.py": "from __future__ import annotations\n\nimport typing as t\n\n\ndef chunks(items: t.Sequence[t.Any], size: int) -> t.Iterator[t.Sequence[t.Any]]:\n    for i in range(0, len(items), size):\n        yield items[i : i + size]\n",
    "sqlglot/optimizer/fold_noop.py": "from __future__ import annotations\n\nfrom sqlglot import exp\n\n\ndef fold_noop(expression: exp.Expr) -> exp.Expr:\n    \"\"\"Placeholder rule that returns the expression unchanged.\"\"\"\n    return expression\n",
    "sqlglot/dialects/newdb.py": "from __future__ import annotations\n\nfrom sqlglot.dialects.dialect import Dialect\n\n\nclass NewDB(Dialect):\n    pass\n",
    "sqlglot/typing/newdb.py": "from __future__ import annotations\n\nfrom sqlglot.typing import EXPRESSION_METADATA as BASE\n\nEXPRESSION_METADATA = {**BASE}\n",
    "sqlglot/executor/util.py": "def first(rows):\n    return next(iter(rows), None)\n",
    "sqlglot/parsers/newdb.py": "from __future__ import annotations\n\nfrom sqlglot import parser\n\n\nclass NewDBParser(parser.Parser):\n    pass\n",
    "sqlglot/_compat.py": "import sys\n\nPY310 = sys.version_info >= (3, 10)\n",
    "sqlglot/generators/newdb.py": "from __future__ import annotations\n\nfrom sqlglot import generator\n\n\nclass NewDBGenerator(generator.Generator):\n    pass\n",
    "sqlglot/optimizer/broken_rule.py": "from sqlglot import exp\nfrom sqlglot.optimizer.does_not_exist import thing\n\n\ndef rule(e):\n    return thing(e)\n",
    "sqlglot/typing/brokendb.py": "from sqlglot.typing import EXPRESSION_METADATA as BASE\nfrom sqlglot.typing.nope import X\n\nEXPRESSION_METADATA = {**BASE, **X}\n",
}
NEW_TEST = {
    "tests/test_chunks.py": "import unittest\n\n\nclass TestChunks(unittest.TestCase):\n    def test_one(self):\n        self.assertEqual(list(range(3)), [0, 1, 2])\n",
    "tests/dialects/test_newdb.py": "from tests.dialects.test_dialect import Validator\n\n\nclass TestNewDB(Validator):\n    dialect = \"duckdb\"\n\n    def test_select(self):\n        self.validate_identity(\"SELECT 1\")\n",
    "tests/conftest.py": "import pytest\n\n\n@pytest.fixture\ndef sql_text():\n    return \"SELECT 1\"\n",
    "tests/test_broken_import.py": "import unittest\n\nfrom sqlglot.nope import nothing\n\n\nclass TestBroken(unittest.TestCase):\n    def test_x(self):\n        nothing()\n",
    "tests/optimizer_helpers.py": "def normalize(sql):\n    return sql.strip().upper()\n",
}
NEW_OTHER = {
    "docs/dialects.md": "# Dialects\n\nNotes on dialect support.\n",
    "tests/fixtures/optimizer/new_rule.sql": "SELECT 1 AS a;\nSELECT 1 AS a;\n",
    ".github/workflows/lint.yml": "name: lint\non: [push]\njobs:\n  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4\n",
    "sqlglot/data/keywords.json": "{\"keywords\": []}\n",
    "benchmarks/tokenize.py": "import sqlglot\n\nprint(len(sqlglot.tokenize('SELECT 1')))\n",
    "SECURITY.md": "# Security\n\nReport issues privately.\n",
    "sqlglot/README.txt": "Package notes.\n",
    "tests/fixtures/notes.txt": "fixtures notes\n",
}


def mutate_text(rng, text):
    lines = text.split("\n")
    idx = [i for i, l in enumerate(lines) if l.strip()]
    i = rng.choice(idx)
    lines.insert(i + 1, lines[i])
    return "\n".join(lines)


def file_level(rng):
    n = 0

    def nid():
        nonlocal n
        n += 1
        return f"m06{n:04d}"

    # new files
    for rel, body in {**NEW_PY, **NEW_TEST, **NEW_OTHER}.items():
        wr(rel, body)
        emit(nid(), "new_file", rel)
    # new file that a module now imports (new + edit)
    wr("sqlglot/helpers_extra.py", NEW_PY["sqlglot/helpers_extra.py"])
    s = rd("sqlglot/serde.py")
    wr("sqlglot/serde.py", s.replace("import typing as t\n", "import typing as t\n\nfrom sqlglot.helpers_extra import chunks  # noqa: F401\n", 1))
    emit(nid(), "new_file+edit", "helpers_extra imported by serde")

    # deleted files
    dels = ["sqlglot/dialects/athena.py", "sqlglot/optimizer/eliminate_ctes.py", "sqlglot/serde.py",
            "sqlglot/anonymize.py", "sqlglot/lineage.py", "sqlglot/typing/snowflake.py", "sqlglot/diff.py",
            "sqlglot/executor/table.py", "sqlglot/planner.py", "sqlglot/py.typed",
            "tests/test_time.py", "tests/dialects/test_athena.py", "tests/helpers.py", "tests/gen_fixtures.py",
            "tests/fixtures/identity.sql", "tests/fixtures/jsonpath/LICENSE", "tests/leakcheck.py",
            "benchmarks/parse.py", "posts/" , "CHANGELOG.md", ".gitpod.yml", "pdoc/cli.py", "sqlglot.png",
            "sqlglotc/MANIFEST.in", "AGENTS.md"]
    for rel in dels:
        target = p(rel)
        if rel.endswith("/"):
            files = sorted(f for f in target.rglob("*") if f.is_file())
            if not files:
                continue
            files[0].unlink()
            emit(nid(), "delete_file", str(files[0].relative_to(REPO)))
            continue
        if not target.exists():
            continue
        target.unlink()
        emit(nid(), "delete_file", rel)

    # renames (pure, and with an edit)
    renames = [("sqlglot/anonymize.py", "sqlglot/anon.py", False), ("sqlglot/serde.py", "sqlglot/serialize.py", False),
               ("sqlglot/dialects/athena.py", "sqlglot/dialects/athena_dialect.py", False),
               ("sqlglot/optimizer/eliminate_ctes.py", "sqlglot/optimizer/remove_ctes.py", True),
               ("tests/test_time.py", "tests/test_time_format.py", False),
               ("tests/test_serde.py", "tests/test_serialize.py", True),
               ("tests/fixtures/partial.sql", "tests/fixtures/partial_queries.sql", False),
               ("tests/fixtures/identity.sql", "tests/fixtures/identities.sql", False),
               ("benchmarks/compare.py", "benchmarks/compare_dialects.py", False),
               ("CONTRIBUTING.md", "docs/CONTRIBUTING.md", False),
               ("pdoc/cli.py", "pdoc/build_docs.py", True),
               ("tests/helpers.py", "tests/test_helpers_util.py", False)]
    for a, b, edit in renames:
        if not p(a).exists():
            continue
        p(b).parent.mkdir(parents=True, exist_ok=True)
        p(a).rename(p(b))
        if edit:
            wr(b, rd(b).replace("\n\n", "\n\n# Moved from " + a + "\n\n", 1) if b.endswith(".py") else mutate_text(rng, rd(b)))
        emit(nid(), "rename" + ("+edit" if edit else ""), f"{a} -> {b}")
    # rename that also fixes the importers (a real refactor)
    importers = [f for f in pymods() + [str(x.relative_to(REPO)) for x in REPO.glob("tests/**/*.py")]
                 if "sqlglot.anonymize" in rd(f) or "from sqlglot import anonymize" in rd(f)]
    p("sqlglot/anonymize.py").rename(p("sqlglot/anon.py"))
    for f in importers:
        wr(f, rd(f).replace("sqlglot.anonymize", "sqlglot.anon").replace("from sqlglot import anonymize", "from sqlglot import anon as anonymize"))
    emit(nid(), "rename+importers", f"anonymize -> anon ({len(importers)} importers)")
    # copies (only shown as copies with -C -C)
    for a, b in [("sqlglot/dialects/athena.py", "sqlglot/dialects/athena2.py"), ("tests/fixtures/partial.sql", "tests/fixtures/partial2.sql"),
                 ("tests/test_time.py", "tests/test_time2.py")]:
        shutil.copy(p(a), p(b))
        emit(nid(), "copy", f"{a} -> {b}", diff_args=("-M", "-C", "-C"))
    # mode only
    for rel in ["sqlglot/serde.py", "tests/test_time.py", "setup.py", "tests/fixtures/identity.sql", "benchmarks/parse.py"]:
        p(rel).chmod(0o755)
        emit(nid(), "mode", rel)
    # mode + edit
    p("sqlglot/__main__.py").chmod(0o755)
    wr("sqlglot/__main__.py", "#!/usr/bin/env python3\n" + rd("sqlglot/__main__.py"))
    emit(nid(), "mode+edit", "sqlglot/__main__.py shebang")
    # binary
    png = bytes(range(256)) * 4
    for rel, binflag in [("docs/logo.png", False), ("docs/logo2.png", True), ("sqlglot.png", False), ("sqlglot.png", True),
                         ("tests/fixtures/blob.bin", False)]:
        p(rel).parent.mkdir(parents=True, exist_ok=True)
        p(rel).write_bytes(png[: rng.randint(300, 1000)] if not p(rel).exists() else p(rel).read_bytes()[:-17] + b"\x00\x01")
        emit(nid(), "binary", rel + (" --binary" if binflag else ""), diff_args=("-M", "--binary") if binflag else ("-M",))
    # quoted paths
    for rel, body in [("docs/release notes.md", "# Notes\n"), ("tests/fixtures/café.sql", "SELECT 1;\n"),
                      ("benchmarks/old bench.py", "print(1)\n")]:
        wr(rel, body)
        emit(nid(), "quoted_new", rel)
    p("CONTRIBUTING.md").rename(p("CONTRIBUTING guide.md"))
    emit(nid(), "quoted_rename", "CONTRIBUTING.md -> CONTRIBUTING guide.md")

    # config and unknown non-Python files
    cfg = [
        ("pyproject.toml", lambda s: s.replace("line-length = 100", "line-length = 110")),
        ("pyproject.toml", lambda s: s.replace('    "Programming Language :: SQL",\n', '    "Programming Language :: SQL",\n    "Topic :: Database",\n')),
        ("pyproject.toml", lambda s: s.replace('fallback_version = "0.0.0"', 'fallback_version = "0.0.1"')),
        ("pyproject.toml", lambda s: s + "\n[tool.pytest.ini_options]\naddopts = \"-q\"\n"),
        ("pyproject.toml", lambda s: s.replace('ignore = ["E721", "E741"]', 'ignore = ["E721", "E741", "E501"]')),
        ("setup.cfg", lambda s: s.replace("no_implicit_optional = True", "no_implicit_optional = True\nwarn_unused_ignores = True")),
        ("setup.py", lambda s: s.replace('"pyperf",', '"pyperf",\n            "pytest",')),
        ("setup.py", lambda s: s.replace('"duckdb>=0.6"', '"duckdb>=1.0"')),
        ("MANIFEST.in", lambda s: s + "include LICENSE\n"),
        ("Makefile", lambda s: s + "\nclean:\n\trm -rf build dist\n"),
        (".pre-commit-config.yaml", lambda s: s.replace("ruff", "ruff", 1) + "# keep hooks pinned\n"),
        (".gitignore", lambda s: s + "\n.venv/\n"),
        (".gitpod.yml", lambda s: s + "\n# workspace\n"),
        ("sqlglotc/setup.py", lambda s: s + "\n# Built with mypyc.\n"),
        ("sqlglotc/setup.py", lambda s: re.sub(r"\n(\s*)(\w+) = ", lambda m: f"\n{m.group(1)}{m.group(2)}  = ", s, count=1)),
        ("sqlglotc/pyproject.toml", lambda s: s + "\n# sqlglotc build\n"),
        ("README.md", lambda s: s.replace("\n\n", "\n\nSQLGlot is fast.\n\n", 1)),
        ("CHANGELOG.md", lambda s: "## Unreleased\n\n- Nothing yet.\n\n" + s),
        ("AGENTS.md", lambda s: s + "\nKeep tests fast.\n"),
        ("tests/__init__.py", lambda s: s + "\n# test package\n"),
        ("tests/sqlglot/__init__.py", lambda s: s + "\n# integration tests\n"),
        ("pdoc/cli.py", lambda s: s + "\n# docs entry point\n"),
        ("LICENSE", lambda s: s.replace("Copyright", "Copyright (c)", 1)),
        ("benchmarks/__init__.py", lambda s: s + "# benchmarks\n"),
        (".github/workflows/" + sorted(x.name for x in (REPO / ".github/workflows").glob("*.y*ml"))[0], lambda s: s + "\n# ci\n"),
        ("sqlglot/py.typed", lambda s: s + "partial\n"),
        ("tests/fixtures/jsonpath/LICENSE", lambda s: s + "\n"),
        ("sqlglot/dialects/__init__.py", lambda s: s.replace("import threading\n", "import threading  # noqa\n", 1)),
    ]
    for rel, fn in cfg:
        if not p(rel).exists():
            continue
        s = rd(rel)
        t = fn(s)
        if t == s:
            continue
        wr(rel, t)
        emit(nid(), "config", rel)

    # no newline at EOF
    for rel in ["sqlglot/serde.py", "tests/fixtures/partial.sql", "sqlglot/diff.py", "README.md"]:
        wr(rel, rd(rel).rstrip("\n"))
        emit(nid(), "noeol", rel)
    s = rd("sqlglot/time.py").rstrip("\n")
    wr("sqlglot/time.py", s + "\n\n\ndef _unused() -> None:\n    return None")
    emit(nid(), "noeol+code", "sqlglot/time.py")
    # CRLF conversion of a whole file
    for rel in ["sqlglot/serde.py", "docs_dummy", "tests/fixtures/partial.sql", "sqlglot/trie.py"]:
        if not p(rel).exists():
            continue
        p(rel).write_bytes(p(rel).read_bytes().replace(b"\n", b"\r\n"))
        emit(nid(), "crlf_file", rel)
    return n


def main():
    global SRC, REPO, OUT
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("repo")
    ap.add_argument("out")
    ap.add_argument("--seed", type=int, default=6)
    a = ap.parse_args()
    SRC, REPO, OUT = Path(a.src), Path(a.repo), Path(a.out)
    rng = random.Random(a.seed)
    shutil.rmtree(OUT / "commits", ignore_errors=True)
    (OUT / "commits").mkdir(parents=True)
    setup_repo()
    reset()
    n = file_level(rng)
    pool = [m for m in pymods() if p(m).stat().st_size > 2000]
    fut = [m for m in pool if "from __future__ import annotations" in rd(m)]
    nofut = [m for m in pool if m not in fut]
    start = 1000
    for fn, kind, pl, cnt in [(rename_local, "rename_local", pool, 16), (annotation_only, "annot", fut, 14),
                              (lambda r, s: annotation_only(r, s, False), "annot_nofuture", nofut, 4),
                              (import_reorder, "import_reorder", pool, 10), (reformat, "reformat", pool, 16),
                              (quotes, "quotes", pool, 6)]:
        made = no_op(rng, fn, kind, pl, cnt, start)
        start += 100
        print(kind, made)
    # semantic no-op inside a test module
    tpool = sorted(str(f.relative_to(REPO)) for f in REPO.glob("tests/**/test_*.py"))
    no_op(rng, rename_local, "rename_local_test", tpool, 4, start)
    no_op(rng, reformat, "reformat_test", tpool, 4, start + 100)
    (OUT / "kinds.json").write_text(json.dumps(KINDS, indent=0))
    print(len(KINDS), "commits")


if __name__ == "__main__":
    main()
