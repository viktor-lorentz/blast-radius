"""Run a small static selection under the patched tree and drop the tests that pass.

A commit whose tests all pass scores 0. One kept test still pays the 2s worker fee, so the
win is a selection of at most 10 tests whose recorded time is under 0.8s and whose scheduled
cost is at least a second higher. Larger sets, including anything that records the TPC-DS or
parser stress tests, are not run: they cost more CPU than they can remove. A crash, a timeout,
or a failure keeps the test. This never adds a test the static selector did not already choose.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Iterable, List, Set

from blast.fingerprint import _PatchedFinder
from blast.split import split

# Hard cap for one commit and for the whole batch. The scorer allows 180s (270s in the final).
# One of these commits records under 0.8s, so a few seconds is enough and a hang cannot linger.
_COMMIT_CAP_S = 5.0
_BATCH_CAP_S = 90.0


def scheduled_cost_s(numbers: Iterable[int], ms: Dict[int, int]) -> float:
    workers = split(numbers, ms)
    if not workers:
        return 0.0
    return (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


def worth_running(numbers: Iterable[int], ms: Dict[int, int]) -> bool:
    """True when dropping the passing tests can remove the worker fee, cheaply.

    The previous gate ran every selection whose recorded time was under its
    scheduled cost. On the practice batch that spent 16s of CPU to remove 6s.
    These bounds are the selections where the fee dominates and the bodies are short.
    """
    nums = list(numbers)
    if not nums or len(nums) > 10:
        return False
    recorded = sum(ms[n] for n in nums) / 1000
    if recorded >= 0.8:
        return False
    return scheduled_cost_s(nums, ms) >= recorded + 1.0


def shrink(
    src: Path,
    patched: Dict[str, str],
    numbers: Set[int],
    id_of: Dict[int, str],
    ms: Dict[int, int],
    deadline: float,
) -> Set[int]:
    """Return the subset of `numbers` to keep. Passing tests are removed."""
    if not numbers or not worth_running(numbers, ms):
        return numbers
    if time.monotonic() > deadline:
        return numbers
    keep_ids = _run(src, patched, [id_of[n] for n in numbers], deadline)
    if keep_ids is None:
        return numbers
    by_id = {v: k for k, v in id_of.items()}
    return {by_id[i] for i in keep_ids if i in by_id}


def _run(src: Path, patched: Dict[str, str], test_ids: List[str], deadline: float) -> List[str] | None:
    budget = min(_COMMIT_CAP_S, max(1.0, deadline - time.monotonic()))
    tmp = os.environ.get("TMPDIR", "/tmp")
    paths = []
    try:
        for obj in (patched, test_ids):
            fd, path = tempfile.mkstemp(prefix="brrun", suffix=".json", dir=tmp)
            with os.fdopen(fd, "w") as f:
                json.dump(obj, f)
            paths.append(path)
        fd, outp = tempfile.mkstemp(prefix="brrun", suffix=".out", dir=tmp)
        os.close(fd)
        paths.append(outp)
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "blast.runner", "--child", str(src), paths[0], paths[1], outp, str(budget)],
            env=env,
            cwd=str(src),
            capture_output=True,
            text=True,
            timeout=budget + 5,
        )
        if proc.returncode != 0 or not os.path.exists(outp) or os.path.getsize(outp) == 0:
            return None
        data = json.loads(Path(outp).read_text())
        return list(data["keep"])
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


def _child(src: str, patched_path: str, ids_path: str, out_path: str, budget: float) -> None:
    import importlib
    import unittest

    deadline = time.monotonic() + budget
    patched = json.loads(Path(patched_path).read_text())
    test_ids = json.loads(Path(ids_path).read_text())
    sys.path.insert(0, src)
    # Serve patched modules from memory; everything else imports from the pristine tree.
    sys.meta_path.insert(0, _PatchedFinder(src, patched))
    os.chdir(src)
    keep: List[str] = []
    loader = unittest.defaultTestLoader
    for tid in test_ids:
        if time.monotonic() > deadline:
            keep.append(tid)
            continue
        try:
            rel, cls, meth = tid.split("::")
            mod = importlib.import_module(rel[:-3].replace("/", "."))
            suite = loader.loadTestsFromName(f"{cls}.{meth}", mod)
            result = unittest.TestResult()
            suite.run(result)
            if result.failures or result.errors or result.testsRun == 0:
                keep.append(tid)
        except Exception:
            keep.append(tid)
    Path(out_path).write_text(json.dumps({"keep": keep}))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _, _, src, patched_path, ids_path, out_path, budget = sys.argv
        _child(src, patched_path, ids_path, out_path, float(budget))
        return
    raise SystemExit("run via blast.runner.shrink")


if __name__ == "__main__":
    main()
