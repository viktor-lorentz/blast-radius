"""Tests that name a dialect through a helper in their own file.

A load trace can miss a test that reaches a dialect only through a helper, for
example a fixture runner that executes every query via ``.sql(dialect="duckdb")``.
This map is static and built from the pinned tree. A test method names dialect D
when it, or a function or method of its file that it reaches through ``self.m(...)``,
``cls.m(...)`` or ``f(...)``, passes the string D as ``dialect=``, ``read=`` or
``write=``, or as the first argument of ``.sql(...)``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_KW = {"dialect", "read", "write"}


def _spelled(fn: ast.AST) -> set:
    out = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        for k in n.keywords:
            if k.arg in _KW and isinstance(k.value, ast.Constant) and isinstance(k.value.value, str):
                out.add(k.value.value.lower())
        f = n.func
        if (
            isinstance(f, ast.Attribute)
            and f.attr == "sql"
            and n.args
            and isinstance(n.args[0], ast.Constant)
            and isinstance(n.args[0].value, str)
        ):
            out.add(n.args[0].value.lower())
    return out


def _calls(fn: ast.AST) -> set:
    out = set()
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Name):
            out.add(f.id)
        elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("self", "cls"):
            out.add(f.attr)
    return out


def file_map(rel: str, text: str) -> dict:
    """dialect name -> set of test ids in this file whose call closure names it."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    funcs = {}
    for s in tree.body:
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[s.name] = s
    out: dict = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        meths = {m.name: m for m in cls.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        scope = {**funcs, **meths}
        memo: dict = {}
        for name, m in meths.items():
            if not name.startswith("test"):
                continue
            seen, stack, names = set(), [name], set()
            while stack:
                cur = stack.pop()
                if cur in seen or cur not in scope:
                    continue
                seen.add(cur)
                if cur not in memo:
                    memo[cur] = (_spelled(scope[cur]), _calls(scope[cur]))
                sp, cs = memo[cur]
                names |= sp
                stack.extend(cs - seen)
            tid = f"{rel}::{cls.name}::{name}"
            for d in names:
                out.setdefault(d, set()).add(tid)
    return out


def build(src: Path) -> dict:
    out: dict = {}
    for p in sorted((src / "tests").rglob("test_*.py")):
        try:
            text = p.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for d, ids in file_map(p.relative_to(src).as_posix(), text).items():
            out.setdefault(d, set()).update(ids)
    return out
