"""Static import graph of the sqlglot package (every import statement, including ones inside functions)."""
from __future__ import annotations

import ast
import os
from collections import defaultdict
from pathlib import Path


def module_name(rel: str) -> str:
    m = rel[:-3].replace("/", ".")
    return m[: -len(".__init__")] if m.endswith(".__init__") else m


def build(root: str) -> dict:
    root_p = Path(root)
    mods = {}
    for p in (root_p / "sqlglot").rglob("*.py"):
        rel = str(p.relative_to(root_p))
        mods[module_name(rel)] = (p, rel.endswith("__init__.py"))
    deps = defaultdict(set)
    for name, (p, is_pkg) in mods.items():
        try:
            tree = ast.parse(p.read_text())
        except SyntaxError:
            continue
        pkg = name if is_pkg else name.rsplit(".", 1)[0]
        skip = set()
        for n in ast.walk(tree):  # imports under `if TYPE_CHECKING:` / inside functions never run at import
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                for b in (n.body if isinstance(n.body, list) else [n.body]):
                    for x in ast.walk(b):
                        skip.add(id(x))
            if isinstance(n, ast.If):
                test = ast.unparse(n.test) if hasattr(ast, "unparse") else ""
                if test.endswith("TYPE_CHECKING"):
                    for b in n.body:
                        for x in ast.walk(b):
                            skip.add(id(x))
        for n in ast.walk(tree):
            if id(n) in skip:
                continue
            targets = []
            if isinstance(n, ast.Import):
                targets = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom):
                if n.level:
                    base = pkg.split(".")
                    base = base[: len(base) - (n.level - 1)] if n.level > 1 else base
                    mod = ".".join(base + ([n.module] if n.module else []))
                else:
                    mod = n.module or ""
                targets = [mod] + [f"{mod}.{a.name}" for a in n.names]
            for t in targets:
                # importing a.b.c also imports a and a.b
                parts = t.split(".")
                for i in range(1, len(parts) + 1):
                    cand = ".".join(parts[:i])
                    if cand in mods and cand != name:
                        deps[name].add(cand)
    rdeps = defaultdict(set)
    for a, bs in deps.items():
        for b in bs:
            rdeps[b].add(a)
    return {"mods": sorted(mods), "rdeps": {k: sorted(v) for k, v in rdeps.items()}}


def dependents(graph: dict, start) -> set:
    """start modules plus every module that (transitively) imports one of them."""
    r = graph["rdeps"]
    out = set(start)
    stack = list(start)
    while stack:
        m = stack.pop()
        for d in r.get(m, ()):
            if d not in out:
                out.add(d)
                stack.append(d)
    return out
