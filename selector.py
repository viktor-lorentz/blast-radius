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
        # Only leaf dialect/parser/generator modules have a load map; anything else
        # (or a map that resolved to no tests) still runs the full suite.
        loaded = DIALECT_TESTS.get(_module_name(path))
        if loaded:
            return set(loaded)
        raise All(f"unparsable: {path}")
    out: set = set()
    for r in regions:
        if r.kind == "doc":
            # tests/test_docs.py is not in the scored suite. No scored test reads __doc__.
            if r.doctest:
                out |= {n for f in ("tests/test_docs.py",) for n in maps.file_tests(f)}
            continue
        if r.start == 0:
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
    _MENTION_INDEX = (segs, fixtures)
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
                raise All(f"semantic: {e}")
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
        return out
    except All as e:
        log("ALL:", e)
        return set(all_tests)
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
    # On for the small selections only. The wide runner (any set cheaper on paper
    # than its schedule) cut 6.4s and added 16s of CPU. worth_running now refuses
    # that. Set BR_RUN=0 to skip the runs.
    run_tests = os.environ.get("BR_RUN", "1") != "0"
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
