"""Drop test_max_nodes when the patch cannot change its result.

The test parses a few small statements and one wide, deep equality list. It
checks that the small ones return a SELECT, that a node limit of 5 raises, that
the counter resets, and that the wide statement raises ParseError with the limit
in the message. Those facts follow from the real node counter on a depth-100
prefix of the same statement plus the small parses. A crash, a timeout, or a
different count keeps the test. This never adds a test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from blast import patch as P
from blast.split import split

MAX_NODES_ID = "tests/test_parser.py::TestParser::test_max_nodes"
_MIN_MARGINAL_S = 4.0
_BUDGET_S = 6.0
_COMMIT_TIMEOUT_S = 20


def _cost(nums, ms) -> float:
    workers = split(nums, ms)
    if not workers:
        return 0.0
    return (max(sum(ms[n] for n in w) for w in workers) + 2000 * len(workers)) / 1000


def _patch_map(src: Path, text: str) -> dict | None:
    """Patched Python sources, or None when the proof would not apply."""
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
        # The test body itself is the spec. A change to it is not replayed here.
        if path == "tests/test_parser.py":
            return None
        try:
            out[path] = P.apply((src / path).read_text(), fp)
        except (P.PatchError, OSError, UnicodeError):
            return None
    return out or None


def _run(src: Path, patched: dict) -> dict:
    src = Path(src).resolve()
    tmp = os.environ.get("TMPDIR", "/tmp")
    fd, pin = tempfile.mkstemp(prefix="brmax", suffix=".json", dir=tmp)
    out_fd, outp = tempfile.mkstemp(prefix="brmax", suffix=".out", dir=tmp)
    os.close(out_fd)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(patched, handle)
        repo = str(Path(__file__).resolve().parents[1])
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONHASHSEED="0")
        env["PYTHONPATH"] = repo + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.run(
            [sys.executable, "-m", "blast.maxnodes", "--child", str(src), pin, outp],
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
        for path in (pin, outp):
            try:
                os.unlink(path)
            except OSError:
                pass


def narrow_max_nodes(src: Path, rows: list, id_to_number: dict, ms: dict,
                     budget_s: float = _BUDGET_S, min_marginal_s: float = _MIN_MARGINAL_S) -> dict:
    """Return {commit: numbers without test_max_nodes} when the proof says it passes.

    `rows` is (commit, patch_text, numbers). Anything not returned is unchanged.
    Stops after `budget_s` of process CPU. Highest marginal cost first.
    """
    src = Path(src).resolve()
    number = id_to_number.get(MAX_NODES_ID)
    if number is None:
        return {}
    ranked = []
    for stem, text, nums in rows:
        if number not in nums:
            continue
        marginal = _cost(nums, ms) - _cost(nums - {number}, ms)
        if marginal < min_marginal_s:
            continue
        patched = _patch_map(src, text)
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
    golden = _run(src, {})
    base = golden.get("sig") if golden.get("ok") else None
    if not base:
        return {}
    updates = {}
    for _marginal, stem, patched, nums in ranked:
        if cpu() - start >= budget_s:
            break
        got = _run(src, patched)
        if got.get("ok") and got.get("sig") == base:
            updates[stem] = set(nums) - {number}
    return updates


def _child(src: str, patched_path: str, out_path: str) -> None:
    patched = json.loads(Path(patched_path).read_text())
    sys.path.insert(0, src)
    os.chdir(src)
    if patched:
        from blast.fingerprint import _PatchedFinder

        sys.meta_path.insert(0, _PatchedFinder(src, patched))
    try:
        sig = _signature()
        Path(out_path).write_text(json.dumps({"ok": True, "sig": sig}))
    except Exception as exc:  # noqa: BLE001
        Path(out_path).write_text(json.dumps({"ok": False, "err": type(exc).__name__}))


def _signature() -> list:
    """Facts the test asserts, minus the two parses that build a million nodes.

    The wide statement is `SELECT` plus 10000 copies of `x=x` repeated 100 times.
    Its node count is linear in that width. Matching the counter at widths 1, 2,
    and 8, and seeing the real error text at a limit of 50, fixes both large cases.
    """
    import sqlglot.parser as parser_mod
    from sqlglot import exp, parse_one
    from sqlglot.errors import ParseError

    if not isinstance(parse_one("SELECT 1", max_nodes=1_000_000), exp.Select):
        raise AssertionError("select")
    wide = "SELECT " + ",".join(f"col{i}" for i in range(500)) + " FROM t"
    if not isinstance(parse_one(wide, max_nodes=1_000_000), exp.Select):
        raise AssertionError("wide")
    try:
        parse_one("SELECT " + ",".join(f"x={i}" for i in range(100)), max_nodes=5)
        raise AssertionError("limit")
    except ParseError:
        pass
    if not isinstance(parse_one("SELECT 1, 2, 3"), exp.Select):
        raise AssertionError("reset")

    orig = parser_mod.Parser.validate_expression
    seen = [0]

    def wrapped(self, expression, args=None):
        result = orig(self, expression, args)
        seen[0] = self._node_count
        return result

    parser_mod.Parser.validate_expression = wrapped
    try:
        def count(width: int) -> int:
            seen[0] = 0
            expr = "x" + ("=x" * 100)
            parse_one("SELECT " + ",".join([expr] * width), max_nodes=10**9)
            return seen[0]

        counts = [count(1), count(2), count(8)]
    finally:
        parser_mod.Parser.validate_expression = orig

    expr = "x" + ("=x" * 100)
    try:
        parse_one("SELECT " + expr, max_nodes=50)
        raise AssertionError("msg")
    except ParseError as exc:
        text = str(exc)
        if "Maximum number of AST nodes" not in text or "(50)" not in text:
            raise AssertionError("msg") from exc
    return counts


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        _child(sys.argv[2], sys.argv[3], sys.argv[4])
        return
    raise SystemExit("run via blast.maxnodes.narrow_max_nodes")


if __name__ == "__main__":
    main()
