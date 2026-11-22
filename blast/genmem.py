"""Drop test_generator_memory when no edited line can run during it.

The test imports every dialect and builds each generator 1100 times. Its map
(tools/genmem_map.py, pinned tree, image build) lists every sqlglot code object
the test enters: module bodies, class bodies and functions. A commit drops the
test only when every file edit is a sqlglot .py change in place (no rename, new,
deleted or binary file) whose patched text compiles, and every changed line sits
inside the body of a function the test never enters. Edits to module or class
level code, decorators, signatures or defaults belong to the enclosing scope,
which the imports enter, so they keep the test. No child process runs.
"""
from __future__ import annotations

import ast
import json
import warnings
from pathlib import Path

from blast import patch as P

GENMEM_ID = "tests/test_generator.py::TestGenerator::test_generator_memory"


def _scopes(text: str) -> list:
    out = []
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = min([n.lineno] + [d.lineno for d in n.decorator_list])
            out.append((n.body[0].lineno, n.end_lineno, None if isinstance(n, ast.ClassDef) else first, n.lineno))
    return out


def _key_of(scopes: list, rel: str, line: int):
    """Innermost function whose body holds `line` (not its def line); None for
    module level and class bodies, which run on import."""
    best = None
    for b0, end, first, defl in scopes:
        if b0 <= line <= end and line > defl and (best is None or b0 >= best[0]):
            best = (b0, first)
    return None if best is None or best[1] is None else f"{rel}:{best[1]}"


def _keys(scopes: list, rel: str, fp) -> set:
    keys = set()
    for h in fp.hunks:
        old = h.old_start
        run_del = ins = False
        for ln in list(h.lines) + [" "]:
            t = ln[:1]
            if t == "-":
                keys.add(_key_of(scopes, rel, old))
                old += 1
                run_del = True
            elif t == "+":
                ins = True
            else:
                if ins and not run_del:  # pure insertion between old-1 and old
                    a, b = _key_of(scopes, rel, old - 1), _key_of(scopes, rel, old)
                    keys.add(a if a == b else None)
                ins = run_del = False
                old += 1
    return keys


def _unreached(src: Path, text: str, entered: set, loaded: set, cache: dict) -> bool:
    try:
        files = P.parse(text)
    except P.PatchError:
        return False
    if not files:
        return False
    for fp in files:
        path = str(fp.path)
        if fp.special or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            return False
        if not path.startswith("sqlglot/") or not path.endswith(".py") or not fp.hunks:
            return False
        try:
            old = (src / path).read_text()
            new = P.apply(old, fp)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                compile(new, path, "exec", dont_inherit=True)
        except Exception:  # patch does not apply, or the file no longer compiles: keep
            return False
        if path not in loaded:
            continue  # never imported by the test at the pinned commit
        if path not in cache:
            cache[path] = _scopes(old)
        keys = _keys(cache[path], path, fp)
        if None in keys or keys & entered:
            return False
    return True


def narrow_genmem(src: Path, rows: list, id_to_number: dict, map_path: Path) -> dict:
    """{commit: numbers without test_generator_memory} for commits it cannot see."""
    number = id_to_number.get(GENMEM_ID)
    if number is None:
        return {}
    try:
        data = json.loads(Path(map_path).read_text())
    except (OSError, ValueError):
        return {}
    entered, loaded = set(data["entered"]), set(data["loaded"])
    src = Path(src).resolve()
    cache: dict = {}
    updates = {}
    for stem, text, nums in rows:
        if number in nums and _unreached(src, text, entered, loaded, cache):
            updates[stem] = set(nums) - {number}
    return updates
