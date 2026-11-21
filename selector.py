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
_TEST_IMPORTERS = {}


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
            # tests/test_docs.py is not in the scored suite. No scored test reads __doc__.
            if r.doctest:
                out |= {n for f in ("tests/test_docs.py",) for n in maps.file_tests(f)}
            continue
        if r.start == 0:
            raise All(f"module-level structural change: {path}")
        tests, imp, hit = maps.region_tests(path, r.start, r.end)
        if is_tests and _SIDE_EFFECTS.search(old):
            # a test module that mutates the filesystem / environment / processes can break unrelated tests
            # through shared state: any change to it may fail tests outside its own file
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
    # Off by default. On the practice batch the runs cut 6.4s of simulated time and
    # added 16s of CPU (BB-AD82E23D, 185.3). Set BR_RUN=1 to enable them.
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
    for pf in sorted((DATA / "commits").glob("*.patch")):
        if debug:
            c0 = _cpu()
        text = pf.read_text()
        try:
            nums = select(text, maps, all_tests, sem)
        except Exception as e:  # never crash: fall back to everything
            log("select crashed:", pf.stem, repr(e))
            nums = set(all_tests)
        if run_tests:
            try:
                patched = patched_sources(text, nums, number_to_id)
                if patched is not None:
                    nums = runner.shrink(SRC, patched, nums, number_to_id, ms, deadline)
            except Exception as e:  # a bad run keeps the static selection
                log("run skipped:", pf.stem, repr(e))
        print(json.dumps({"commit": pf.stem, "workers": split(nums, ms)}))
        if debug:
            log(f"cpu {pf.stem} {_cpu() - c0:.3f}")


if __name__ == "__main__":
    main()
