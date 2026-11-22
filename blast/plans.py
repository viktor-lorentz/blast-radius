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
_BUDGET_S = float(os.environ.get("BR_PLAN_BUDGET", "12"))
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


_TPCDS_INPUTS = "tests/fixtures/optimizer/tpc-ds/"
_TPCH_SQL = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"


def _inputs_unchanged(src: Path, text: str) -> bool:
    """True when the patch edits only fixtures the test never reads, plus tpc-h.sql
    in a form TestExecutor.setUpClass still loads (it reads that file and nothing
    else of tpc-h's text). The code, the TPC-DS fixtures and the test are pristine."""
    try:
        files = P.parse(text)
    except P.PatchError:
        return False
    if not files:
        return False
    for fp in files:
        path = str(fp.path)
        if fp.special or fp.old_path != fp.new_path or fp.old_path is None:
            return False
        if not path.startswith("tests/fixtures/") or path.startswith(_TPCDS_INPUTS):
            return False
        if path == _TPCH_SQL and not _loads(src, fp):
            return False
    return True


def _loads(src: Path, fp) -> bool:
    import importlib.util
    try:
        new = P.apply((src / _TPCH_SQL).read_text(encoding="utf-8"), fp)
        spec = importlib.util.spec_from_file_location("_br_helpers", src / "tests" / "helpers.py")
        helpers = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helpers)  # imports only os
        fd, path = tempfile.mkstemp(prefix="brplan", suffix=".sql", dir=os.environ.get("TMPDIR", "/tmp"))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new)
            pairs = list(helpers.load_sql_fixture_pairs(path))  # absolute path wins in os.path.join
        finally:
            os.unlink(path)
        return bool(pairs)
    except Exception:
        return False


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
                 budget_s: float = _BUDGET_S, min_marginal_s: float = _MIN_MARGINAL_S,
                 golden_path: Path | None = None) -> dict:
    """Return {commit: numbers without tpcds} for commits whose plans match.

    `rows` is (commit, patch_text, numbers). Anything not returned is unchanged.
    Stops after `budget_s` of process CPU. Highest marginal cost first.
    """
    src = Path(src).resolve()
    number = id_to_number.get(TPCDS_ID)
    if number is None:
        return {}
    ranked = []
    updates = {}
    for stem, text, nums in rows:
        if number not in nums:
            continue
        if os.environ.get("BR_PLAN_INPUTS", "1") != "0" and _inputs_unchanged(Path(src), text):
            updates[stem] = set(nums) - {number}
            continue
        marginal = _cost(nums, ms) - _cost(nums - {number}, ms)
        if marginal < min_marginal_s:
            continue
        patched = _safe_patch(Path(src), text)
        if patched is None:
            continue
        ranked.append((marginal, stem, patched, nums))
    if not ranked:
        return updates
    ranked.sort(reverse=True)
    import resource

    def cpu() -> float:
        self_use = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        return self_use.ru_utime + self_use.ru_stime + kids.ru_utime + kids.ru_stime

    start = cpu()
    rows_golden = None
    if golden_path is not None:  # built from the pinned tree in the image (tools/plan_golden.py)
        try:
            rows_golden = json.loads(Path(golden_path).read_text()).get("rows")
        except (OSError, ValueError):
            rows_golden = None
    if not rows_golden:
        golden = _run(Path(src), {}, None)
        rows_golden = golden.get("rows") if golden.get("ok") else None
    if not rows_golden:
        return updates
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

        max_q = int(os.environ.get("BR_PLAN_MAXQ", "8")) if golden else 0
        rows, changed = [], []
        for meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
            if not meta.get("execute"):
                parse_one(sql)
                continue
            rendered = transpile(sql, write="duckdb")
            tree = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
            row = [hashlib.blake2b("|".join(rendered).encode(), digest_size=8).hexdigest(), _digest(tree)]
            if golden:
                if len(rows) >= len(golden) or row != golden[len(rows)]:
                    changed.append(sql)
                    if len(changed) > max_q:
                        Path(out_path).write_text(json.dumps({"ok": True, "same": False}))
                        return
            rows.append(row)
        if golden:
            same = len(rows) == len(golden)
            if same and changed:
                same = _execute_changed(changed)
            result = {"ok": True, "same": same, "changed": len(changed)}
        else:
            result = {"ok": True, "rows": rows}
    except Exception as exc:  # a plan we cannot build might be the failure: keep the test
        result = {"ok": False, "err": type(exc).__name__}
    Path(out_path).write_text(json.dumps(result))


def _execute_changed(sqls: list) -> bool:
    """Run each changed query the way test_execute_tpcds does, under the patched tree:
    DuckDB on the rendered SQL is the expected frame, the Python executor the actual."""
    import ast
    import csv

    import duckdb
    import numpy as np
    import pandas as pd
    from pandas.testing import assert_frame_equal

    from sqlglot import find_tables, parse_one, transpile
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    from tests.helpers import FIXTURES_DIR, TPCDS_SCHEMA
    from tests.test_executor import open_file

    directory = FIXTURES_DIR + "/optimizer/tpc-ds/"
    conn = duckdb.connect()
    tables: dict = {}

    def setup(table: str) -> None:  # TestExecutor.setUpClass, one table
        columns = TPCDS_SCHEMA[table]
        file_name = f"{directory}{table}.csv.gz"
        conn.execute(
            f"""
                CREATE VIEW {table} AS
                SELECT *
                FROM READ_CSV('{file_name}', delim='|', header=True, columns={columns})
                """
        )
        reader = csv.reader(open_file(file_name), delimiter="|")
        rows, ctypes = [], []
        next(reader)
        for row in reader:
            if not ctypes:
                for v in row:
                    try:
                        ctypes.append(type(ast.literal_eval(v)))
                    except (ValueError, SyntaxError):
                        ctypes.append(str)
            rows.append(tuple(None if (t is not str and v == "") else t(v) for t, v in zip(ctypes, row)))
        tables[table] = Table(columns=columns, rows=rows)

    try:
        for sql in sqls:
            expression = parse_one(sql)
            needed = {t.name: None for t in find_tables(expression)}
            for name in needed:
                if name not in tables:
                    setup(name)
            table = execute(expression, schema=TPCDS_SCHEMA, tables={n: tables[n] for n in needed})
            a = conn.execute(transpile(sql, write="duckdb")[0]).fetchdf()
            b = pd.DataFrame(((np.nan if c is None else c for c in r) for r in table.rows), columns=table.columns)
            assert_frame_equal(a, b, check_dtype=False, check_index_type=False)
        return True
    except Exception:
        return False
    finally:
        conn.close()


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _, _, src, patched_path, golden_path, out_path = sys.argv
        _child(src, patched_path, golden_path, out_path)
        return
    raise SystemExit("run via blast.plans.narrow_tpcds")


if __name__ == "__main__":
    main()
