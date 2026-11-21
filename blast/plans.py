"""Drop test_execute_tpcds when the patch cannot change its result.

The test parses every TPC-DS query, renders the executed ones through the DuckDB
dialect (that SQL is what DuckDB runs as the expected frame), and executes the
optimized tree. When those two artifacts match the pristine tree and the patch
does not touch the executor, the frames match and the test passes.

A crash, a timeout, or a changed plan keeps the test. This never adds a test.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blast import patch as P
from blast.split import split

TPCDS_ID = "tests/test_executor.py::TestExecutor::test_execute_tpcds"

# Modules the executor still runs after the optimized tree and the DuckDB SQL
# are fixed. A change in one of these can fail the test with the plans identical.
_GUARD_EXACT = frozenset({
    "sqlglot/planner.py",
    "sqlglot/serde.py",
    "sqlglot/generators/python.py",
    "sqlglot/optimizer/scope.py",
    "sqlglot/optimizer/eliminate_joins.py",
    "sqlglot/helper.py",
    "sqlglot/schema.py",
    "sqlglot/tokens.py",
    "sqlglot/dialects/dialect.py",
})
_GUARD_PREFIXES = ("sqlglot/executor/", "sqlglot/expressions/")

# Probing a commit whose makespan barely moves is not worth the CPU.
_MIN_MARGINAL_S = 4.0
_BUDGET_S = 12.0
_COMMIT_TIMEOUT_S = 25


def _cost(nums, ms) -> float:
    workers = split(nums, ms)
    if not workers:
        return 0.0
    return (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


def _safe_patch(src: Path, text: str) -> dict | None:
    """Patched sqlglot sources, or None when the plan check would not be sound."""
    try:
        files = P.parse(text)
    except P.PatchError:
        return None
    if not files:
        return None
    out = {}
    for fp in files:
        path = str(fp.path)
        if fp.special or fp.old_path != fp.new_path or not path.endswith(".py"):
            return None
        if not path.startswith("sqlglot/"):
            return None
        if path in _GUARD_EXACT or path.startswith(_GUARD_PREFIXES):
            return None
        try:
            out[path] = P.apply((src / path).read_text(), fp)
        except (P.PatchError, OSError, UnicodeError):
            return None
    return out or None


def _run(src: Path, patched: dict, golden: list | None) -> dict:
    src = Path(src).resolve()
    tmp = os.environ.get("TMPDIR", "/tmp")
    paths = []
    try:
        for obj in (patched, golden if golden is not None else []):
            fd, path = tempfile.mkstemp(prefix="brplan", suffix=".json", dir=tmp)
            with os.fdopen(fd, "w") as handle:
                json.dump(obj, handle)
            paths.append(path)
        fd, outp = tempfile.mkstemp(prefix="brplan", suffix=".out", dir=tmp)
        os.close(fd)
        paths.append(outp)
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "blast.plans", "--child", str(src), paths[0], paths[1], outp],
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


def narrow_tpcds(src: Path, rows: list, id_to_number: dict, ms: dict,
                 budget_s: float = _BUDGET_S, min_marginal_s: float = _MIN_MARGINAL_S) -> dict:
    """Return {commit: numbers without tpcds} for commits whose plans match.

    `rows` is (commit, patch_text, numbers). Anything not returned is unchanged.
    Stops after `budget_s` of process CPU. Highest marginal cost first.
    """
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
        patched = _safe_patch(Path(src), text)
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
    golden = _run(Path(src), {}, None)
    rows_golden = golden.get("rows") if golden.get("ok") else None
    if not rows_golden:
        return {}
    updates = {}
    for _marginal, stem, patched, nums in ranked:
        if cpu() - start >= budget_s:
            break
        got = _run(Path(src), patched, rows_golden)
        if got.get("ok") and got.get("same"):
            updates[stem] = set(nums) - {number}
    return updates


def _digest(expr) -> str:
    from sqlglot import exp
    hasher = hashlib.blake2b(digest_size=8)
    stack = [expr]
    while stack:
        node = stack.pop()
        if not isinstance(node, exp.Expression):
            hasher.update(repr(node).encode())
            continue
        hasher.update(type(node).__name__.encode())
        kind = node.type
        if kind is not None:
            hasher.update(str(kind).encode())
        for key in sorted(node.args):
            value = node.args[key]
            if value is None or value is False or value == [] or value == ():
                continue
            hasher.update(key.encode())
            if isinstance(value, list):
                stack.extend(reversed(value))
            else:
                stack.append(value)
    return hasher.hexdigest()


def _child(src: str, patched_path: str, golden_path: str, out_path: str) -> None:
    patched = json.loads(Path(patched_path).read_text())
    golden = json.loads(Path(golden_path).read_text())
    sys.path.insert(0, src)
    os.chdir(src)
    from blast.fingerprint import _PatchedFinder
    sys.meta_path.insert(0, _PatchedFinder(src, patched))
    result: dict = {"ok": False}
    try:
        from sqlglot import parse_one, transpile
        from sqlglot.optimizer import optimize
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

        rows = []
        for meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
            if not meta.get("execute"):
                parse_one(sql)
                continue
            rendered = transpile(sql, write="duckdb")
            tree = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
            row = [hashlib.blake2b("|".join(rendered).encode(), digest_size=8).hexdigest(), _digest(tree)]
            if golden:
                want = golden[len(rows)]
                if row != want:
                    result = {"ok": True, "same": False}
                    Path(out_path).write_text(json.dumps(result))
                    return
            rows.append(row)
        if golden:
            result = {"ok": True, "same": len(rows) == len(golden)}
        else:
            result = {"ok": True, "rows": rows}
    except Exception as exc:  # a plan we cannot build might be the failure: keep the test
        result = {"ok": False, "err": type(exc).__name__}
    Path(out_path).write_text(json.dumps(result))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _, _, src, patched_path, golden_path, out_path = sys.argv
        _child(src, patched_path, golden_path, out_path)
        return
    raise SystemExit("run via blast.plans.narrow_tpcds")


if __name__ == "__main__":
    main()
