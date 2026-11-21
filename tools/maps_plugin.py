"""pytest plugin (build-time only): record which data files each test opens.

Each xdist worker writes $BR_OPENS_DIR/opens-<pid>.json with entries
[relative_path, phase, owner] where phase is 'call' | 'setup' | 'teardown' | 'collect' | 'global'.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.realpath(os.environ.get("BR_SRC_ROOT", os.getcwd()))
OUT = os.environ.get("BR_OPENS_DIR", "/tmp/opens")
_cur = [("global", "")]
_seen = set()


_IMPORTS = os.environ.get("BR_IMPORTS") == "1"
_imps = set()  # (module, kind, owner) for lazily loaded sqlglot modules
_LAZY_PKGS = ("sqlglot.dialects.", "sqlglot.generators.", "sqlglot.parsers.")
_eager = set()


def _hook(event, args):
    if event == "import" and _IMPORTS:
        name = args[0]
        if isinstance(name, str) and name.startswith(_LAZY_PKGS) and name not in _eager:
            kind, owner = _cur[-1]
            _imps.add((name, kind, owner))
        return
    if event != "open":
        return
    p = args[0]
    if isinstance(p, bytes):
        try:
            p = p.decode()
        except UnicodeDecodeError:
            return
    if not isinstance(p, str):
        return
    if p.endswith((".py", ".pyc", ".so")):
        return
    try:
        rp = os.path.realpath(p)
    except Exception:
        return
    if not rp.startswith(ROOT + os.sep):
        return
    rel = os.path.relpath(rp, ROOT)
    kind, owner = _cur[-1]
    _seen.add((rel, kind, owner))


if _IMPORTS:
    import sqlglot  # noqa: F401  (everything it imports eagerly is not "lazy")

    _eager.update(m for m in sys.modules if m.startswith("sqlglot"))
sys.addaudithook(_hook)


def _unload_lazy():
    """Forget lazily loaded modules so the next importer really re-imports (and logs) them."""
    if not _IMPORTS:
        return
    gone = [m for m in list(sys.modules) if m.startswith(_LAZY_PKGS) and m not in _eager]
    for m in gone:
        mod = sys.modules.pop(m, None)
        parent, _, leaf = m.rpartition(".")
        pm = sys.modules.get(parent)
        if pm is not None and getattr(pm, leaf, None) is mod:
            try:
                delattr(pm, leaf)
            except Exception:
                pass
    try:
        from sqlglot.dialects.dialect import Dialect

        gm = set(gone)
        for k in [k for k, c in Dialect._classes.items() if c.__module__ in gm]:
            del Dialect._classes[k]
    except Exception:
        pass


# --- run process-pool work in-process so coverage/contexts/opens are attributed to the calling test
import concurrent.futures as _cf  # noqa: E402


class _SyncExecutor:
    def __init__(self, *a, **k):
        init = k.get("initializer")
        if init is not None:  # a real pool runs this in every worker before any task
            init(*k.get("initargs", ()))

    def submit(self, fn, *a, **k):
        # Same semantics as a real process pool: arguments and the result cross a pickle boundary
        # (which is where e.g. sqlglot.serde runs), but everything executes inside the calling test.
        import pickle

        f = _cf.Future()
        try:
            fn2, a2, k2 = pickle.loads(pickle.dumps((fn, a, k)))
            res = fn2(*a2, **k2)
            f.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *iterables, **k):
        return [self.submit(fn, *args).result() for args in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_cf.ProcessPoolExecutor = _SyncExecutor


import pytest  # noqa: E402


_ATTRS = os.environ.get("BR_ATTRS") == "1"
_reads = {}  # (phase, owner) -> set of keys
_installed = set()
_cur_reads = [set()]


class _Rec:
    """Non-data descriptor standing in for a class attribute; records which test reads it."""

    __slots__ = ("v", "k")

    def __init__(self, v, k):
        self.v = v
        self.k = k

    def __get__(self, inst, owner=None):
        _cur_reads[0].add(self.k)
        return self.v


def _set_reads(kind, owner):
    _cur_reads[0] = _reads.setdefault((kind, owner), set())


def _install_descriptors():
    import enum
    import types

    import sqlglot

    seen = set()
    n = 0
    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        stack = [v for v in vars(mod).values() if isinstance(v, type) and v.__module__ == name]
        while stack:
            c = stack.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            if issubclass(c, enum.Enum):
                continue
            ck = f"{c.__module__}.{c.__qualname__}"
            for k, v in list(vars(c).items()):
                if k.startswith("__") and k.endswith("__"):
                    continue
                if isinstance(v, type):
                    if v.__module__ == name and v.__qualname__.startswith(c.__qualname__ + "."):
                        stack.append(v)
                elif isinstance(v, (types.FunctionType, staticmethod, classmethod, property)) or hasattr(type(v), "__get__"):
                    continue
                try:
                    setattr(c, k, _Rec(v, f"C|{ck}|{k}"))
                    _installed.add(f"C|{ck}|{k}")
                    n += 1
                except Exception:
                    pass
    print(f"[maps_plugin] installed {n} attribute recorders", file=sys.stderr)


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session):
    if _IMPORTS:
        return
    """Import every sqlglot module up-front (outside any test) so that import-time lines are
    attributed to import time (empty context), not to whichever test triggers a lazy import."""
    import importlib
    import pkgutil

    try:
        import sqlglot

        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            if m.name.endswith("__main__"):
                continue
            try:
                importlib.import_module(m.name)
            except Exception:
                pass
    except Exception:
        pass
    if _ATTRS:
        _install_descriptors()


# --- isolate tests from runtime caches: every test recomputes cached state, so coverage and attribute reads
# are attributed to every test that depends on them (not just the first one to fill a cache).
_snap = []        # (container, snapshot)
_lru = []         # functools lru/cache wrappers
_cls_keys = []    # (class, frozenset(own keys))


def _unwrap(v):
    return v.v if isinstance(v, _Rec) else v


def _take_snapshot():
    import functools

    seen = set()
    for name, mod in list(sys.modules.items()):
        if not (name == "sqlglot" or name.startswith("sqlglot.")) or mod is None:
            continue
        vals = list(vars(mod).values())
        classes = [v for v in vals if isinstance(v, type) and getattr(v, "__module__", "") == name]
        while classes:
            c = classes.pop()
            if id(c) in seen:
                continue
            seen.add(id(c))
            _cls_keys.append((c, frozenset(vars(c))))
            for v in list(vars(c).values()):
                v = _unwrap(v)
                if isinstance(v, type) and getattr(v, "__module__", "") == name:
                    classes.append(v)
                else:
                    vals.append(v)
        for v in vals:
            v = _unwrap(v)
            if isinstance(v, (staticmethod, classmethod)):
                v = v.__func__
            if id(v) in seen:
                continue
            seen.add(id(v))
            if isinstance(v, dict):
                _snap.append((v, dict(v)))
            elif isinstance(v, list):
                _snap.append((v, list(v)))
            elif isinstance(v, set):
                _snap.append((v, set(v)))
            elif hasattr(v, "cache_clear") and callable(v):
                _lru.append(v)
    print(f"[maps_plugin] cache isolation: {len(_snap)} containers, {len(_lru)} lru caches, "
          f"{len(_cls_keys)} classes", file=sys.stderr)


def _restore():
    for c, s in _snap:
        try:
            if len(c) != len(s):
                if isinstance(c, dict):
                    c.clear()
                    c.update(s)
                elif isinstance(c, list):
                    c[:] = s
                else:
                    c.clear()
                    c.update(s)
        except Exception:
            pass
    for f in _lru:
        try:
            f.cache_clear()
        except Exception:
            pass
    for c, keys in _cls_keys:
        try:
            extra = [k for k in list(vars(c)) if k not in keys]
            for k in extra:
                delattr(c, k)
        except Exception:
            pass


def pytest_collection_finish(session):
    _take_snapshot()


def pytest_collectstart(collector):
    _cur.append(("collect", getattr(collector, "nodeid", "") or ""))
    _set_reads(*_cur[-1])


def pytest_collectreport(report):
    _unload_lazy()
    if len(_cur) > 1 and _cur[-1][0] == "collect":
        _cur.pop()
    _set_reads(*_cur[-1])


def pytest_runtest_logstart(nodeid, location):
    _cur.append(("setup", nodeid))
    _set_reads(*_cur[-1])


def pytest_runtest_setup(item):
    _cur[-1] = ("setup", item.nodeid)
    _set_reads(*_cur[-1])


def pytest_runtest_call(item):
    _cur[-1] = ("call", item.nodeid)
    _set_reads(*_cur[-1])


def pytest_runtest_teardown(item):
    _cur[-1] = ("teardown", item.nodeid)
    _set_reads(*_cur[-1])


def pytest_runtest_logfinish(nodeid, location):
    _unload_lazy()
    _restore()
    if len(_cur) > 1:
        _cur.pop()
    _set_reads(*_cur[-1])


def pytest_sessionfinish(session):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"opens-{os.getpid()}.json"), "w") as f:
        json.dump(sorted(_seen), f)
    if _IMPORTS:
        with open(os.path.join(OUT, f"imports-{os.getpid()}.json"), "w") as f:
            json.dump(sorted(_imps), f)
    if _ATTRS:
        with open(os.path.join(OUT, f"attrs-{os.getpid()}.json"), "w") as f:
            json.dump([[k, o, sorted(v)] for (k, o), v in _reads.items() if v], f)
        with open(os.path.join(OUT, f"installed-{os.getpid()}.json"), "w") as f:
            json.dump(sorted(_installed), f)
