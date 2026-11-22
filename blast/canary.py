"""Canary runner for the heavy tests.

A heavy test (test_execute_tpcds, test_max_nodes, test_generator_memory) is dropped from a
selection only when cheap tests that fail alongside it were run under the patched tree and all
passed. The canaries for a heavy test are the selected tests of a few related test files that
record at most CANARY_MAX_S. No table from a patch or a kind of edit to tests is involved: the
canary files are fixed per heavy test, and which of them run is the static selection.

Mechanics: the selector process forks once per commit. The child builds a patched view of the
sqlglot checkout under /tmp (symlinks to the pristine tree, real files only along patched paths,
so fixture edits are seen), drops any sqlglot/tests modules it inherited, and runs the canaries
with unittest, cheapest first, stopping at the first failure. RLIMIT_CPU caps the child and the
parent kills it at a wall deadline. Anything unexpected keeps the heavy test.
"""
from __future__ import annotations

import os
import resource
import select
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set

HEAVY = {
    "tests/test_executor.py::TestExecutor::test_execute_tpcds": (
        "tests/test_optimizer.py",  # cheap setUpClass: runs first when hits tie
        "tests/test_executor.py",  # setUpClass loads every TPC-H/TPC-DS csv
    ),
    "tests/test_parser.py::TestParser::test_max_nodes": (
        "tests/test_parser.py",
        "tests/test_transpile.py",
    ),
    "tests/test_generator.py::TestGenerator::test_generator_memory": (
        "tests/test_generator.py",
        "tests/dialects/test_dialect.py",
    ),
}
CANARY_MAX_S = float(os.environ.get("BR_CANARY_MAX", "0.45"))  # recorded seconds per canary
COMMIT_CPU_S = float(os.environ.get("BR_CANARY_COMMIT_CPU", "3.5"))
BATCH_CPU_S = float(os.environ.get("BR_CANARY_CPU", "3.5"))
_PRELOAD = ("unittest", "unittest.mock", "csv", "sqlite3", "decimal", "concurrent.futures",
            "multiprocessing", "duckdb", "numpy", "pandas", "pandas.testing")


def canaries(heavy_id: str, numbers: Set[int], id_of: Dict[int, str], ms: Dict[int, int],
             hits: Optional[Dict[int, int]] = None) -> List[int]:
    files = HEAVY[heavy_id]
    heavy_ids = set(HEAVY)
    out = [n for n in numbers
           if id_of[n] not in heavy_ids
           and id_of[n].split("::")[0] in files
           and ms[n] <= CANARY_MAX_S * 1000]
    # Likely failures first so a failing patch stops early; one block per class so
    # setUpClass runs once. hits: patched hunks each test executed at the pinned commit.
    hits = hits or {}
    cls = lambda n: id_of[n].rsplit("::", 1)[0]
    top: Dict[str, int] = {}
    for n in out:
        top[cls(n)] = max(top.get(cls(n), 0), hits.get(n, 0))
    rank = {f: i for i, f in enumerate(files)}  # file order = class setup cost
    return sorted(out, key=lambda n: (-top[cls(n)], rank[id_of[n].split("::")[0]], cls(n),
                                      -hits.get(n, 0), ms[n], n))


def _overlay(src: Path, patched: Dict[str, str], root: str) -> None:
    """Symlink tree of src under root; each patched file is written as a real file."""
    dirs: Set[str] = set()
    for rel in patched:
        parts = rel.split("/")[:-1]
        for i in range(len(parts)):
            dirs.add("/".join(parts[: i + 1]))

    def fill(rel_dir: str) -> None:
        here = src / rel_dir if rel_dir else src
        dst = os.path.join(root, rel_dir) if rel_dir else root
        os.makedirs(dst, exist_ok=True)
        for name in os.listdir(here):
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if rel in patched:
                with open(os.path.join(dst, name), "w") as f:
                    f.write(patched[rel])
            elif rel in dirs:
                fill(rel)
            else:
                os.symlink(here / name, os.path.join(dst, name))

    fill("")


def _child(src: Path, patched: Dict[str, str], test_ids: List[str], wfd: int, cpu: float, root: str) -> None:
    code = b"E"
    try:
        soft = max(1, int(cpu) + 1)
        resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 1))
        for name in list(sys.modules):
            if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
                del sys.modules[name]
        _overlay(src, patched, root)
        sys.path[:] = [root] + [p for p in sys.path if p not in ("", str(src))]
        os.chdir(root)
        import concurrent.futures as cf
        import importlib
        import unittest

        class _Inline(cf.Executor):
            # Spawn pools re-import sqlglot per worker; run the work in this process.
            def __init__(self, *a, initializer=None, initargs=(), **kw):
                if initializer is not None:
                    initializer(*initargs)

            def submit(self, fn, *a, **kw):
                f = cf.Future()
                try:
                    f.set_result(fn(*a, **kw))
                except BaseException as e:
                    f.set_exception(e)
                return f

        cf.ProcessPoolExecutor = _Inline
        loader = unittest.defaultTestLoader
        suite = unittest.TestSuite()
        for tid in test_ids:  # grouped by class so setUpClass runs once
            rel, cls, meth = tid.split("::")
            mod = importlib.import_module(rel[:-3].replace("/", "."))
            if hasattr(mod, "ProcessPoolExecutor"):
                mod.ProcessPoolExecutor = _Inline
            suite.addTests(loader.loadTestsFromName(f"{cls}.{meth}", mod))
        result = unittest.TestResult()
        result.failfast = True
        _t = time.process_time()
        suite.run(result)
        if os.environ.get("BR_CANARY_DEBUG"):
            print(f"canary child load {_t:.2f} run {time.process_time()-_t:.2f} ran {result.testsRun}/{len(test_ids)} "
                  f"fail {[t.id() for t, _ in (result.failures + result.errors)][:2]}", file=sys.stderr, flush=True)
        bad = result.failures or result.errors or result.unexpectedSuccesses
        code = b"F" if bad or result.testsRun != len(test_ids) else b"P"
    except BaseException:
        code = b"E"
    try:
        os.write(wfd, code)
    finally:
        os._exit(0)


class Runner:
    def __init__(self, src: Path):
        self.src = src
        self.spent = 0.0
        self.preloaded = False
        self.runs = 0

    def _preload(self) -> None:
        if self.preloaded or os.environ.get("BR_CANARY_PRELOAD", "1") == "0":
            return
        self.preloaded = True
        import importlib
        for m in _PRELOAD:
            try:
                importlib.import_module(m)
            except Exception:
                pass

    def left(self) -> float:
        return BATCH_CPU_S - self.spent

    def all_pass(self, patched: Dict[str, str], test_ids: List[str]) -> bool:
        """True only when every canary ran and passed under the patched tree."""
        if not test_ids:
            return False
        cpu = min(COMMIT_CPU_S, self.left())
        if cpu <= 0.5:
            return False
        self._preload()
        root = tempfile.mkdtemp(prefix="brc", dir=os.environ.get("TMPDIR", "/tmp"))
        r, w = os.pipe()
        t0 = time.monotonic()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            _child(self.src, patched, test_ids, w, cpu, root)
        os.close(w)
        self.runs += 1
        code = b""
        deadline = t0 + 2 * cpu + 2
        try:
            while True:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    os.kill(pid, signal.SIGKILL)
                    break
                ready, _, _ = select.select([r], [], [], wait)
                if ready:
                    code = os.read(r, 1)
                    break
        finally:
            os.close(r)
            _, _, ru = os.wait4(pid, 0)
            self.spent += ru.ru_utime + ru.ru_stime
            shutil.rmtree(root, ignore_errors=True)
        return code == b"P"


def cleanup_tmp() -> None:
    tmp = os.environ.get("TMPDIR", "/tmp")
    try:
        for name in os.listdir(tmp):
            if name.startswith("brc"):
                shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
    except OSError:
        pass
