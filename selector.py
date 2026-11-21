"""Blast Radius selector (v0): AST no-op detection, otherwise run everything with the best split.

Stdout: exactly one JSON line per commit. Everything else goes to stderr.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from blast import patch as P  # noqa: E402
from blast.split import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))


def log(*a):
    print(*a, file=sys.stderr)


def ast_same(old: str, new: str) -> bool:
    try:
        return ast.dump(ast.parse(old)) == ast.dump(ast.parse(new))
    except SyntaxError:
        return False


def select(patch_text: str, all_tests: list) -> list:
    """Return the set of test numbers to run for one patch."""
    try:
        files = P.parse(patch_text)
    except P.PatchError as e:
        log("parse failed:", e)
        return all_tests
    if not files:
        return all_tests
    for fp in files:
        if fp.special or fp.old_path is None or fp.new_path is None or fp.old_path != fp.new_path:
            return all_tests
        if not fp.path.endswith(".py"):
            return all_tests
        try:
            old = (SRC / fp.old_path).read_text()
            new = P.apply(old, fp)
        except (OSError, P.PatchError, UnicodeDecodeError) as e:
            log("apply failed:", fp.path, e)
            return all_tests
        if not ast_same(old, new):
            return all_tests
    return []


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_tests = sorted(ms)
    for pf in sorted((DATA / "commits").glob("*.patch")):
        try:
            nums = select(pf.read_text(), all_tests)
        except Exception as e:  # never crash: fall back to everything
            log("select crashed:", pf.stem, repr(e))
            nums = all_tests
        print(json.dumps({"commit": pf.stem, "workers": split(nums, ms)}))


if __name__ == "__main__":
    main()
