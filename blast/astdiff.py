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



_DYNAMIC = {"locals", "vars", "eval", "exec", "dir", "currentframe", "_getframe", "getframeinfo"}
_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)


def _own_nodes(fn):
    """Nodes in fn's own scope (comprehensions included; nested def/lambda/class bodies excluded)."""
    own, nested = [], []
    stack = list(fn.body)
    while stack:
        x = stack.pop()
        if isinstance(x, _SCOPES):
            nested.append(x)
            continue
        own.append(x)
        stack.extend(ast.iter_child_nodes(x))
    return own, nested


class _LocalNorm(ast.NodeVisitor):
    """Give each function's private locals canonical names in order of first use.

    A local is a name the function binds (assignment, for/with target, walrus, comprehension
    target) that is not a parameter, not declared global/nonlocal, and never appears inside a
    nested def/lambda/class. Functions that can see their own frame (locals(), eval, ...) are
    left alone. Two functions whose canonical forms match differ only by a bijective rename of
    names nothing else can observe, so they run the same.
    """

    def visit_FunctionDef(self, fn):
        self._norm(fn)
        self.generic_visit(fn)

    visit_AsyncFunctionDef = visit_FunctionDef

    def _norm(self, fn):
        own, nested = _own_nodes(fn)
        bound, blocked = set(), set()
        a = fn.args
        for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
            if arg is not None:
                blocked.add(arg.arg)
        for x in own:
            if isinstance(x, (ast.Global, ast.Nonlocal)):
                blocked.update(x.names)
            elif isinstance(x, ast.Name):
                if isinstance(x.ctx, (ast.Store, ast.Del)):
                    bound.add(x.id)
                if x.id in _DYNAMIC:
                    return
            elif isinstance(x, ast.Attribute) and x.attr in _DYNAMIC:
                return
            elif isinstance(x, (ast.ExceptHandler,)) and x.name:
                blocked.add(x.name)
            elif isinstance(x, ast.alias):
                blocked.add((x.asname or x.name).split(".")[0])
            elif type(x).__name__ in ("MatchAs", "MatchStar") and x.name:
                blocked.add(x.name)
            elif type(x).__name__ == "MatchMapping" and x.rest:
                blocked.add(x.rest)
        for sub in nested:
            for y in ast.walk(sub):
                if isinstance(y, ast.Name):
                    blocked.add(y.id)
                elif isinstance(y, ast.arg):
                    blocked.add(y.arg)
        names = bound - blocked
        if not names:
            return
        mapping = {}
        for x in sorted((y for y in own if isinstance(y, ast.Name) and y.id in names),
                        key=lambda y: (y.lineno, y.col_offset)):
            if x.id not in mapping:
                mapping[x.id] = "\x00L%d" % len(mapping)
        for x in own:
            if isinstance(x, ast.Name) and x.id in mapping:
                x.id = mapping[x.id]



def _future_annotations(tree) -> bool:
    return any(isinstance(s, ast.ImportFrom) and s.module == "__future__"
               and any(a.name == "annotations" for a in s.names) for s in tree.body)


class _AnnNorm(ast.NodeTransformer):
    """Drop annotations that are never evaluated.

    Under `from __future__ import annotations` a function's parameter and return annotations
    are stored as strings and evaluated only on request (nothing in the target asks for them);
    a local variable's annotation inside a function body is never evaluated. Redundant `pass`
    statements next to other statements are dropped too.
    """

    def __init__(self, future: bool):
        self.future = future
        self.depth = 0

    def _fn(self, node):
        if self.future:
            node.returns = None
            a = node.args
            for arg in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg]:
                if arg is not None:
                    arg.annotation = None
        self.depth += 1
        self.generic_visit(node)
        self.depth -= 1
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = _fn

    def visit_ClassDef(self, node):
        d, self.depth = self.depth, 0
        self.generic_visit(node)
        self.depth = d
        return node

    def visit_AnnAssign(self, node):
        self.generic_visit(node)
        if self.depth and isinstance(node.target, ast.Name):
            node.annotation = ast.Constant(value=None)
        return node

    def generic_visit(self, node):
        super().generic_visit(node)
        for f in _STMT_LISTS:
            body = getattr(node, f, None)
            if isinstance(body, list) and len(body) > 1 and any(isinstance(x, ast.Pass) for x in body):
                kept = [x for x in body if not isinstance(x, ast.Pass)]
                setattr(node, f, kept or body[:1])
        return node


def _plain_def(s, fut: bool) -> bool:
    """A top-level def whose execution runs nothing but binding its name."""
    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) or s.decorator_list:
        return False
    if s.name.startswith("__") and s.name.endswith("__"):
        return False
    a = s.args
    if any(d is not None and not isinstance(d, ast.Constant) for d in a.defaults + a.kw_defaults):
        return False
    if not fut and (s.returns is not None or any(
            x.annotation is not None for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x)):
        return False
    return ">>>" not in (ast.get_docstring(s, clean=False) or "")


def plain_new_module(src: str):
    """Names a module defines when its body is only a docstring, stdlib imports and plain defs; else None.

    Importing such a module runs nothing observable beyond creating it.
    """
    import sys
    try:
        n = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    fut = _future_annotations(n)
    std = sys.stdlib_module_names
    names = set()
    for i, s in enumerate(n.body):
        if i == 0 and isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant) and isinstance(s.value.value, str):
            continue
        if isinstance(s, ast.ImportFrom) and not s.level and s.module and \
                (s.module == "__future__" or s.module.split(".")[0] in std) and all(a.name != "*" for a in s.names):
            continue
        if isinstance(s, ast.Import) and all(a.name.split(".")[0] in std for a in s.names):
            continue
        if _plain_def(s, fut):
            names.add(s.name)
            continue
        return None
    return names


def unused_new_defs(old_src: str, new_src: str) -> set:
    """Top-level plain functions that new_src adds and spells nowhere else in either version.

    Defining one runs nothing but the def statement. sqlglot only enumerates module namespaces for
    classes (helper.subclasses, Dialect lookup by dir()), so a new function is observable only through
    its name, which the caller checks across the tree.
    """
    import re
    try:
        n = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return set()
    fut = _future_annotations(n)
    out = set()
    for s in n.body:
        if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) or s.decorator_list:
            continue
        name = s.name
        if name.startswith("__") and name.endswith("__"):
            continue
        a = s.args
        if any(d is not None and not isinstance(d, ast.Constant) for d in a.defaults + a.kw_defaults):
            continue
        if not fut and (s.returns is not None or any(
                x.annotation is not None for x in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if x)):
            continue
        doc = ast.get_docstring(s, clean=False) or ""
        if ">>>" in doc:
            continue
        w = re.compile(rf"\b{re.escape(name)}\b")
        if w.search(old_src) or len(w.findall(new_src)) != 1:
            continue
        out.add(name)
    return out


def diff(old_src: str, new_src: str, version_norm: bool = True, code_norm: bool = True,
         drop: frozenset = frozenset(), drop_imports: frozenset = frozenset()) -> Optional[List[Region]]:
    """Regions of the old file that changed, [] if ASTs are identical, None if either side is unparsable."""
    try:
        o = ast.parse(old_src)
        n = ast.parse(new_src)
    except (SyntaxError, ValueError, RecursionError):
        return None
    if drop:
        n.body = [s for s in n.body if not (isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)) and s.name in drop)]
    if drop_imports:
        n.body = [s for s in n.body if not (isinstance(s, ast.ImportFrom) and not s.level and all(
            (s.module, a.name, a.asname or a.name) in drop_imports for a in s.names))]
    if (
        version_norm
        and ".version" in old_src
        and ("<=" in old_src or "<=" in new_src or ">=" in old_src or ">=" in new_src)
    ):
        o = _VersionNorm().visit(o)
        n = _VersionNorm().visit(n)
    if code_norm:
        # these keep behaviour but change code objects (co_varnames, co_code)
        _LocalNorm().visit(o)
        _LocalNorm().visit(n)
        fut = _future_annotations(o) and _future_annotations(n)
        o = _AnnNorm(fut).visit(o)
        n = _AnnNorm(fut).visit(n)
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
