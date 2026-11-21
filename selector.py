"""Blast Radius selector.

For each patch: apply it to the pristine sqlglot source, structurally diff the AST, and map each
changed region to the tests that executed it (per-test coverage map built from the pinned commit).
Anything not provably safe falls back to running every test for that commit.

Stdout: exactly one JSON line per commit. Everything else goes to stderr.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from blast import astdiff  # noqa: E402
from blast import patch as P  # noqa: E402
from blast.maps import Maps  # noqa: E402
from blast.semantic import NeedAll, Semantic  # noqa: E402
from blast.split import split  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
SRC = Path(os.environ.get("BR_SRC", "/opt/sqlglot"))
MAPS_DIR = Path(os.environ.get("BR_MAPS", str(HERE / "maps")))
FP_DIR = Path(os.environ.get("BR_FP", str(HERE / "fp")))

CONFIG_BASENAMES = {
    "pyproject.toml", "setup.cfg", "setup.py", "conftest.py", "pytest.ini", "tox.ini", "MANIFEST.in",
    "Makefile", "requirements.txt", "requirements-dev.txt", "__init__.py",
}
INERT_PREFIXES = ("benchmarks/", "posts/", "docs/", ".github/")


def log(*a):
    print(*a, file=sys.stderr)


class All(Exception):
    """Raised when the conservative answer (run everything) is required."""


def select_file(fp, maps, all_tests: set, semantic_needed: dict) -> set:
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
    regions = astdiff.diff(old, new)
    if regions is None:
        raise All(f"unparsable: {path}")  # syntax error etc.: import failures hit everything importing it
    out: set = set()
    for r in regions:
        if r.kind == "doc":
            if r.doctest:
                out |= {n for f in ("tests/test_docs.py",) for n in maps.file_tests(f)} or set(all_tests)
            continue
        if r.start == 0:
            raise All(f"module-level structural change: {path}")
        tests, imp, hit = maps.region_tests(path, r.start, r.end)
        if r.kind == "rt":
            out |= tests
            if imp and not r.lam:  # function body that also runs at import time
                if is_tests:
                    out |= maps.file_tests(path)
                else:
                    semantic_needed[path] = new
        else:  # import-time code
            if not hit:
                continue  # never executed: cannot affect any test
            if is_tests:
                out |= maps.file_tests(path)
            else:
                semantic_needed[path] = new
    return out


def select(patch_text: str, maps, all_tests: set, sem) -> set:
    try:
        files = P.parse(patch_text)
        if not files:
            raise All("empty patch")
        out: set = set()
        semantic_needed: dict = {}
        for fp in files:
            out |= select_file(fp, maps, all_tests, semantic_needed)
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
        return out
    except All as e:
        log("ALL:", e)
        return set(all_tests)
    except (P.PatchError, OSError, UnicodeDecodeError, RecursionError) as e:
        log("ALL (error):", repr(e))
        return set(all_tests)


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_text())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    all_tests = set(ms)
    id_to_number = {t["id"]: t["number"] for t in tests}
    maps = None
    sem = None
    try:
        maps = Maps(MAPS_DIR, id_to_number)
        if (FP_DIR / "fp_base.json").exists() and (MAPS_DIR / "attrs.json").exists():
            sem = Semantic(maps, FP_DIR, SRC)
    except Exception as e:
        log("maps unavailable:", repr(e))
    for pf in sorted((DATA / "commits").glob("*.patch")):
        try:
            nums = select(pf.read_text(), maps, all_tests, sem)
        except Exception as e:  # never crash: fall back to everything
            log("select crashed:", pf.stem, repr(e))
            nums = set(all_tests)
        print(json.dumps({"commit": pf.stem, "workers": split(nums, ms)}))


if __name__ == "__main__":
    main()
