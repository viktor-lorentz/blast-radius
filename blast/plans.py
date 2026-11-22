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
import warnings
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

# A matching plan digest is enough to drop the test only for this subset.
# executor/, planner.py, schema.py, serde.py, helper.py and generators/python.py
# fail with an identical digest, so the digest child does not run for them.
_DIGEST_EXACT = frozenset({
    "sqlglot/optimizer/scope.py",
    "sqlglot/optimizer/eliminate_joins.py",
    "sqlglot/dialects/dialect.py",
    "sqlglot/tokens.py",
})
_DIGEST_PREFIXES = ("sqlglot/expressions/",)

# Probing a commit whose makespan barely moves is not worth the CPU.
_MIN_MARGINAL_S = 4.0
_BUDGET_S = float(os.environ.get("BR_PLAN_BUDGET", "20"))
_COMMIT_TIMEOUT_S = 25
_MAXQ = int(os.environ.get("BR_PLAN_MAXQ", "20"))
_PHASES = os.environ.get("BR_PLAN_PHASES", "1") != "0"  # per-phase plan + optimizer snapshots
# First executed TPC-DS queries, only for an in-place sqlglot/planner.py edit.
# Its window is separate, so a planner commit does not spend another check's CPU.
_CANARY_N = int(os.environ.get("BR_PLAN_CANARY_N", "3"))
_CANARY_QCPU = float(os.environ.get("BR_PLAN_CANARY_QCPU", "3.5"))
# Phase rows the shared window does not reach. Small on purpose: a full second
# pass of every phase row added ~21s on c1, and the skipped guard pass is a
# handful of executed queries.
_PHASE_BUDGET_S = float(os.environ.get("BR_PLAN_PHASE_BUDGET", "4"))


def _digest_file(path: str) -> bool:
    return path in _DIGEST_EXACT or path.startswith(_DIGEST_PREFIXES)


def _subset_patch(src: Path, text: str) -> dict | None:
    """Patched sources when every file the test can read is in the digest subset.

    None if any other file is edited, so the digest is not paid on those commits.
    """
    try:
        files = P.parse(text)
    except P.PatchError:
        return None
    if not files:
        return None
    out = {}
    for fp in files:
        path = str(fp.path)
        if _unread(src, fp):
            continue
        if fp.special or fp.old_path != fp.new_path or not path.endswith(".py"):
            return None
        if not _digest_file(path):
            return None
        try:
            new = P.apply((src / path).read_text(), fp)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(new, path, "exec", dont_inherit=True)
        except (P.PatchError, OSError, UnicodeError, SyntaxError, ValueError):
            return None
        out[path] = new
    return out or None


def _planner_inplace(src: Path, text: str) -> dict | None:
    """Patched sqlglot/planner.py when that file is the only in-place edit.

    A rename, a delete, or any other file the test can read returns None.
    The short canary then does not run and cannot drop that commit.
    """
    try:
        files = P.parse(text)
    except P.PatchError:
        return None
    if not files:
        return None
    new = None
    for fp in files:
        if _unread(src, fp):
            continue
        path = str(fp.path)
        if fp.special or fp.old_path != fp.new_path or fp.old_path is None:
            return None
        if path != "sqlglot/planner.py":
            return None
        try:
            old = (src / path).read_text()
            got = P.apply(old, fp)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                compile(got, path, "exec", dont_inherit=True)
        except (P.PatchError, OSError, UnicodeError, SyntaxError, ValueError):
            return None
        if got == old:
            return None
        new = got
    if new is None:
        return None
    return {"sqlglot/planner.py": new}


def _is_phase(qs) -> bool:
    return isinstance(qs, tuple) and bool(qs) and qs[0] == "phase"


def _n_exec(row) -> int:
    qs = row[4]
    if not _is_phase(qs):
        return 99
    return sum(1 for e in qs[1] if e[5])


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
_TPCDS_SQL = "tests/fixtures/optimizer/tpc-ds/tpc-ds.sql"
_TPCH_SQL = "tests/fixtures/optimizer/tpc-h/tpc-h.sql"
# Line numbers of the expected (optimized) statement in each pair. The executor
# test parses the source statement only; this half is the optimizer's golden text.
_EXPECTED_LINES: set | None = None


def _expected_lines(src: Path) -> set:
    global _EXPECTED_LINES
    if _EXPECTED_LINES is not None:
        return _EXPECTED_LINES
    text = (src / _TPCDS_SQL).read_text(encoding="utf-8", errors="replace")
    stmts = []
    start = 1
    for i, raw in enumerate(text.splitlines(), 1):
        if ";" in raw:
            stmts.append((start, i))
            start = i + 1
    expected: set = set()
    for qi, (a, b) in enumerate(stmts):
        if qi % 2 == 1:
            expected.update(range(a, b + 1))
    _EXPECTED_LINES = expected
    return expected


def _expected_sql_only(src: Path, fp) -> bool:
    """True when a tpc-ds.sql edit cannot reach test_execute_tpcds.

    The test parses and runs the source statement of each pair. The following
    statement is the optimizer golden text, which that test never reads. A
    semicolon would re-pair the statements, so those hunks stay visible.
    """
    if fp.special or fp.old_path != fp.new_path or fp.old_path is None:
        return False
    try:
        expected = _expected_lines(src)
    except OSError:
        return False
    if not fp.hunks:
        return False
    for h in fp.hunks:
        old = h.old_start
        for raw in h.lines:
            tag = raw[:1]
            body = raw[1:]
            if tag == "-":
                if ";" in body or old not in expected:
                    return False
                old += 1
            elif tag == "+":
                if ";" in body or old not in expected:
                    return False
            elif tag == "\\":
                return False
            else:
                old += 1
    return True


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
        if path == _TPCDS_SQL and _expected_sql_only(src, fp):
            return True  # golden SQL only: test_execute_tpcds reads the source half
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


# Per-query reach (tools/plan_qmap.py, built from the pinned tree in the image): for
# each sqlglot function, the TPC-DS queries whose part of the test entered it. A patch
# whose hunks sit only inside functions reaches just the union of their queries; any
# hunk at module or class level, in an import-time function, or in setUpClass reaches
# every query. Files the test never imports at the pinned commit are not loaded.
_EMPTY = os.environ.get("BR_PLAN_EMPTY", "digest")  # digest | drop: reach is empty


def _load_qmap(path) -> dict | None:
    try:
        q = json.loads(Path(path).read_text())
        q["boot"] = set(q["boot"])
        q["tags"] = {k: set(v) for k, v in q["funcs"].items()}
        q["funcs"] = {k: {t if t == "S" else int(t.lstrip("PROpreUX").rpartition("_")[2]) for t in v}
                      for k, v in q["funcs"].items()}
        q["loaded"] = {k.rsplit(":", 1)[0] for k in q["boot"] | set(q["funcs"])}
        q["exec"] = set(q["exec"])
        q["_scopes"] = {}
        return q
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _key_of(scopes: list, rel: str, line: int):
    best = None
    for b0, end, first, defl in scopes:
        if b0 <= line <= end and line > defl and (best is None or b0 >= best[0]):
            best = (b0, first)
    return None if best is None else f"{rel}:{best[1]}"


def _reach(src: Path, rel: str, fp, qmap: dict):
    """Queries this file edit can reach, or None for every query."""
    import ast
    scopes = qmap["_scopes"].get(rel)
    if scopes is None:
        scopes = []
        for n in ast.walk(ast.parse((src / rel).read_text())):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                first = min([n.lineno] + [d.lineno for d in n.decorator_list])
                scopes.append((n.body[0].lineno, n.end_lineno, first, n.lineno))
        qmap["_scopes"][rel] = scopes
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
                if ins and not run_del:  # pure insertion between lines old-1 and old
                    a, b = _key_of(scopes, rel, old - 1), _key_of(scopes, rel, old)
                    keys.add(a if a == b else None)
                ins = run_del = False
                old += 1
    if None in keys or keys & qmap["boot"]:
        return None
    qs, tags, xonly = set(), set(), set()
    for k in keys:
        qs.update(qmap["funcs"].get(k, ()))
        kt = qmap["tags"].get(k, set())
        tags |= kt
        for t in kt:  # entered only while executing query q, not in its parse/render/optimize
            if t.startswith("X"):
                q = t[1:]
                if not any(u[1:] == q or u.endswith("_" + q) or u == "Opre" + q for u in kt if u[0] != "X"):
                    xonly.add(int(q))
    return None if "S" in qs else (qs, tags, xonly)


def _ast_same(old: str, new: str) -> bool:
    """True when the edit is comments or formatting only. Not local renames:
    code_norm is off, so a renamed local stays visible and the test is kept."""
    from blast import astdiff
    try:
        return astdiff.diff(old, new, code_norm=False) == []
    except Exception:
        return False


def _reach_plan(src: Path, text: str, qmap: dict):
    """(patched, queries, guard) for the plan check; queries None = every query.
    None when the patch cannot be checked soundly (keep the test)."""
    try:
        files = P.parse(text)
    except P.PatchError:
        return None
    if not files:
        return None
    out, qs, guard, every, freed = {}, set(), False, False, False
    tags, xonly = set(), set()
    for fp in files:
        path = str(fp.path)
        if _unread(src, fp):
            continue
        if fp.special or fp.old_path != fp.new_path or not path.endswith(".py"):
            return None
        if not path.startswith("sqlglot/"):
            return None
        if path not in qmap["loaded"]:
            continue  # never imported by the test at the pinned commit
        try:
            old = (src / path).read_text()
            new = P.apply(old, fp)
            compile(new, path, "exec", dont_inherit=True)
        except Exception:  # unparsable patch or source, syntax error: keep
            return None
        # A guard file whose AST did not change cannot fail the test, and must
        # not refuse the rest of the patch. The known identical-plan misses
        # (context.py, table.py, generators/python.py) all change the AST.
        if (path in _GUARD_EXACT or path.startswith(_GUARD_PREFIXES)) and _ast_same(old, new):
            freed = True  # do not let a comment refuse the rest of the patch
            continue
        try:
            r = _reach(src, path, fp, qmap)
        except Exception:
            return None
        out[path] = new
        guard = guard or path in _GUARD_EXACT or path.startswith(_GUARD_PREFIXES)
        if r is None:
            every = True
        else:
            qs |= r[0]
            tags |= r[1]
            xonly |= r[2]
    if every:
        return None if guard else (out, None, False, None, freed)
    return out, qs, guard, (tags, xonly), freed


def _phase_plan(qs: set, phase, qmap: dict, guard: bool) -> list:
    """Per reached query [qi, exec index or -1, parse?, render?, first optimizer rule
    to rerun (0 = all, None = none), execute?]. A query whose parse, optimize()
    prologue/loop or snapshot unpickling enters an edited function reruns optimize
    from the raw parse; otherwise from the snapshot before the first edited rule."""
    tags, xonly = phase
    eidx = {q: i for i, q in enumerate(qmap["exec_order"])}
    nrules = len(qmap.get("rules", ()))
    plan = []
    for q in sorted(qs):
        parse = f"P{q}" in tags
        if q not in eidx:
            plan.append([q, -1, parse, False, None, False])
            continue
        if parse or f"Opre{q}" in tags or f"U{q}" in tags:
            start = 0
        else:
            hit = [r for r in range(nrules) if f"O{r}_{q}" in tags]
            start = min(hit) if hit else None
        plan.append([q, eidx[q], parse, parse or f"R{q}" in tags, start, guard or q in xonly])
    return plan


def _run(src: Path, patched: dict, golden) -> dict:
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
    digest_queued = 0
    planner_cands = []
    qmap = None
    if golden_path is not None and os.environ.get("BR_PLAN_REACH", "1") != "0":
        qmap = _load_qmap(Path(golden_path).with_name("tpcds_qmap.json"))
        if qmap is not None:
            qmap["exec_order"] = sorted(qmap["exec"])
    for stem, text, nums in rows:
        if number not in nums:
            continue
        if os.environ.get("BR_PLAN_INPUTS", "1") != "0" and _inputs_unchanged(Path(src), text):
            updates[stem] = set(nums) - {number}
            continue
        marginal = _cost(nums, ms) - _cost(nums - {number}, ms)
        if marginal < min_marginal_s:
            continue
        if os.environ.get("BR_PLAN_CANARY", "1") != "0" and _CANARY_N > 0:
            planner = _planner_inplace(Path(src), text)
            if planner:
                planner_cands.append((marginal, stem, planner, nums))
        if qmap is not None:
            plan = _reach_plan(src, text, qmap)
            queued = False
            if plan is not None:
                patched, qs, guard, phase, freed = plan
                if qs is not None and not qs and _EMPTY == "drop":
                    updates[stem] = set(nums) - {number}  # no query reaches an edited function
                    continue
                if qs is not None and not qs:  # BR_PLAN_EMPTY=digest: verify the plans anyway
                    if guard:
                        plan = None  # subset digest below, when every file qualifies
                    else:
                        qs = None
                if plan is not None:
                    phased = qs is not None and _PHASES and "snap_rules" in qmap
                    if phased:
                        plan_q = _phase_plan(qs, phase, qmap, guard)
                        if sum(1 for e in plan_q if e[5]) > _MAXQ:
                            plan = None  # too many to execute: subset digest, or keep
                        else:
                            qs = ("phase", plan_q)
                    elif qs is not None and len(qs) > _MAXQ:
                        if guard:
                            plan = None
                        else:
                            qs = None
                if plan is not None:
                    if not patched:
                        updates[stem] = set(nums) - {number}  # only files the test never loads
                        continue
                    ranked.append((marginal, stem, patched, nums, qs, guard, freed))
                    queued = True
            if not queued:
                sub = _subset_patch(Path(src), text)
                if sub:
                    ranked.append((marginal, stem, sub, nums, "digest", False, False))
                    digest_queued += 1
            continue
        patched = _safe_patch(Path(src), text)
        if patched is None:
            sub = _subset_patch(Path(src), text)
            if sub:
                ranked.append((marginal, stem, sub, nums, "digest", False, False))
                digest_queued += 1
            continue
        ranked.append((marginal, stem, patched, nums, None, False, False))
    ranked_stems = {r[1] for r in ranked}
    # Planner commits the plan check already queued keep that proof. The canary
    # is only the refused in-place planner.py edits, in their own window.
    canaries = [
        (m, stem, patched, nums, "canary", False, False)
        for m, stem, patched, nums in planner_cands
        if stem not in ranked_stems and stem not in updates
    ]
    if not ranked and not canaries:
        return updates
    ranked.sort(key=lambda r: r[:2], reverse=True)
    canaries.sort(key=lambda r: r[:2], reverse=True)
    # The digest must not share the main budget: on c1 the higher-marginal
    # checks spend it, and the subset commits (the only ones that may be
    # digested) never run. A second window spends CPU only on that subset.
    # A comment in a guard file is a third window, so proving the other files
    # harmless does not push an existing drop past the main budget.
    # Phase rows stay in that main window. The ones it never reaches get a
    # fourth window, so a skipped helper.py pass does not take their budget.
    # Fewest executions first, guard files before the rest. The planner canary
    # is a fifth window: only in-place planner.py, so other commits pay nothing.
    plain = [r for r in ranked if r[4] != "digest" and not r[6]]
    phases = [r for r in ranked if _is_phase(r[4]) and not r[6]]
    freed_rows = [r for r in ranked if r[6]]
    digests = [r for r in ranked if r[4] == "digest"]
    import resource

    def cpu() -> float:
        self_use = resource.getrusage(resource.RUSAGE_SELF)
        kids = resource.getrusage(resource.RUSAGE_CHILDREN)
        return self_use.ru_utime + self_use.ru_stime + kids.ru_utime + kids.ru_stime

    digest_ran = digest_hit = 0
    phase_ran = phase_hit = canary_ran = canary_hit = 0
    attempted: set = set()
    rows_golden = None
    if ranked and golden_path is not None:  # built from the pinned tree in the image (tools/plan_golden.py)
        try:
            rows_golden = json.loads(Path(golden_path).read_text()).get("rows")
        except (OSError, ValueError):
            rows_golden = None
    if ranked and not rows_golden:
        golden = _run(Path(src), {}, None)
        rows_golden = golden.get("rows") if golden.get("ok") else None

    def _check(batch, budget: float = budget_s) -> None:
        nonlocal digest_ran, digest_hit, phase_ran, phase_hit, canary_ran, canary_hit
        start = cpu()
        for _marginal, stem, patched, nums, qs, guard, _freed in batch:
            if cpu() - start >= budget:
                break
            attempted.add(stem)
            if qs == "canary":
                spec = {"canary": _CANARY_N}
                label = "CAN%d" % _CANARY_N
                canary_ran += 1
            elif qs == "digest":
                spec = {"rows": rows_golden, "digest_only": True}
                label = "DIGEST"
                digest_ran += 1
            elif isinstance(qs, tuple):
                spec = {"rows": rows_golden, "plan": qs[1], "snapdir": str(Path(golden_path).parent),
                        "rules": qmap["snap_rules"]}
                label = "PH%d/%s" % (len(qs[1]), ",".join(sorted({str(e[4]) for e in qs[1] if e[1] >= 0})))
                phase_ran += 1
            else:
                spec = rows_golden if qs is None else {"rows": rows_golden, "only": sorted(qs), "all": guard}
                label = "ALL" if qs is None else len(qs)
            got = _run(Path(src), patched, spec)
            if os.environ.get("BR_PLAN_DEBUG"):
                print(f"plan {stem} q={label} guard={guard} -> {got}", file=sys.stderr)
            if got.get("ok") and got.get("same"):
                updates[stem] = set(nums) - {number}
                if qs == "digest":
                    digest_hit += 1
                elif qs == "canary":
                    canary_hit += 1
                elif isinstance(qs, tuple):
                    phase_hit += 1

    overflow = []
    overflow_stats = (0, 0, 0)
    if ranked and rows_golden:
        _check(plain)
        overflow = [r for r in phases if r[1] not in attempted and r[1] not in updates]
        overflow.sort(key=lambda r: (0 if r[5] else 1, _n_exec(r), -r[0], r[1]))
        phase_ran = phase_hit = 0
        _check(overflow, _PHASE_BUDGET_S)
        overflow_stats = (phase_hit, phase_ran, len(overflow))
        _check(freed_rows)
        _check(digests)
    if canaries:
        _check(canaries)
    if digest_queued:
        print(f"tpcds subset-digest {digest_hit}/{digest_ran} queued {digest_queued}", file=sys.stderr)
    if overflow_stats[2]:
        print(
            f"tpcds phase-window {overflow_stats[0]}/{overflow_stats[1]} queued {overflow_stats[2]}",
            file=sys.stderr,
        )
    if canaries:
        print(f"tpcds planner-canary {canary_hit}/{canary_ran} queued {len(canaries)}", file=sys.stderr)
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

        if isinstance(golden, dict) and golden.get("canary"):
            result = _child_canary(int(golden["canary"]))
            Path(out_path).write_text(json.dumps(result))
            return
        if isinstance(golden, dict) and golden.get("digest_only"):
            result = _child_digest(golden)
            Path(out_path).write_text(json.dumps(result))
            return
        if isinstance(golden, dict) and "plan" in golden:  # phase mode
            Path(out_path).write_text(json.dumps(_child_phase(golden)))
            return
        if isinstance(golden, dict):  # reach mode: only the queries the patch can reach
            Path(out_path).write_text(json.dumps(_child_only(golden)))
            return
        max_q = int(os.environ.get("BR_PLAN_MAXQ", "20")) if golden else 0
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


def _child_only(spec: dict) -> dict:
    """Parse and pickle every reached query; execute the reached executed ones whose
    DuckDB render or optimized tree differs (all of them when the patch touches the
    executor side, spec["all"])."""
    import pickle

    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    only, golden, run_all = set(spec["only"]), spec["rows"], spec["all"]
    run, ei = [], -1
    for qi, (meta, sql, _expected) in enumerate(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")):
        if meta.get("execute"):
            ei += 1
        if qi not in only:
            continue
        pickle.loads(pickle.dumps(parse_one(sql)))
        if not meta.get("execute"):
            continue
        if run_all:
            run.append(sql)
            continue
        rendered = transpile(sql, write="duckdb")
        tree = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
        row = [hashlib.blake2b("|".join(rendered).encode(), digest_size=8).hexdigest(), _digest(tree)]
        if ei >= len(golden) or row != golden[ei]:
            run.append(sql)
    same = _execute_changed(run) if run else True
    return {"ok": True, "same": same, "changed": len(run)}


def _child_phase(spec: dict) -> dict:
    """Run only the phases each reached query needs (see _phase_plan); execute the
    ones whose DuckDB render or optimized tree differs, or that the plan marks."""
    import pickle

    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from sqlglot.optimizer.optimizer import RULES
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    golden, rules = spec["rows"], spec["rules"]
    sqls = [sql for _meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql")]
    snaps: dict = {}

    def snap(r: int):
        if r not in snaps:
            with open(os.path.join(spec["snapdir"], "tpcds_snap_%d.pkl" % r), "rb") as handle:
                snaps[r] = pickle.load(handle)
        return snaps[r]

    run = []
    for qi, ei, parse, render, start, execute in spec["plan"]:
        sql = sqls[qi]
        if parse:
            pickle.loads(pickle.dumps(parse_one(sql)))
        if ei < 0:
            continue
        if execute:
            run.append(sql)
            continue
        row = list(golden[ei])
        if render:
            row[0] = hashlib.blake2b("|".join(transpile(sql, write="duckdb")).encode(), digest_size=8).hexdigest()
        if start is not None:
            base = max((r for r in rules if r <= start and snap(r)[ei] is not None), default=0)
            tree = pickle.loads(snap(base)[ei]) if base else parse_one(sql)
            row[1] = _digest(optimize(tree, TPCDS_SCHEMA, rules=RULES[base:], leave_tables_isolated=True))
        if row != golden[ei]:
            run.append(sql)
    if len(run) > _MAXQ:
        return {"ok": True, "same": False, "changed": len(run)}
    same = _execute_changed(run) if run else True
    return {"ok": True, "same": same, "changed": len(run)}


def _child_digest(spec: dict) -> dict:
    """Compare every TPC-DS query's DuckDB SQL and optimized tree. Do not execute.

    The parent calls this only for the proved file subset. The first mismatch
    keeps the test, so a differing plan pays less than a full run.
    """
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer import optimize
    from tests.helpers import TPCDS_SCHEMA, load_sql_fixture_pairs

    golden = spec["rows"]
    ei = -1
    for meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            parse_one(sql)
            continue
        ei += 1
        rendered = transpile(sql, write="duckdb")
        tree = optimize(parse_one(sql), TPCDS_SCHEMA, leave_tables_isolated=True)
        row = [hashlib.blake2b("|".join(rendered).encode(), digest_size=8).hexdigest(), _digest(tree)]
        if ei >= len(golden) or row != golden[ei]:
            return {"ok": True, "same": False, "changed": 1, "digest": True}
    return {"ok": True, "same": ei + 1 == len(golden) and ei >= 0, "changed": 0, "digest": True}


def _child_canary(n: int) -> dict:
    """Execute the first n TPC-DS queries the test executes. A mismatch keeps it."""
    from tests.helpers import load_sql_fixture_pairs

    sqls = []
    for meta, sql, _expected in load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"):
        if not meta.get("execute"):
            continue
        sqls.append(sql)
        if len(sqls) >= n:
            break
    if len(sqls) < n:
        return {"ok": False}
    same = _execute_changed(sqls, cap=_CANARY_QCPU)
    return {"ok": True, "same": same, "changed": 0 if same else len(sqls)}


def _execute_changed(sqls: list, cap: float | None = None) -> bool:
    """Run each changed query the way test_execute_tpcds does, under the patched tree:
    DuckDB on the rendered SQL is the expected frame, the Python executor the actual."""
    import ast
    import csv
    import pickle

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

    if cap is None:
        cap = float(os.environ.get("BR_PLAN_QCPU", "1.7"))  # local CPU; ~3 s on the server
    t0 = time.process_time()
    try:
        for sql in sqls:
            if time.process_time() - t0 > cap:
                return False  # out of per-commit CPU: keep the test
            expression = pickle.loads(pickle.dumps(parse_one(sql)))  # to the pool and back
            needed = {t.name: None for t in find_tables(expression)}
            for name in needed:
                if name not in tables:
                    setup(name)
            table = execute(expression, schema=TPCDS_SCHEMA, tables={n: tables[n] for n in needed})
            table = pickle.loads(pickle.dumps(table))
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
