"""Drop test_execute_tpcds when the patched plans still return DuckDB's frames.

Identical plans already prove the test passes, as long as the patch stays out of
the executor. When only a few executed queries change, those queries are run
and the test is dropped only if every frame matches. A timeout, a crash, or a
wider diff keeps the test. This never adds a test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blast.plans import TPCDS_ID, _cost, _safe_patch

_BUDGET_S = 20.0
_MIN_MARGINAL_S = 4.0
_MAX_DIFF = 4
_COMMIT_TIMEOUT_S = 15.0


def narrow(src: Path, rows: list, id_to_number: dict, ms: dict,
           budget_s: float = _BUDGET_S, min_marginal_s: float = _MIN_MARGINAL_S,
           max_diff: int = _MAX_DIFF) -> dict:
    """Return {commit: numbers without tpcds} for commits whose frames match."""
    src = Path(src).resolve()
    number = id_to_number.get(TPCDS_ID)
    if number is None:
        return {}
    ranked = []
    for stem, text, nums in rows:
        if number not in nums:
            continue
        marginal = _cost(nums, ms) - _cost(nums - {number}, ms)
        if marginal < min_marginal_s:
            continue
        patched = _safe_patch(src, text)
        if patched is None:
            continue
        ranked.append((marginal, stem, patched, nums))
    if not ranked:
        return {}
    ranked.sort(reverse=True)
    import resource

    def cpu() -> float:
        self_use = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        return self_use.ru_utime + self_use.ru_stime + kids.ru_utime + kids.ru_stime

    start = cpu()
    golden = _run(src, {}, None, max_diff)
    rows_golden = golden.get("rows") if golden.get("ok") else None
    if not rows_golden:
        return {}
    updates = {}
    for _marginal, stem, patched, nums in ranked:
        if cpu() - start >= budget_s:
            break
        got = _run(src, patched, rows_golden, max_diff)
        if got.get("ok") and got.get("drop"):
            updates[stem] = set(nums) - {number}
    return updates


def _run(src: Path, patched: dict, golden: list | None, max_diff: int) -> dict:
    src = Path(src).resolve()
    tmp = os.environ.get("TMPDIR", "/tmp")
    paths = []
    try:
        for obj in (patched, golden if golden is not None else []):
            fd, path = tempfile.mkstemp(prefix="brheavy", suffix=".json", dir=tmp)
            with os.fdopen(fd, "w") as handle:
                json.dump(obj, handle)
            paths.append(path)
        fd, outp = tempfile.mkstemp(prefix="brheavy", suffix=".out", dir=tmp)
        os.close(fd)
        paths.append(outp)
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0", SKIP_INTEGRATION="0")
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "blast.heavy", "--child", str(src), paths[0], paths[1], outp, str(max_diff)],
            env=env,
            cwd=str(src),
            capture_output=True,
            text=True,
            timeout=_COMMIT_TIMEOUT_S,
        )
        if proc.returncode != 0 or not os.path.exists(outp) or os.path.getsize(outp) == 0:
            return {"ok": False}
        return json.loads(Path(outp).read_text())
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {"ok": False}
    finally:
        for path in paths:
            try:
                os.unlink(path)
            except OSError:
                pass


def _frames(src: str, patched: dict, golden: list, max_diff: int) -> dict:
    sys.path.insert(0, src)
    os.chdir(src)
    from blast.fingerprint import _PatchedFinder

    if patched:
        sys.meta_path.insert(0, _PatchedFinder(src, patched))
    from blast.plans import _digest
    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    full = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    got = []
    exec_at = []
    for i, (meta, sql, _expected) in enumerate(full):
        if not meta.get("execute"):
            parse_one(sql)
            continue
        rendered = transpile(sql, write="duckdb")
        tree = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
        got.append([__import__("hashlib").blake2b("|".join(rendered).encode(), digest_size=8).hexdigest(), _digest(tree)])
        exec_at.append(i)
    if not golden:
        return {"ok": True, "rows": got}
    if len(got) != len(golden):
        return {"ok": True, "drop": False}
    differ = [j for j, (row, want) in enumerate(zip(got, golden)) if row != want]
    if not differ:
        return {"ok": True, "drop": True}
    if len(differ) > max_diff:
        return {"ok": True, "drop": False}
    from tests.test_executor import TestExecutor

    TestExecutor.setUpClass()
    case = TestExecutor()
    for j in differ:
        meta, sql, _expected = full[exec_at[j]]
        expression = parse_one(sql)
        tables = {table.name: case.tpcds_tables[table.name] for table in find_tables(expression)}
        case.subtestHelper(exec_at[j], execute(expression, schema=TPCDS_SCHEMA, tables=tables), tpch=False)
    return {"ok": True, "drop": True}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _, _, src, patched_path, golden_path, out_path, max_diff = sys.argv
        patched = json.loads(Path(patched_path).read_text())
        golden = json.loads(Path(golden_path).read_text())
        try:
            result = _frames(src, patched, golden, int(max_diff))
        except Exception:
            result = {"ok": False}
        Path(out_path).write_text(json.dumps(result))
        return
    raise SystemExit("run via blast.heavy.narrow")


if __name__ == "__main__":
    main()
