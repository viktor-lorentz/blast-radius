"""Structural diff of two Python sources -> regions of the OLD file that changed.

Region.kind:
  'rt'  : code that runs when a function/lambda is called (test time)
  'imp' : code that runs at import/definition time (module/class bodies, decorators, defaults)
  'doc' : docstring change (doctest=True if either side contains a '>>>' prompt)
Lines are inclusive 1-based line numbers in the old file. (0, 0) means "unknown place in module".
"""
from __future__ import annotations

import ast
from typing import List, NamedTuple, Optional


class Region(NamedTuple):
    kind: str
    start: int
    end: int
    doctest: bool = False
    lam: bool = False  # body of a lambda that is only *stored* at import time (not called there)


class _St(NamedTuple):
    mode: str  # 'imp' | 'rt'
    stmt: Optional[ast.AST]
    func: Optional[ast.AST]
    stored: bool = True  # no Call/comprehension between the statement and here
    lam_safe: bool = False  # inside a lambda body that was merely stored at import level


_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)
_STMT_LISTS = ("body", "orelse", "finalbody")


def _first(node) -> int:
    decos = getattr(node, "decorator_list", None)
    first = node.lineno
    if decos:
        first = min(first, min(d.lineno for d in decos))
    return first


def _header_span(stmt):
    first = _first(stmt)
    body = getattr(stmt, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.stmt):
        return first, max(body[0].lineno - 1, stmt.lineno)
    return first, stmt.end_lineno


def _full_span(node):
    return _first(node), node.end_lineno


def _is_doc(stmt) -> bool:
    return (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str))


def _same_const(a, b) -> bool:
    return type(a) is type(b) and (a == b or (a != a and b != b))


class _Differ:
    def __init__(self):
        self.regions: List[Region] = []

    def rec(self, st: _St, extra: Optional[ast.AST] = None):
        if st.stmt is None:
            self.regions.append(Region("imp", 0, 0))
            return
        a, b = _header_span(st.stmt)
        if st.mode == "rt" and st.func is not None:
            if isinstance(st.func, ast.Lambda):
                a, b = min(a, st.func.lineno), max(b, st.func.end_lineno)
            self.regions.append(Region("rt", a, b, False, st.lam_safe))
        else:
            self.regions.append(Region("imp", a, b))

    def rec_doc(self, o, n):
        so, sn = o.value.value, n.value.value
        a, b = o.lineno, o.end_lineno
        self.regions.append(Region("doc", a, b, (">>>" in so) or (">>>" in sn)))

    def visit(self, o, n, st: _St, owner_is_body=False):
        if isinstance(o, ast.stmt) and type(o) is type(n):
            st = _St(st.mode, o, st.func, True, st.lam_safe)
        elif isinstance(o, (ast.Call, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
                            ast.Subscript, ast.Await, ast.Starred)) and type(o) is type(n):
            st = st._replace(stored=False)
        if type(o) is not type(n):
            if isinstance(o, ast.stmt):
                self.rec(_St(st.mode, o, st.func))
            else:
                self.rec(st)
            return
        if isinstance(o, ast.Expr) and owner_is_body and _is_doc(o) and _is_doc(n):
            if o.value.value != n.value.value:
                self.rec_doc(o, n)
            return
        if isinstance(o, ast.Expr) and isinstance(o.value, ast.Constant) and not (
                isinstance(n.value, ast.Constant) and _same_const(o.value.value, n.value.value)):
            # a constant expression (e.g. a docstring) emits no line events, so its own lines have no
            # coverage; whatever replaces it runs whenever the enclosing block runs.
            if st.mode == "rt" and isinstance(st.func, _FUNCS):
                a, b = _full_span(st.func)
                self.regions.append(Region("rt", a, b))
            else:
                self.rec(_St(st.mode, o, st.func))
            return
        for field in o._fields:
            ov, nv = getattr(o, field, None), getattr(n, field, None)
            fst = st
            if isinstance(o, _FUNCS) and field == "body":
                fst = _St("rt", o, o, True, False)
            elif isinstance(o, ast.Lambda) and field == "body":
                if st.mode == "imp":
                    # stored-only lambdas run at test time; lambdas passed to calls may run at import
                    fst = _St("rt", st.stmt, o, True, st.stored) if st.stored else _St("imp", st.stmt, None)
                else:
                    fst = _St("rt", st.stmt, o, True, st.lam_safe)
            if isinstance(ov, list):
                if not isinstance(nv, list) or len(ov) != len(nv):
                    self._list_changed(o, field, st)
                    continue
                is_body = field in _STMT_LISTS or field == "body"
                for i, (x, y) in enumerate(zip(ov, nv)):
                    if isinstance(x, ast.AST):
                        self.visit(x, y, fst, owner_is_body=(is_body and i == 0))
                    elif not _same_const(x, y):
                        self.rec(st)
            elif isinstance(ov, ast.AST):
                if not isinstance(nv, ast.AST):
                    self.rec(st)
                else:
                    self.visit(ov, nv, fst)
            else:
                if not _same_const(ov, nv):
                    self.rec(st)

    def _list_changed(self, o, field, st):
        if isinstance(o, ast.Module):
            self.regions.append(Region("imp", 0, 0))
        elif isinstance(o, _FUNCS) and field == "body":
            a, b = _full_span(o)
            self.regions.append(Region("rt", a, b))
        elif isinstance(o, ast.ClassDef) and field == "body":
            a, b = _header_span(o)
            kind = "rt" if (st.mode == "rt" and st.func is not None) else "imp"
            self.regions.append(Region(kind, a, b))
        else:
            self.rec(_St(st.mode, o if isinstance(o, ast.stmt) else st.stmt, st.func))


# Names of Enum classes defined in the analysed source tree (set by the caller). `x is E.M` and `x == E.M`
# agree for enum members (members are singletons and are what these attributes always hold), and
# `x is None` / `x == None` agree unless type(x).__eq__ treats None as equal (none of sqlglot's do).
ENUM_CLASSES: set = set()


def _eq_safe(node) -> bool:
    if isinstance(node, ast.Constant) and node.value is None:
        return True
    if isinstance(node, ast.Attribute) and node.attr.isupper():
        v = node.value
        name = v.id if isinstance(v, ast.Name) else v.attr if isinstance(v, ast.Attribute) else None
        return name in ENUM_CLASSES
    return False


class _EqNorm(ast.NodeTransformer):
    def visit_Compare(self, node):
        self.generic_visit(node)
        operands = [node.left] + list(node.comparators)
        ops = []
        for i, op in enumerate(node.ops):
            if isinstance(op, (ast.Eq, ast.NotEq)) and (_eq_safe(operands[i]) or _eq_safe(operands[i + 1])):
                op = ast.Is() if isinstance(op, ast.Eq) else ast.IsNot()
            ops.append(op)
        node.ops = ops
        return node


def collect_enums(root) -> set:
    """Enum class names under root/sqlglot (transitively through local Enum subclasses)."""
    from pathlib import Path
    bases = {}
    for f in Path(root, "sqlglot").rglob("*.py"):
        try:
            t = ast.parse(f.read_text())
        except (SyntaxError, ValueError):
            continue
        for c in ast.walk(t):
            if isinstance(c, ast.ClassDef):
                bases.setdefault(c.name, set()).update(
                    b.id if isinstance(b, ast.Name) else b.attr if isinstance(b, ast.Attribute) else "" for b in c.bases)
    enums = {"Enum", "IntEnum", "StrEnum", "Flag", "IntFlag"}
    changed = True
    while changed:
        changed = False
        for n, bs in bases.items():
            if n not in enums and bs & enums:
                enums.add(n)
                changed = True
    return enums


def _is_dialect_version(node) -> bool:
    # Dialect.__init__ stores version as a 3-tuple. Nothing else assigns it.
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "version"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "dialect"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def _const_tuple_len(node):
    if isinstance(node, ast.Tuple) and node.elts and all(
        isinstance(e, ast.Constant) and type(e.value) is int for e in node.elts
    ):
        return len(node.elts)
    return None


class _VersionNorm(ast.NodeTransformer):
    """`<` and `<=` agree when a 3-tuple is compared to any other length.

    Python orders a longer tuple above a matching shorter one, so the equal
    case is unreachable and the two operators are the same program.
    """

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1:
            return node
        op = node.ops[0]
        if not isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            return node
        other = None
        if _is_dialect_version(node.left):
            other = node.comparators[0]
        elif _is_dialect_version(node.comparators[0]):
            other = node.left
        length = _const_tuple_len(other) if other is not None else None
        if length is None or length == 3:
            return node
        if isinstance(op, ast.LtE):
            node.ops = [ast.Lt()]
        elif isinstance(op, ast.GtE):
            node.ops = [ast.Gt()]
        return node


def diff(old_src: str, new_src: str, version_norm: bool = True) -> Optional[List[Region]]:
    """Regions of the old file that changed, [] if ASTs are identical, None if either side is unparsable."""
    try:
        o = ast.parse(old_src)
        n = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if (
        version_norm
        and ".version" in old_src
        and ("<=" in old_src or "<=" in new_src or ">=" in old_src or ">=" in new_src)
    ):
        o = _VersionNorm().visit(o)
        n = _VersionNorm().visit(n)
    if ENUM_CLASSES:
        o = _EqNorm().visit(o)
        n = _EqNorm().visit(n)
    d = _Differ()
    d.visit(o, n, _St("imp", None, None))
    seen = set()
    out = []
    for r in d.regions:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
