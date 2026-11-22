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
_BUDGET_S = float(os.environ.get("BR_PLAN_BUDGET", "20"))
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
        if _unread(src, fp):  # a test-side file test_execute_tpcds never reads
            continue
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
    return all(_unread(src, fp) for fp in files)


# What test_execute_tpcds reads under tests/: its own module, helpers.py, package
# and pytest hooks, and the TPC fixtures. Other test modules are never imported.
_TEST_READS = frozenset({"tests/test_executor.py", "tests/helpers.py"})


def _unread(src: Path, fp) -> bool:
    """True when this one file edit cannot reach test_execute_tpcds: a fixture
    outside tpc-ds/ (tpc-h.sql only in a form setUpClass still loads), or another
    test module. Both sides of a rename must qualify."""
    if fp.special:
        return False
    for path in {str(fp.old_path or ""), str(fp.new_path or "")} - {""}:
        name = path.rsplit("/", 1)[-1]
        if not path.startswith("tests/") or path in _TEST_READS:
            return False
        if name in ("__init__.py", "conftest.py") or path.startswith(_TPCDS_INPUTS):
            return False
        if path.startswith("tests/fixtures/"):
            if fp.old_path != fp.new_path or fp.old_path is None:
                return False
            if path == _TPCH_SQL and not _loads(src, fp):
                return False
            if path.startswith("tests/fixtures/optimizer/tpc-h/") and path != _TPCH_SQL:
                return False
        elif not path.endswith(".py"):
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


def _run(src: Path, patched: dict, golden: list | None, only: set | None = None) -> dict:
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
        env.pop("BR_PLAN_ONLY", None)
        if only is not None:
            env["BR_PLAN_ONLY"] = ",".join(str(i) for i in sorted(only))
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
    cover = None
    if golden_path is not None and os.environ.get("BR_PLAN_COVER", "1") != "0":
        try:
            cover = load_cover(Path(golden_path).with_name("tpcds_cover.json"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            cover = None
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
        only = _affected(Path(src), text, cover) if cover is not None else None
        if only is not None and not only:
            updates[stem] = set(nums) - {number}
            continue
        ranked.append((marginal, stem, patched, nums, only))
    if not ranked:
        return updates
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
    ranked.sort(key=lambda r: r[:2], reverse=True)
    for _marginal, stem, patched, nums, only in ranked:
        if cpu() - start >= budget_s:
            break
        got = _run(Path(src), patched, rows_golden, only)
        if got.get("ok") and got.get("same"):
            updates[stem] = set(nums) - {number}
    return updates


def _changed_lines(fp, n_old: int) -> tuple:
    """(old lines a patch touches: deleted lines and both neighbours of an insertion,
    new lines it adds), 1-based."""
    old_out, new_out = set(), set()
    for h in fp.hunks:
        line = h.old_start if h.old_len else h.old_start + 1  # next old line
        nline = h.new_start if h.new_len else h.new_start + 1  # next new line
        for raw in h.lines:
            tag = raw[:1]
            if tag == "-":
                old_out.add(line)
                line += 1
            elif tag == "+":
                old_out.update((line - 1, line))
                new_out.add(nline)
                nline += 1
            else:
                line += 1
                nline += 1
    return {n for n in old_out if 1 <= n <= max(n_old, 1)} or {1}, new_out


def _owners(tree) -> list:
    """(body first line, body last line, code first line, qualified name) of every def."""
    import ast
    out = []

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + (child.name,)
                if not isinstance(child, ast.ClassDef) and child.body:
                    first = min([child.lineno] + [d.lineno for d in child.decorator_list])
                    out.append((max(child.lineno + 1, child.body[0].lineno), child.end_lineno, first, name))
                walk(child, name)
            else:
                walk(child, prefix)

    walk(tree, ())
    return out


def _owner(owners, line):
    inner = [o for o in owners if o[0] <= line <= o[1]]
    return max(inner, key=lambda o: o[2]) if inner else None


def _edit_keys(src: Path, text: str):
    """{module: first lines of the functions whose bodies the patch edits}, or None
    when an edit touches a module, class or def-header line, a renamed or ambiguous
    function, or the patched module no longer compiles."""
    import ast
    import warnings
    try:
        files = P.parse(text)
    except P.PatchError:
        return None
    out: dict = {}
    for fp in files:
        if _unread(src, fp):
            continue
        path = str(fp.path)
        try:
            old = (src / path).read_text()
            new = P.apply(old, fp)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                old_owners = _owners(ast.parse(old))
                new_owners = _owners(compile(new, path, "exec", ast.PyCF_ONLY_AST))
                compile(new, path, "exec")  # symbol-table errors only show here
        except (OSError, UnicodeError, SyntaxError, ValueError, P.PatchError):
            return None
        keys = out.setdefault(path, set())
        by_name: dict = {}
        for o in old_owners:
            by_name.setdefault(o[3], []).append(o[2])
        old_lines, new_lines = _changed_lines(fp, old.count("\n") + 1)
        for line in old_lines:
            o = _owner(old_owners, line)
            if o is None:
                return None
            keys.add(o[2])
        for line in new_lines:
            o = _owner(new_owners, line)
            if o is None or o[3] not in by_name:
                return None
            keys.update(by_name[o[3]])
    return out or None


def _affected(src: Path, text: str, cover):
    """TPC-DS query indices whose run in the pristine tree called a function the patch
    edits, or None when the edit may reach every query (import-time code, a function
    whose callers depend on query order, i.e. caches). An empty set means the patch cannot change any plan or executor run."""
    keys = _edit_keys(src, text)
    if keys is None:
        return None
    modules, eager, per_query = cover
    out: set = set()
    for path, lines in keys.items():
        if path not in modules:
            # Neither the recording nor the digest child ever imports it, and nothing
            # the patch leaves untouched can import it: the full check would only
            # confirm "same". Its own compile was checked above.
            continue
        hit = per_query.get(path, {})
        for line in lines:
            if line in eager.get(path, ()):
                return None
            out.update(hit.get(line, ()))
    return out


def _uncovered(src: Path, text: str, cover) -> bool:
    return _affected(src, text, cover) == set()


def _cover_child(src: str, out_path: str, order: str) -> None:
    """Build time: which sqlglot functions run on import, and on each TPC-DS query's
    plans and executor run over empty tables, in the pristine tree."""
    sys.path.insert(0, src)
    os.chdir(src)
    root = os.path.realpath(src) + os.sep
    seen: dict = {}  # path -> {(first line, name): set of query indices, -1 = import}
    state = {"q": -1}

    def start(code, _offset):
        name = os.path.realpath(code.co_filename)
        if name.startswith(root):
            seen.setdefault(name[len(root):], {}).setdefault(
                (code.co_firstlineno, code.co_name), set()).add(state["q"])
        return sys.monitoring.DISABLE

    mon = sys.monitoring
    mon.use_tool_id(mon.PROFILER_ID, "brcover")
    mon.register_callback(mon.PROFILER_ID, mon.events.PY_START, start)
    mon.set_events(mon.PROFILER_ID, mon.events.PY_START)
    ok = False
    try:
        import sqlglot  # noqa: F401
        from sqlglot import parse_one, transpile
        from sqlglot.executor import execute
        from sqlglot.executor.table import Table
        from sqlglot.optimizer import optimize
        from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs
        try:
            import tests.test_executor  # noqa: F401
        except Exception:
            pass
        empty = {t: Table(columns=tuple(cols)) for t, cols in TPCDS_SCHEMA.items()}
        queries = [sql for meta, sql, _e in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")
                   if meta.get("execute")]
        order_ix = list(range(len(queries)))
        if order == "rev":
            order_ix.reverse()
        for i in order_ix:
            mon.restart_events()
            state["q"] = i
            sql = queries[i]
            transpile(sql, write="duckdb")
            optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
            try:
                execute(parse_one(sql), schema=TPCDS_SCHEMA, tables=empty)
            except Exception:
                pass
        ok = True
    finally:
        mon.set_events(mon.PROFILER_ID, 0)
        mon.free_tool_id(mon.PROFILER_ID)
    if ok:
        Path(out_path).write_text(json.dumps({
            path: [[line, name, sorted(qs)] for (line, name), qs in codes.items()]
            for path, codes in seen.items()
        }))


def merge_cover(fwd: dict, rev: dict) -> dict:
    """tpcds_cover.json from the forward and reverse recordings. A function whose set
    of calling queries depends on the order (a cache, a lazy table) or that runs on
    import counts as eager: an edit to it reaches every query."""
    modules, eager, per_query = set(), {}, {}
    for path in set(fwd) | set(rev):
        f = {(l, n): set(q) for l, n, q in fwd.get(path, [])}
        r = {(l, n): set(q) for l, n, q in rev.get(path, [])}
        for key in set(f) | set(r):
            line, name = key
            qf, qr = f.get(key, set()), r.get(key, set())
            if name == "<module>":
                modules.add(path)
            if -1 in qf or -1 in qr or qf != qr:
                eager.setdefault(path, set()).add(line)
            else:
                per_query.setdefault(path, {}).setdefault(line, set()).update(qf)
    for path, lines in eager.items():  # same first line, two code objects: be safe
        for line in lines:
            per_query.get(path, {}).pop(line, None)
    return {"modules": sorted(modules),
            "eager": {p: sorted(v) for p, v in eager.items()},
            "per_query": {p: {str(l): sorted(q) for l, q in v.items()} for p, v in per_query.items()}}


def load_cover(path: Path):
    raw = json.loads(Path(path).read_text())
    return (frozenset(raw["modules"]),
            {p: frozenset(v) for p, v in raw["eager"].items()},
            {p: {int(l): frozenset(q) for l, q in v.items()} for p, v in raw["per_query"].items()})


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

        max_q = int(os.environ.get("BR_PLAN_MAXQ", "20")) if golden else 0
        raw_only = os.environ.get("BR_PLAN_ONLY") if golden else None
        only = {int(i) for i in raw_only.split(",") if i} if raw_only is not None else None
        rows, changed = [], []
        for meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
            if not meta.get("execute"):
                if only is None:
                    parse_one(sql)
                continue
            if only is not None and len(rows) not in only:
                if len(rows) >= len(golden):
                    changed.append(sql)
                rows.append(golden[len(rows)] if len(rows) < len(golden) else None)
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

    import time

    cap = float(os.environ.get("BR_PLAN_QCPU", "1.7"))  # local CPU; ~3 s on the server
    t0 = time.process_time()
    try:
        for sql in sqls:
            if time.process_time() - t0 > cap:
                return False  # out of per-commit CPU: keep the test
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
    if len(sys.argv) > 1 and sys.argv[1] == "--cover":
        _cover_child(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    raise SystemExit("run via blast.plans.narrow_tpcds")


if __name__ == "__main__":
    main()
