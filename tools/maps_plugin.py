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


def _hook(event, args):
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


sys.addaudithook(_hook)


import pytest  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_collection(session):
    """Import every sqlglot module up-front (outside any test) so that import-time lines are
    attributed to import time (empty context), not to whichever test triggers a lazy import."""
    import importlib
    import pkgutil

    try:
        import sqlglot

        for m in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
            try:
                importlib.import_module(m.name)
            except Exception:
                pass
    except Exception:
        pass


def pytest_collectstart(collector):
    _cur.append(("collect", getattr(collector, "nodeid", "") or ""))


def pytest_collectreport(report):
    if len(_cur) > 1 and _cur[-1][0] == "collect":
        _cur.pop()


def pytest_runtest_logstart(nodeid, location):
    _cur.append(("setup", nodeid))


def pytest_runtest_setup(item):
    _cur[-1] = ("setup", item.nodeid)


def pytest_runtest_call(item):
    _cur[-1] = ("call", item.nodeid)


def pytest_runtest_teardown(item):
    _cur[-1] = ("teardown", item.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    if len(_cur) > 1:
        _cur.pop()


def pytest_sessionfinish(session):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"opens-{os.getpid()}.json"), "w") as f:
        json.dump(sorted(_seen), f)
