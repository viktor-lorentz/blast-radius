"""Deterministic fingerprints of every sqlglot module global and class attribute after a full import.

Run as a script inside a fresh interpreter:
    python -m blast.fingerprint SRC_ROOT [PATCHED_JSON]   -> JSON on stdout
PATCHED_JSON (optional) maps relative paths ("sqlglot/x.py") to replacement source text; those modules
are imported from that text instead of the file on disk.

Output: {"ok": true, "mods": [...], "fp": {key: digest}, "fn": {key: [file, first, last]}}
  key = "G|<module>|<name>"            module global
        "C|<module>.<qualname>|<attr>"  class attribute (own __dict__ only)
        "K|<module>.<qualname>"         class identity: bases + attribute-name set
"""
from __future__ import annotations

import enum
import functools
import hashlib
import importlib
import importlib.abc
import importlib.util
import json
import os
import pkgutil
import re
import sys
import types

_ADDR = re.compile(r" at 0x[0-9a-fA-F]+")


class _Fp:
    def __init__(self, unordered=False):
        self.memo = {}
        self.busy = set()
        self.unordered = unordered  # treat tuples/lists as multisets (for hash-seed-dependent values)

    def h(self, s: str) -> str:
        return hashlib.blake2b(s.encode("utf-8", "surrogatepass"), digest_size=12).hexdigest()

    def fp(self, v) -> str:
        t = type(v)
        if v is None or t in (bool, int, float, complex, str, bytes):
            return self.h(f"{t.__name__}:{v!r}")
        i = id(v)
        m = self.memo.get(i)
        if m is not None:
            return m[0]
        if i in self.busy:
            return "cycle"
        self.busy.add(i)
        try:
            r = self._fp(v, t)
        finally:
            self.busy.discard(i)
        self.memo[i] = (r, v)  # keep v alive so ids are not reused
        return r

    def _fp(self, v, t) -> str:
        if isinstance(v, type):
            return self.h(f"cls:{getattr(v, '__module__', '')}.{getattr(v, '__qualname__', '')}")
        if isinstance(v, enum.Enum):
            return self.h(f"enum:{type(v).__module__}.{type(v).__qualname__}.{v.name}:{self.fp(v.value)}")
        if t in (tuple, list):
            parts = [self.fp(x) for x in v]
            if self.unordered:
                parts.sort()
            return self.h(t.__name__ + ":" + ",".join(parts))
        if isinstance(v, dict):
            return self.h("dict:" + ",".join(sorted(self.fp(k) + "=" + self.fp(x) for k, x in v.items())))
        if isinstance(v, (set, frozenset)):
            return self.h("set:" + ",".join(sorted(self.fp(x) for x in v)))
        if isinstance(v, types.CodeType):
            return self.h(f"code:{v.co_name}:{v.co_code.hex()}:{self.fp(v.co_consts)}:{v.co_names}:{v.co_varnames}")
        if isinstance(v, types.FunctionType):
            cells = []
            for c in v.__closure__ or ():
                try:
                    cells.append(self.fp(c.cell_contents))
                except ValueError:
                    cells.append("empty")
            return self.h(f"fn:{self.fp(v.__code__)}:{self.fp(v.__defaults__)}:{self.fp(v.__kwdefaults__)}:{','.join(cells)}")
        if isinstance(v, (staticmethod, classmethod)):
            return self.h(f"{t.__name__}:{self.fp(v.__func__)}")
        if isinstance(v, property):
            return self.h(f"prop:{self.fp(v.fget)}:{self.fp(v.fset)}:{self.fp(v.fdel)}")
        if isinstance(v, functools.partial):
            return self.h(f"partial:{self.fp(v.func)}:{self.fp(v.args)}:{self.fp(v.keywords)}")
        if isinstance(v, types.ModuleType):
            return self.h(f"mod:{v.__name__}")
        if isinstance(v, (types.BuiltinFunctionType, types.MethodWrapperType, types.WrapperDescriptorType)):
            return self.h(f"builtin:{getattr(v, '__qualname__', repr(v))}")
        if isinstance(v, types.MethodType):
            return self.h(f"method:{self.fp(v.__func__)}:{self.fp(v.__self__)}")
        if isinstance(v, re.Pattern):
            return self.h(f"re:{v.pattern!r}:{v.flags}")
        d = getattr(v, "__dict__", None)
        dd = d if isinstance(d, dict) else {}
        names = []
        for k in t.__mro__:
            sl = k.__dict__.get("__slots__", ())
            names += [sl] if isinstance(sl, str) else list(sl)
        names = [n for n in dict.fromkeys(names) if n not in ("parent", "_hash", "__weakref__", "__dict__")]
        if dd and not names:
            return self.h(f"obj:{t.__module__}.{t.__qualname__}:{self.fp(dd)}")
        if names:
            vals = []
            if dd:
                vals.append("__dict__=" + self.fp(dd))
            for s in names:
                vals.append(s + "=" + (self.fp(getattr(v, s)) if hasattr(v, s) else "unset"))
            return self.h(f"slots:{t.__module__}.{t.__qualname__}:{','.join(vals)}")
        try:
            r = repr(v)
        except Exception:  # noqa: BLE001
            r = "unrepr"
        return self.h(f"repr:{t.__module__}.{t.__qualname__}:{_ADDR.sub('', r)}")


class _PatchedFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root: str, patched: dict):
        self.root = root
        self.by_mod = {}
        for rel, text in patched.items():
            mod = rel[:-3].replace("/", ".")
            if mod.endswith(".__init__"):
                mod = mod[: -len(".__init__")]
            self.by_mod[mod] = (os.path.join(root, rel), text, rel.endswith("__init__.py"))

    def find_spec(self, fullname, path=None, target=None):
        hit = self.by_mod.get(fullname)
        if hit is None:
            return None
        origin, text, is_pkg = hit
        loader = _TextLoader(fullname, origin, text)
        spec = importlib.util.spec_from_file_location(
            fullname, origin, loader=loader,
            submodule_search_locations=[os.path.dirname(origin)] if is_pkg else None)
        return spec


class _TextLoader(importlib.abc.Loader):
    def __init__(self, name, origin, text):
        self.name, self.origin, self.text = name, origin, text

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        code = compile(self.text, self.origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)


def import_all(root: str):
    import sqlglot  # noqa: F401

    ok, failed = ["sqlglot"], []
    for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if m.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(m.name)
            ok.append(m.name)
        except BaseException:  # noqa: BLE001
            failed.append(m.name)
    return ok, failed


def classes_of(mod):
    seen = set()
    stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == mod.__name__]
    while stack:
        c = stack.pop()
        if id(c) in seen:
            continue
        seen.add(id(c))
        yield c
        for v in list(vars(c).values()):
            if isinstance(v, type) and v.__module__ == mod.__name__ and v.__qualname__.startswith(c.__qualname__ + "."):
                stack.append(v)


def snapshot(names, noisy=frozenset()):
    """-> (fp_by_module, fn) ; fp_by_module[module][key] = digest"""
    F = _Fp()
    U = _Fp(unordered=True)
    bymod, fn = {}, {}

    def note_fn(key, v):
        f = v.__func__ if isinstance(v, (staticmethod, classmethod)) else v
        if isinstance(f, types.FunctionType):
            c = f.__code__
            lines = [ln for _, _, ln in c.co_lines() if ln]
            fn[key] = [c.co_filename, min(lines, default=c.co_firstlineno), max(lines, default=c.co_firstlineno)]

    for name in sorted(names):
        mod = sys.modules.get(name)
        if mod is None:
            continue
        fp = bymod.setdefault(name, {})
        for k, v in list(vars(mod).items()):
            if k.startswith("__") and k.endswith("__"):
                continue
            key = f"G|{name}|{k}"
            fp[key] = (U if key in noisy else F).fp(v)
            note_fn(key, v)
        for c in classes_of(mod):
            ck = f"{c.__module__}.{c.__qualname__}"
            own = {k: v for k, v in vars(c).items() if not (k.startswith("__") and k.endswith("__"))}
            fp[f"K|{ck}"] = F.h("bases:" + ",".join(f"{b.__module__}.{b.__qualname__}" for b in c.__bases__)
                                 + "|attrs:" + ",".join(sorted(own)))
            for k, v in own.items():
                key = f"C|{ck}|{k}"
                fp[key] = (U if key in noisy else F).fp(v)
                note_fn(key, v)
    return bymod, fn


def import_some(names):
    import sqlglot  # noqa: F401

    ok, failed = [], []
    for n in sorted(names):
        if n.endswith("__main__"):
            continue
        try:
            importlib.import_module(n)
            ok.append(n)
        except BaseException:  # noqa: BLE001
            failed.append(n)
    return ok, failed


def compare(root, patched, noisy, base_path, closure):
    """Child-side comparison against the build-time baseline; returns only what changed."""
    base = json.loads(open(base_path).read())
    sys.path.insert(0, root)
    sys.meta_path.insert(0, _PatchedFinder(root, patched))
    try:
        ok, failed = import_some(closure)
    except BaseException as e:  # noqa: BLE001
        return {"ok": False, "error": repr(e)}
    newly_failed = sorted(set(failed) - set(base.get("failed", [])))
    bymod, fn = snapshot(ok, noisy)
    dropped = set(base.get("dropped", []))
    changed = {}
    for m in ok:
        b = base["bymod"].get(m, {})
        n = bymod.get(m, {})
        for k in set(b) | set(n):
            if k not in dropped and b.get(k) != n.get(k):
                changed[k] = [k in b, k in n]
    bfn = base.get("fn", {})
    return {"ok": True, "newly_failed": newly_failed, "changed": changed,
            "bfn": {k: bfn[k] for k in changed if k in bfn}, "nfn": {k: fn[k] for k in changed if k in fn}}


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--compare":
        # --compare ROOT PATCHED_JSON NOISY_JSON BASE_JSON CLOSURE_JSON
        root, pj, nj, bj, cj = sys.argv[2:7]
        out = compare(root, json.loads(open(pj).read()), frozenset(json.loads(open(nj).read())), bj,
                      json.loads(open(cj).read()))
        json.dump(out, sys.stdout, separators=(",", ":"))
        return
    root = sys.argv[1]
    patched = json.loads(open(sys.argv[2]).read()) if len(sys.argv) > 2 and sys.argv[2] != "-" else {}
    noisy = frozenset(json.loads(open(sys.argv[3]).read())) if len(sys.argv) > 3 else frozenset()
    sys.path.insert(0, root)
    if patched:
        sys.meta_path.insert(0, _PatchedFinder(root, patched))
    try:
        ok, failed = import_all(root)
    except BaseException as e:  # noqa: BLE001
        json.dump({"ok": False, "error": repr(e)}, sys.stdout)
        return
    bymod, fn = snapshot(ok, noisy)
    json.dump({"ok": True, "mods": sorted(ok), "failed": sorted(failed), "bymod": bymod, "fn": fn}, sys.stdout,
              separators=(",", ":"))


if __name__ == "__main__":
    main()
