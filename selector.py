"""Blast Radius selector.

For each patch: apply it to the pristine sqlglot source, structurally diff the AST, and map each
changed region to the tests that executed it (per-test coverage map built from the pinned commit).
Anything not provably safe falls back to running every test for that commit.

Stdout: exactly one JSON line per commit. Everything else goes to stderr.
"""
from __future__ import annotations

import ast
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from blast import astdiff  # noqa: E402
from blast import patch as P  # noqa: E402
from blast import maxnodes  # noqa: E402
from blast import plans  # noqa: E402
from blast import runner  # noqa: E402
from blast.maps import Maps  # noqa: E402
from blast.semantic import NeedAll, Semantic  # noqa: E402
from blast.split import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAPS_DIR = Path(os.environ.get("BR_MAPS", str(HERE / "maps")))
FP_DIR = Path(os.environ.get("BR_FP", str(HERE / "fp")))
try:
    astdiff.ENUM_CLASSES = set(json.loads((FP_DIR / "enums.json").read_text()))
except (OSError, ValueError):
    pass

# module name -> test numbers that load it. Built from the pinned tree (imports,
# fixture dialect headers, coverage of loader lines). Absent means "not a leaf
# dialect module": an unparsable file still runs every test.
DIALECT_TESTS: dict[str, set[int]] = {}


def load_dialect_tests(id_to_number: dict) -> None:
    path = MAPS_DIR / "dialect_load.json"
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return
    nums = [id_to_number.get(i, -1) for i in raw.get("ids", ())]
    for mod, idxs in raw.get("mods", {}).items():
        got = {nums[i] for i in idxs if 0 <= i < len(nums) and nums[i] >= 0}
        if got:
            DIALECT_TESTS[mod] = got


def _module_name(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _importers_of(path: str, maps):
    """Tests that load this module, or None when that set is not known.

    Leaf dialect modules keep the load map, which is tighter than the static
    import closure. Everything else uses the closure. Empty means unknown.
    """
    if not path.startswith("sqlglot/"):
        return None
    dial = DIALECT_TESTS.get(_module_name(path))
    if dial:
        return set(dial)
    try:
        from blast.importers import tests_importing
        return tests_importing(path, SRC, maps, DIALECT_TESTS)
    except Exception as e:
        log("importers skip:", repr(e))
        return None


_MAX_NODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"
_MAX_NODES_LOADS = None
_MAX_NODES_INDEX = None
# Set for the commit currently being selected. An import-breakage answer must
# not build the test-source mention index: that index is several seconds of
# CPU, and intersecting it can drop a test that fails at import time.
_IMPORTER_HIT = False


def _max_nodes_loads():
    """Modules test_max_nodes imported when that test was run on the pinned tree."""
    global _MAX_NODES_LOADS
    if _MAX_NODES_LOADS is None:
        path = MAPS_DIR / "max_nodes_load.json"
        try:
            _MAX_NODES_LOADS = set(json.loads(path.read_text()).get("modules") or [])
        except (OSError, ValueError):
            _MAX_NODES_LOADS = set()
    return _MAX_NODES_LOADS


def _max_nodes_number(maps):
    """Scorer test number for test_max_nodes, or None when this suite lacks it."""
    global _MAX_NODES_INDEX
    if maps is None:
        return None
    if _MAX_NODES_INDEX is None:
        try:
            ids = json.loads((Path(maps.root) / "index.json").read_text())["tests"]
            _MAX_NODES_INDEX = ids.index(_MAX_NODES_ID)
        except (OSError, ValueError, KeyError, AttributeError):
            _MAX_NODES_INDEX = -1
    if _MAX_NODES_INDEX < 0 or _MAX_NODES_INDEX >= len(maps.num):
        return None
    n = maps.num[_MAX_NODES_INDEX]
    return n if isinstance(n, int) and n >= 0 else None


def _dialect_load_union(files):
    """Tests that load each patched module, when every module is in the dialect trace.

    The trace is maps/dialect_load.json: one test set per leaf dialect, parser, and
    generator module, built so a collection-time import is not mistaken for a test
    that never loads the module. None means the patch is not that shape.
    """
    if not files or not DIALECT_TESTS:
        return None
    acc: set = set()
    for fp in files:
        path = fp.path
        if fp.special or fp.old_path != fp.new_path or not path.endswith(".py"):
            return None
        if not path.startswith("sqlglot/"):
            return None
        dial = DIALECT_TESTS.get(_module_name(path))
        if not dial:
            return None
        acc |= set(dial)
    return acc or None


def _without_unloaded_max_nodes(files, reason, maps, nums: set) -> set:
    """Drop test_max_nodes from a full-suite fallback when the test cannot load the edit.

    Only module-breakage fallbacks qualify. A patch that also edits a module this
    test loads can make the test import the broken one, so that file keeps the test.
    """
    loads = _max_nodes_loads()
    if not loads or not files:
        return nums
    msg = str(reason)
    if not (
        msg.startswith("unparsable:")
        or msg.startswith("module-level structural")
        or msg.startswith("semantic:")
    ):
        return nums
    for fp in files:
        path = fp.path
        if (
            fp.special
            or fp.old_path != fp.new_path
            or not path.endswith(".py")
            or not path.startswith("sqlglot/")
        ):
            return nums
        if _module_name(path) in loads:
            return nums
    number = _max_nodes_number(maps)
    if number is None:
        return nums
    return set(nums) - {number}


CONFIG_BASENAMES = {
    "pyproject.toml", "setup.cfg", "setup.py", "conftest.py", "pytest.ini", "tox.ini", "MANIFEST.in",
    "Makefile", "requirements.txt", "requirements-dev.txt", "__init__.py",
}
INERT_PREFIXES = ("benchmarks/", "posts/", "docs/", ".github/")


def log(*a):
    print(*a, file=sys.stderr)


class All(Exception):
    """Raised when the conservative answer (run everything) is required."""


import re as _re  # noqa: E402

_SIDE_EFFECTS = _re.compile(r"write_bytes|write_text|\.unlink\(|rmtree|os\.environ|\.mkdir\(|subprocess|chdir|os\.remove|tempfile")
# Calls that mutate the tree or another process even when the regex misses the callee body.
_LEAK_CALLS = frozenset({"run_path", "copy_extensions_to_source", "Popen", "rmtree"})
_TEST_IMPORTERS = {}
_LEAK_SPANS: dict[str, list | None] = {}


def _leak_spans(text: str):
    """Functions in a test module that can leak state into other tests.

    Each entry is (lineno, end_lineno, leaks, qualname). None means the file
    did not parse: treat every region as a leak.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    funcs = []

    class Visitor(ast.NodeVisitor):
        def __init__(self):
            self.cls = None

        def visit_ClassDef(self, node):
            prev = self.cls
            self.cls = node.name
            self.generic_visit(node)
            self.cls = prev

        def visit_FunctionDef(self, node):
            self._fn(node)

        def visit_AsyncFunctionDef(self, node):
            self._fn(node)

        def _fn(self, node):
            calls = set()
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                f = sub.func
                if isinstance(f, ast.Name):
                    calls.add(f.id)
                elif isinstance(f, ast.Attribute):
                    calls.add(f.attr)
            seg = ast.get_source_segment(text, node) or ""
            direct = bool(_SIDE_EFFECTS.search(seg) or (calls & _LEAK_CALLS))
            funcs.append([self.cls, node.name, node.lineno, node.end_lineno or node.lineno, calls, direct])
            for stmt in node.body:
                if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    self.visit(stmt)

    Visitor().visit(tree)
    leak_names = {n for c, n, a, b, calls, direct in funcs if direct}
    changed = True
    while changed:
        changed = False
        for c, n, a, b, calls, direct in funcs:
            if n in leak_names:
                continue
            if calls & leak_names:
                leak_names.add(n)
                changed = True
    return [(a, b, n in leak_names, (c, n)) for c, n, a, b, calls, direct in funcs]


def _region_leaks(path: str, old: str, new: str, start: int, end: int) -> bool:
    """True when a change in a side-effecting test module can break other files.

    A region that sits entirely inside a function with no leak of its own (and
    that does not call one) only affects tests that execute that function.
    Module-level edits, unparsable files, and functions that write, spawn, or
    call such a function still run the whole suite.
    """
    try:
        if path not in _LEAK_SPANS:
            _LEAK_SPANS[path] = _leak_spans(old)
        spans = _LEAK_SPANS[path]
        if spans is None or start <= 0:
            return True
        hits = [s for s in spans if s[0] <= start and s[1] >= end]
        if not hits:
            return True
        lineno, end_lineno, leaks, qual = min(hits, key=lambda s: s[1] - s[0])
        if leaks:
            return True
        new_spans = _leak_spans(new)
        if new_spans is None:
            return True
        same = [s for s in new_spans if s[3] == qual]
        if not same or any(s[2] for s in same):
            return True
        return False
    except Exception:
        return True


def tests_module_tests(maps, path: str) -> set:
    """Tests affected by an import-time change in a tests/ module: its own tests, plus (for helper modules)
    every test in any tests/ file that imports it, transitively."""
    own = maps.file_tests(path)
    if own and path.rsplit("/", 1)[-1].startswith("test_"):
        return own
    import re
    mod = path[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[:-9]
    pkg, _, leaf = mod.rpartition(".")
    if not _TEST_IMPORTERS:
        for f in (SRC / "tests").rglob("*.py"):
            _TEST_IMPORTERS[str(f.relative_to(SRC))] = f.read_text(errors="replace")
    out, todo, seen = set(own), [(mod, pkg, leaf)], set()
    while todo:
        m, pk, lf = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        pat = re.compile(rf"(^|\n)\s*(from\s+{re.escape(m)}\s+import|import\s+{re.escape(m)}\b|"
                         rf"from\s+{re.escape(pk)}\s+import\s+[^\n]*\b{re.escape(lf)}\b|"
                         rf"from\s+\.+{re.escape(lf)}\s+import)")
        for rel, txt in _TEST_IMPORTERS.items():
            if rel != path and pat.search(txt):
                out |= maps.file_tests(rel)
                rm = rel[:-3].replace("/", ".")
                if rm.endswith(".__init__"):
                    rm = rm[:-9]
                p2, _, l2 = rm.rpartition(".")
                todo.append((rm, p2, l2))
    return out


def select_file(fp, maps, all_tests: set, semantic_needed: dict, version_norm: bool = True) -> set:
    path = fp.path
    if fp.special or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
        raise All(f"special/new/deleted/renamed: {path}")
    base = path.rsplit("/", 1)[-1]
    if not path.endswith(".py"):
        opened = maps.opened_by(path) if maps else None
        if opened is not None:
            return opened
        if base in CONFIG_BASENAMES or path.startswith(("sqlglot", "tests/sqlglot")):
            raise All(f"config/unknown data file: {path}")
        if path.startswith("tests/fixtures/") or path.startswith(INERT_PREFIXES) or "/" not in path:
            return set()  # never opened by any test
        raise All(f"unknown file: {path}")

    if base in CONFIG_BASENAMES and base != "__init__.py":
        raise All(f"config py: {path}")
    is_tests = path.startswith("tests/")
    is_pkg = path.startswith("sqlglot/")
    if not (is_tests or is_pkg):
        if path.startswith(INERT_PREFIXES):
            return set()
        raise All(f"py outside sqlglot/tests: {path}")
    if maps is None:
        raise All("no maps")
    if is_pkg and path not in maps.pyfiles:
        raise All(f"unmapped module: {path}")

    old = (SRC / path).read_text()
    new = P.apply(old, fp)
    regions = astdiff.diff(old, new, version_norm=version_norm)
    if regions is None:
        # A syntax error fails the tests that import the module, not every test.
        loaded = _importers_of(path, maps)
        if loaded:
            global _IMPORTER_HIT
            _IMPORTER_HIT = True
            return loaded
        raise All(f"unparsable: {path}")
    out: set = set()
    for r in regions:
        if r.kind == "doc":
            # tests/test_docs.py is not in the scored suite. No scored test reads __doc__.
            if r.doctest:
                out |= {n for f in ("tests/test_docs.py",) for n in maps.file_tests(f)}
            continue
        if r.start == 0:
            # The differ could not place a module-level edit. Tests that never
            # import the module cannot run it. An unknown module still runs all.
            loaded = _importers_of(path, maps)
            if loaded:
                _IMPORTER_HIT = True
                return loaded
            raise All(f"module-level structural change: {path}")
        tests, imp, hit = maps.region_tests(path, r.start, r.end)
        if is_tests and _SIDE_EFFECTS.search(old) and _region_leaks(path, old, new, r.start, r.end):
            # a function that writes, spawns, or calls one can break unrelated tests.
            # a change that stays inside a function with no such leak only affects
            # the tests that execute it (the coverage set below).
            raise All(f"side-effecting test module changed: {path}")
        if r.kind == "rt":
            out |= tests
            if imp and not r.lam:  # function body that also runs at import time
                if is_tests:
                    out |= tests_module_tests(maps, path)
                else:
                    semantic_needed[path] = new
        else:  # import-time code
            if not hit:
                continue  # never executed: cannot affect any test
            if is_tests:
                out |= tests_module_tests(maps, path)
            else:
                semantic_needed[path] = new
    return out


_PLAIN_KEY = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MENTION_INDEX = None
_FIXTURE_TEXTS = None


def _keyword_dicts(tree):
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            target = value = None
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                target, value = stmt.target.id, stmt.value
            elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                target, value = stmt.targets[0].id, stmt.value
            if target == "KEYWORDS" and isinstance(value, ast.Dict):
                found.append(value)
    return found


def _hunk_lines(fp):
    old_lines, new_lines = [], []
    for h in fp.hunks:
        o, n = h.old_start, h.new_start
        for line in h.lines:
            if line.startswith("-"):
                old_lines.append(o)
                o += 1
            elif line.startswith("+"):
                new_lines.append(n)
                n += 1
            else:
                o += 1
                n += 1
    return old_lines, new_lines


def _key_at(dicts, line: int):
    """Plain KEYWORDS key whose entry covers this line, else None."""
    for d in dicts:
        if not (d.lineno <= line <= (d.end_lineno or d.lineno)):
            continue
        for k, v in zip(d.keys, d.values):
            if k is None:
                if v and v.lineno <= line <= (v.end_lineno or v.lineno):
                    return None
                continue
            vend = v.end_lineno or v.lineno if v else k.lineno
            end = max(k.end_lineno or k.lineno, vend)
            if k.lineno <= line <= end:
                if isinstance(k, ast.Constant) and isinstance(k.value, str) and _PLAIN_KEY.match(k.value):
                    return k.value
                return None
        return None
    return None


def _plain_keyword_words(files) -> set[str] | None:
    """Words whose KEYWORDS entry is the only thing this patch edits.

    None means leave the selection alone: a space or symbol key changes the
    tokenizer trie, a line outside KEYWORDS can fail tests that never spell
    the word, and an unparsable file belongs to the syntax path.
    """
    words: set[str] = set()
    saw = False
    for fp in files:
        if fp.special or fp.old_path != fp.new_path or not fp.path.endswith(".py"):
            return None
        if not fp.path.startswith("sqlglot/"):
            return None
        old = (SRC / fp.path).read_text()
        new = P.apply(old, fp)
        # The dict's name is an identifier. A file that never spells it has no
        # KEYWORDS entry, so the parse would only reject the patch.
        if "KEYWORDS" not in old and "KEYWORDS" not in new:
            return None
        try:
            old_ds = _keyword_dicts(ast.parse(old))
            new_ds = _keyword_dicts(ast.parse(new))
        except (SyntaxError, ValueError, RecursionError):
            return None
        old_lines, new_lines = _hunk_lines(fp)
        if not old_lines and not new_lines:
            return None
        for ln in old_lines:
            key = _key_at(old_ds, ln)
            if key is None:
                return None
            words.add(key)
            saw = True
        for ln in new_lines:
            key = _key_at(new_ds, ln)
            if key is None:
                return None
            words.add(key)
            saw = True
    return words if saw else None


def _fixture_texts():
    """Fixture text from the pinned tree. Shared so a whole-file scan does not parse tests."""
    global _FIXTURE_TEXTS
    if _FIXTURE_TEXTS is not None:
        return _FIXTURE_TEXTS
    fixtures = []
    fixroot = SRC / "tests" / "fixtures"
    if fixroot.is_dir():
        for p in fixroot.rglob("*"):
            if not p.is_file() or p.stat().st_size >= 2_000_000:
                continue
            try:
                fixtures.append((str(p.relative_to(SRC)), p.read_text(errors="replace")))
            except OSError:
                pass
    _FIXTURE_TEXTS = fixtures
    return _FIXTURE_TEXTS


def _mention_index(maps):
    """Per-test source slice and fixture text. Built once, from the pinned tree."""
    global _MENTION_INDEX
    if _MENTION_INDEX is not None:
        return _MENTION_INDEX
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_file: dict[str, list] = {}
    for t in tests:
        by_file.setdefault(t["id"].split("::")[0], []).append(t)
    segs: dict[str, list] = {}
    for rel, items in by_file.items():
        path = SRC / rel
        try:
            text = path.read_text(errors="replace")
            tree = ast.parse(text)
        except (OSError, SyntaxError, ValueError, RecursionError):
            segs[rel] = [(t["number"], "") for t in items]
            continue
        classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
        rows = []
        for t in items:
            parts = t["id"].split("::")
            seg = ""
            if len(parts) == 3 and parts[1] in classes:
                node = classes[parts[1]]
                cls_src = ast.get_source_segment(text, node) or ""
                fn_src = ""
                for stmt in node.body:
                    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == parts[2]:
                        fn_src = ast.get_source_segment(text, stmt) or ""
                        break
                # Same slice the offline check used: the test, plus the class head.
                seg = fn_src + "\n" + cls_src[:2000]
            rows.append((t["number"], seg))
        segs[rel] = rows
    _MENTION_INDEX = (segs, _fixture_texts())
    return _MENTION_INDEX


def _tests_mentioning(words: set[str], maps) -> set[int]:
    segs, fixtures = _mention_index(maps)
    pats = [_re.compile(rf"\b{_re.escape(w)}\b", _re.I) for w in words]
    hit: set[int] = set()
    for rows in segs.values():
        for num, seg in rows:
            if seg and any(p.search(seg) for p in pats):
                hit.add(num)
    for rel, text in fixtures:
        if not any(p.search(text) for p in pats):
            continue
        opened = maps.opened_by(rel)
        if opened:
            hit |= set(opened)
    return hit


_FULL_MENTIONS = None
_SQL_FUNC = _re.compile(r"^[A-Z][A-Z0-9_]*$")
_IDENT_WORD = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_QUOTED = _re.compile(r"""(\"[^\"\\]*\"|'[^'\\]*')""")


def _full_mention_index(maps):
    """Whole test file plus fixture text. The slice index misses tests that spell the word later in the class."""
    global _FULL_MENTIONS
    if _FULL_MENTIONS is not None:
        return _FULL_MENTIONS
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    by_file: dict[str, list] = {}
    for t in tests:
        by_file.setdefault(t["id"].split("::")[0], []).append(t["number"])
    texts = {}
    for rel, nums in by_file.items():
        try:
            texts[rel] = (SRC / rel).read_text(errors="replace")
        except OSError:
            texts[rel] = ""
    # Whole-file text is enough. Parsing every test to build slices is several
    # seconds, and this caller never reads the slices.
    _FULL_MENTIONS = (by_file, texts, _fixture_texts())
    return _FULL_MENTIONS


def _tests_mentioning_full(words: set[str], maps) -> set[int]:
    by_file, texts, fixtures = _full_mention_index(maps)
    pats = [_re.compile(rf"\b{_re.escape(w)}\b", _re.I) for w in words]
    hit: set[int] = set()
    for rel, text in texts.items():
        if text and any(p.search(text) for p in pats):
            hit.update(by_file[rel])
    for rel, text in fixtures:
        if not any(p.search(text) for p in pats):
            continue
        opened = maps.opened_by(rel)
        if opened:
            hit |= set(opened)
    return hit


def _assign_name(node, par: dict):
    seen = 0
    while id(node) in par and seen < 8:
        node = par[id(node)]
        seen += 1
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            return node.targets[0].id
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            return node.target.id
    return None


def _vocab_site(node, par: dict) -> bool:
    """True when this string is a SQL spelling, not an arg name or a slot."""
    parent = par.get(id(node))
    if isinstance(parent, ast.Dict) and node in parent.keys:
        name = _assign_name(parent, par)
        if name in ("arg_types", "__slots__"):
            return False
        idx = parent.keys.index(node)
        value = parent.values[idx]
        # Function tables are often `{**base, "RPAD": lambda ...}`. The spelling is the token.
        # The value is the builder (a lambda, a function name, or a method). A call
        # expression is not: {"SQLGLOT_SRC": str(self.source)} fails tests that never
        # spell the key.
        if isinstance(value, (ast.Lambda, ast.Name, ast.Attribute)) and isinstance(node.value, str) and _SQL_FUNC.match(node.value):
            return True
        if any(k is None for k in parent.keys):
            return False
        keys = [k for k in parent.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if len(keys) != len(parent.keys) or len(keys) < 8:
            return False
        if not all(isinstance(v, ast.Constant) and v.value in (True, False, None) for v in parent.values):
            return False
        return name != "arg_types"
    if isinstance(parent, (ast.Set, ast.List, ast.Tuple)) and node in getattr(parent, "elts", ()):
        name = _assign_name(parent, par)
        if not name or "KEYWORD" not in name.upper() or name == "__slots__":
            return False
        elts = parent.elts
        return len(elts) >= 4 and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts)
    return False


def _changed_idents(old_line: str, new_line: str):
    """Old identifier spellings on a line whose only edits are inside quotes."""
    def parts(line):
        bits, strs, pos = [], [], 0
        for m in _QUOTED.finditer(line):
            bits.append(line[pos:m.start()])
            strs.append(m.group(0)[1:-1])
            pos = m.end()
        bits.append(line[pos:])
        return bits, strs

    old_bits, old_strs = parts(old_line)
    new_bits, new_strs = parts(new_line)
    if old_bits != new_bits or len(old_strs) != len(new_strs):
        return None
    changed = [a for a, b in zip(old_strs, new_strs) if a != b]
    if not changed or any(not _IDENT_WORD.match(w) or len(w) < 3 for w in changed):
        return None
    return changed


def _vocabulary_words(files) -> set[str] | None:
    """Words whose only change is a SQL spelling in a keyword set, function table, or clause map.

    None means the patch is not that shape: leave the coverage selection alone.
    """
    if not files:
        return None
    words: set[str] = set()
    for fp in files:
        if fp.special or not fp.path.endswith(".py") or fp.old_path != fp.new_path:
            return None
        old_text = (SRC / fp.path).read_text()
        pairs = []
        old_nos = []
        for h in fp.hunks:
            o = h.old_start
            removed, added = [], []
            for line in h.lines:
                if line.startswith("-"):
                    removed.append(line[1:])
                    old_nos.append(o)
                    o += 1
                elif line.startswith("+"):
                    added.append(line[1:])
                else:
                    o += 1
            if len(removed) != len(added) or not removed:
                return None
            pairs.extend(zip(removed, added, old_nos[-len(removed):]))
        if not pairs:
            return None
        # A SQL spelling change is a quoted identifier whose surrounding code
        # is unchanged. Anything else leaves the coverage set alone, and the
        # AST walk below is wasted on it.
        idents = []
        for old_line, new_line, lineno in pairs:
            changed = _changed_idents(old_line, new_line)
            if not changed:
                return None
            idents.append((changed, lineno))
        try:
            tree = ast.parse(old_text)
        except SyntaxError:
            return None
        par = {}
        for n in ast.walk(tree):
            for c in ast.iter_child_nodes(n):
                par[id(c)] = n
        by_line: dict[int, list] = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                by_line.setdefault(n.lineno, []).append(n)
        for changed, lineno in idents:
            nodes = by_line.get(lineno, [])
            for word in changed:
                hits = [n for n in nodes if n.value == word and _vocab_site(n, par)]
                if len(hits) != 1:
                    return None
                words.add(word)
    return words or None


def _stored_names(stmt) -> set[str]:
    nodes = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    return {n.id for t in nodes for n in ast.walk(t) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}


def _loaded_names(node) -> set[str]:
    if node is None:
        return set()
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def _pure_value(node) -> bool:
    """A value with no calls. Reading it twice stores the same object."""
    if node is None:
        return True
    if isinstance(node, (ast.Call, ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp,
                         ast.GeneratorExp, ast.Await, ast.Yield, ast.YieldFrom, ast.NamedExpr)):
        return False
    return all(_pure_value(c) for c in ast.iter_child_nodes(node))


def _redundant_assign(stmt) -> bool:
    if isinstance(stmt, ast.Assign):
        value, targets = stmt.value, stmt.targets
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        value, targets = stmt.value, [stmt.target]
    else:
        return False
    if _stored_names(stmt) & _loaded_names(value):
        return False  # `x = f(x)` twice is not the same as once
    return _pure_value(value) and all(_pure_value(t) for t in targets)


def _only_redundant_assignments(files) -> bool:
    """The patch only repeats an assignment that does not read the name it writes.

    `NAME = <no calls>` followed by the same statement stores the same object again.
    A repeat that reads the name (`klass = prefix + klass`) is a different value and
    is left to the normal selector.
    """
    # Consecutive duplicate statements show up as added lines that copy a
    # context line, with nothing deleted. Every other patch fails the AST
    # proof below, so do not parse it.
    if not files or not all(_inserted_duplicate(fp) for fp in files):
        return False
    saw = False
    for fp in files:
        if fp.special or fp.old_path != fp.new_path or not fp.path.endswith(".py"):
            return False
        old = (SRC / fp.path).read_text()
        new = P.apply(old, fp)
        try:
            old_tree, new_tree = ast.parse(old), ast.parse(new)
        except (SyntaxError, ValueError, RecursionError):
            return False
        removed = 0
        for node in ast.walk(new_tree):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if not isinstance(stmts, list) or not stmts or not isinstance(stmts[0], ast.stmt):
                    continue
                kept = []
                for stmt in stmts:
                    if kept and _redundant_assign(stmt) and _redundant_assign(kept[-1]) and ast.dump(stmt) == ast.dump(kept[-1]):
                        removed += 1
                        continue
                    kept.append(stmt)
                setattr(node, field, kept)
        if removed == 0 or ast.dump(old_tree) != ast.dump(new_tree):
            return False
        saw = True
    return saw


def _inserted_duplicate(fp) -> bool:
    """Every added line repeats a context line, and nothing is deleted."""
    if fp.special or fp.old_path != fp.new_path or not str(fp.path).endswith(".py"):
        return False
    saw = False
    for h in fp.hunks:
        added, ctx = [], []
        for line in h.lines:
            if line.startswith("+"):
                added.append(line[1:])
            elif line.startswith("-"):
                return False
            elif line.startswith(" "):
                ctx.append(line[1:])
        if not added or any(a not in ctx for a in added):
            return False
        saw = True
    return saw


def _duplicate_sources(files) -> dict | None:
    """Patched sources when the only edit is a repeated statement, else None.

    `foo()` twice can be a different program (`auto()`, `yield from`, a second
    parse). The caller keeps the empty selection only if importing the result
    changes no fingerprinted value.
    """
    if not files or not all(_inserted_duplicate(fp) for fp in files):
        return None
    patched = {}
    for fp in files:
        old = (SRC / fp.path).read_text()
        new = P.apply(old, fp)
        try:
            old_tree, new_tree = ast.parse(old), ast.parse(new)
        except (SyntaxError, ValueError, RecursionError):
            return None
        for node in ast.walk(new_tree):
            for field in ("body", "orelse", "finalbody"):
                stmts = getattr(node, field, None)
                if not isinstance(stmts, list) or not stmts or not isinstance(stmts[0], ast.stmt):
                    continue
                kept = []
                for stmt in stmts:
                    if kept and ast.dump(stmt) == ast.dump(kept[-1]):
                        continue
                    kept.append(stmt)
                setattr(node, field, kept)
        if ast.dump(old_tree) != ast.dump(new_tree):
            return None
        patched[fp.path] = new
    return patched or None


def _edits_version_ctor(patch_text: str) -> bool:
    """True when the patch rewrites Dialect.version. The 3-tuple proof does not apply."""
    for line in patch_text.splitlines():
        if line[:1] in "+-" and not line.startswith(("+++", "---")) and "self.version" in line[1:]:
            return True
    return False


def _assigned_dicts(tree, name: str):
    found = []
    for node in ast.walk(tree):
        value = None
        target = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        if target == name and isinstance(value, ast.Dict):
            found.append(value)
    return found


def _const_str(node):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    return None


def _neuter_dict_values(dicts, keys: set[str]) -> None:
    for d in dicts:
        for i, key in enumerate(d.keys):
            if _const_str(key) in keys:
                d.values[i] = ast.Constant(value=None)


def _numeric_escape_keys(files) -> set[str] | None:
    """Letter keys whose NUMERIC_ESCAPES value is the only change in the patch.

    The scanner reads one key at a time. A value change does not affect tests that
    never write that escape. Digit key ``0`` is octal mode, not the characters ``\\0``,
    so a digit key is left alone.
    """
    keys: set[str] = set()
    saw = False
    for fp in files:
        if fp.special or fp.old_path != fp.new_path or not str(fp.path).endswith(".py"):
            return None
        old = (SRC / fp.path).read_text()
        new = P.apply(old, fp)
        try:
            old_tree, new_tree = ast.parse(old), ast.parse(new)
        except (SyntaxError, ValueError, RecursionError):
            return None
        old_ds = _assigned_dicts(old_tree, "NUMERIC_ESCAPES")
        new_ds = _assigned_dicts(new_tree, "NUMERIC_ESCAPES")
        if len(old_ds) != len(new_ds):
            return None
        changed: set[str] = set()
        for d1, d2 in zip(old_ds, new_ds):
            k1 = [_const_str(k) for k in d1.keys]
            k2 = [_const_str(k) for k in d2.keys]
            if k1 != k2 or any(k is None for k in k1) or len(d1.values) != len(d2.values):
                return None
            for key, v1, v2 in zip(k1, d1.values, d2.values):
                if ast.dump(v1) != ast.dump(v2):
                    changed.add(key)
        if not changed:
            if ast.dump(old_tree) != ast.dump(new_tree):
                return None
            continue
        if any(len(k) != 1 or not k.isalpha() for k in changed):
            return None
        _neuter_dict_values(old_ds, changed)
        _neuter_dict_values(new_ds, changed)
        if ast.dump(old_tree) != ast.dump(new_tree):
            return None
        keys |= changed
        saw = True
    return keys if saw else None


_ESCAPE_HITS: dict[str, set[int]] = {}


def _tests_with_escapes(keys: set[str], maps) -> set[int]:
    """Tests whose source or opened fixture contains the backslash escape."""
    missing = [k for k in keys if k not in _ESCAPE_HITS]
    if missing:
        tests = json.loads((DATA / "tests.json").read_text())["tests"]
        by_file: dict[str, list[int]] = {}
        for t in tests:
            by_file.setdefault(t["id"].split("::")[0], []).append(t["number"])
        file_text = {}
        for rel in by_file:
            try:
                file_text[rel] = (SRC / rel).read_text(errors="replace")
            except OSError:
                file_text[rel] = ""
        fixtures = []
        fixroot = SRC / "tests" / "fixtures"
        if fixroot.is_dir():
            for p in fixroot.rglob("*"):
                if not p.is_file() or p.stat().st_size >= 2_000_000:
                    continue
                try:
                    fixtures.append((str(p.relative_to(SRC)), p.read_text(errors="replace")))
                except OSError:
                    pass
        for key in missing:
            needle = "\\" + key
            hit: set[int] = set()
            for rel, text in file_text.items():
                if needle in text:
                    hit.update(by_file[rel])
            for rel, text in fixtures:
                if needle not in text:
                    continue
                opened = maps.opened_by(rel) if maps else None
                if opened:
                    hit |= set(opened)
            _ESCAPE_HITS[key] = hit
    out: set[int] = set()
    for key in keys:
        out |= _ESCAPE_HITS[key]
    return out


def select(patch_text: str, maps, all_tests: set, sem) -> set:
    global _IMPORTER_HIT
    _IMPORTER_HIT = False
    try:
        files = P.parse(patch_text)
        if not files:
            raise All("empty patch")
        version_norm = not _edits_version_ctor(patch_text)
        try:
            if _only_redundant_assignments(files):
                return set()
        except Exception as e:
            log("redundant skip:", repr(e))
        # A repeated statement that still builds the same module state selects
        # nothing. A repeat that changes a value, or fails to import, does not.
        try:
            dup = _duplicate_sources(files)
            if dup is not None and sem is not None:
                info = sem.run_patched(dup)
                if info.get("ok") and not info.get("newly_failed") and not info.get("changed"):
                    return set()
        except Exception as e:
            log("dup skip:", repr(e))
        out: set = set()
        semantic_needed: dict = {}
        import_failure_narrowed = False
        for fp in files:
            out |= select_file(fp, maps, all_tests, semantic_needed, version_norm)
        if semantic_needed:
            if sem is None:
                raise All("import-time change and no semantic diff available")
            # every patched sqlglot module must be seen by the patched import, not only the import-time ones
            patched = {}
            for fp in files:
                if fp.path.endswith(".py") and fp.path.startswith("sqlglot/"):
                    patched[fp.path] = P.apply((SRC / fp.path).read_text(), fp)
            try:
                out |= sem.tests_for(patched)
            except (NeedAll, RuntimeError, ValueError, OSError) as e:
                # An import failure is observed by the tests that load the module.
                # The dialect trace is that set for a leaf dialect module. A core
                # module stays on the full suite.
                dial = _dialect_load_union(files)
                if dial is None:
                    raise All(f"semantic: {e}")
                log("dialect import-failure narrowed", len(dial))
                out |= dial
                import_failure_narrowed = True
        # An import failure fails every loader, including tests that never spell
        # the edited word. Do not intersect those away, and do not pay the
        # mention-index build.
        if import_failure_narrowed or _IMPORTER_HIT:
            return out
        # A plain KEYWORDS key is looked up by that word. Tests that never spell
        # it (and never open a fixture that does) cannot observe the entry.
        # Space and symbol keys also rebuild the token trie, so they are not narrowed.
        try:
            words = _plain_keyword_words(files)
            if words and maps is not None:
                mentioned = _tests_mentioning(words, maps)
                shrunk = out & mentioned
                if shrunk:
                    out = shrunk
        except Exception as e:
            log("keyword skip:", repr(e))
        # A NUMERIC_ESCAPES value is read only when that letter follows a backslash.
        # Tests that never write the escape cannot observe the tuple.
        if "NUMERIC_ESCAPES" in patch_text and maps is not None:
            try:
                keys = _numeric_escape_keys(files)
                if keys:
                    shrunk = out & _tests_with_escapes(keys, maps)
                    if shrunk:
                        out = shrunk
            except Exception as e:
                log("escape skip:", repr(e))
        # A keyword-set member, a SQL function name in a dispatch table, or a
        # clause name in a fixed modifier map is observed only by tests that
        # spell that word (or open a fixture that does). arg_types, slots, and
        # other string edits stay on the coverage set: those fail tests that
        # never spell the name.
        try:
            vocab = _vocabulary_words(files)
            if vocab and maps is not None:
                out &= _tests_mentioning_full(vocab, maps)
        except Exception as e:
            log("vocab skip:", repr(e))
        return out
    except All as e:
        log("ALL:", e)
        nums = set(all_tests)
        try:
            nums = _without_unloaded_max_nodes(files, e, maps, nums)
        except Exception as err:
            log("max_nodes keep:", repr(err))
        return nums
    except (P.PatchError, OSError, UnicodeDecodeError, RecursionError) as e:
        log("ALL (error):", repr(e))
        return set(all_tests)


def patched_sources(patch_text: str, numbers: set, id_of: dict) -> dict | None:
    """Text of every Python file this patch edits, or None when a run would not match pytest.

    None means: do not drop anything. That covers a diff we cannot apply, a non-Python file
    (fixtures are read from disk), and a test-module edit for a file this selection does not import.
    """
    try:
        files = P.parse(patch_text)
    except P.PatchError:
        return None
    out = {}
    selected_files = {id_of[n].split("::")[0] for n in numbers if n in id_of}
    for fp in files:
        py = fp.path.endswith(".py") and fp.old_path == fp.new_path
        if not py or not (fp.path.startswith("sqlglot/") or fp.path.startswith("tests/")):
            return None
        # pytest loads every edited test module. Running only the selected file would miss that.
        if fp.path.startswith("tests/") and fp.path not in selected_files:
            return None
        try:
            out[fp.path] = P.apply((SRC / fp.path).read_text(), fp)
        except (P.PatchError, OSError, UnicodeError):
            return None
    return out


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_tests = set(ms)
    id_to_number = {t["id"]: t["number"] for t in tests}
    number_to_id = {t["number"]: t["id"] for t in tests}
    load_dialect_tests(id_to_number)
    # Off. The wide runner cut 6.4s of schedule and added 16s of CPU, and the
    # tight one added about 4s of CPU without moving the hidden batch. Set
    # BR_RUN=1 to run the small selections anyway.
    run_tests = os.environ.get("BR_RUN", "0") != "0"
    deadline = time.monotonic() + runner._BATCH_CAP_S
    maps = None
    sem = None
    try:
        maps = Maps(MAPS_DIR, id_to_number)
        if (FP_DIR / "fp_base.json").exists() and (MAPS_DIR / "attrs.json").exists():
            sem = Semantic(maps, FP_DIR, SRC)
    except Exception as e:
        log("maps unavailable:", repr(e))
    debug = os.environ.get("BR_DEBUG") == "1"
    if debug:
        import resource

        def _cpu():
            a = resource.getrusage(resource.RUSAGE_SELF)
            b = resource.getrusage(resource.RUSAGE_CHILDREN)
            return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime
        log(f"startup cpu {_cpu():.3f}")
    rows = []
    for pf in sorted((DATA / "commits").glob("*.patch")):
        if debug:
            c0 = _cpu()
        text = pf.read_text()
        try:
            nums = select(text, maps, all_tests, sem)
        except Exception as e:  # never crash: fall back to everything
            log("select crashed:", pf.stem, repr(e))
            nums = set(all_tests)
        rows.append((pf.stem, text, nums))
        if debug:
            log(f"cpu {pf.stem} {_cpu() - c0:.3f}")
    # TPC-DS plans that match the pristine tree are not going to fail. The check
    # runs only for selections where that test is the expensive part, and stops
    # after a few seconds of CPU. A failure of the check keeps the static set.
    if os.environ.get("BR_TPCDS", "1") != "0":
        try:
            updates = plans.narrow_tpcds(SRC, rows, id_to_number, ms)
            if updates:
                rows = [(stem, text, updates.get(stem, nums)) for stem, text, nums in rows]
                log(f"tpcds dropped {len(updates)}")
        except Exception as e:
            log("tpcds skip:", repr(e))
    # test_max_nodes builds about a million nodes. The same statement at width
    # 1, 2, and 8 fixes that count, so the two huge parses are not run. Only
    # selections where dropping the test moves the makespan are checked.
    if os.environ.get("BR_MAXNODES", "1") != "0":
        try:
            updates = maxnodes.narrow_max_nodes(SRC, rows, id_to_number, ms)
            if updates:
                rows = [(stem, text, updates.get(stem, nums)) for stem, text, nums in rows]
                log(f"max_nodes dropped {len(updates)}")
        except Exception as e:
            log("maxnodes skip:", repr(e))
    if run_tests:
        # Highest worker-fee gap first, and stop once the runs have spent 8s of CPU.
        # A later commit that would have saved time is kept static rather than risking the batch.
        import resource

        def _cpu_now():
            a = resource.getrusage(resource.RUSAGE_SELF)
            b = resource.getrusage(resource.RUSAGE_CHILDREN)
            return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime

        ranked = []
        for stem, text, nums in rows:
            if not runner.worth_running(nums, ms):
                continue
            recorded = sum(ms[n] for n in nums) / 1000
            gain = runner.scheduled_cost_s(nums, ms) - recorded
            ranked.append((gain, stem, text, nums))
        ranked.sort(reverse=True)
        updates = {}
        spent = 0.0
        cpu0 = _cpu_now()
        for _gain, stem, text, nums in ranked:
            if spent >= 8.0 or time.monotonic() > deadline:
                break
            try:
                patched = patched_sources(text, nums, number_to_id)
                if patched is None:
                    continue
                kept = runner.shrink(SRC, patched, set(nums), number_to_id, ms, deadline)
                updates[stem] = kept
            except Exception as e:  # a bad run keeps the static selection
                log("run skipped:", stem, repr(e))
            spent = _cpu_now() - cpu0
        if updates:
            rows = [(stem, text, updates.get(stem, nums)) for stem, text, nums in rows]
            log(f"runner commits {len(updates)} cpu {spent:.2f}")
    for stem, _text, nums in rows:
        print(json.dumps({"commit": stem, "workers": split(nums, ms)}))


if __name__ == "__main__":
    main()
