"""Tests affected by file-level changes: new, deleted, renamed, copied, binary, config and unknown files.

Each rule says which tests can observe the change, from the pinned tree only:
- a removed module fails its importers; a removed test module's own tests stop reporting;
- a removed or edited data file is seen by the tests that open it (maps.opened_by);
- a new module is seen only by tests that load it by name (they spell its name) and by
  directory walkers (tests/test_typing.py over sqlglot/typing/);
- a new test module is collected, so module-level code that is not plain definitions runs everything;
- pytest reads its own config (pytest.ini, tox.ini, conftest.py, [tool.pytest*] in pyproject.toml,
  [tool:pytest] in setup.cfg); other config and docs are read by no test;
- tests/test_sqlglotc.py run_path()s sqlglotc/setup.py.
Anything outside these rules raises All.
"""
from __future__ import annotations

import ast
import configparser
import importlib.util
import re
import sys
import tomllib

SQLGLOTC_TEST = "tests/test_sqlglotc.py"
TYPING_TEST = "tests/test_typing.py"
PYTEST_FILES = {"pytest.ini", "tox.ini", "conftest.py", "__init__.py"}
STARTUP_NAMES = {"sitecustomize", "usercustomize"}
LOADABLE_EXT = (".so", ".pyd", ".pth", ".pyc", ".pyw")
# pytest looks for its config from the `tests` argument upward
INI_PATHS = {"pyproject.toml", "setup.cfg", "tests/pyproject.toml", "tests/setup.cfg"}


class NeedAll(Exception):
    pass


def _base(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _shadows(name: str) -> bool:
    """A root-level module or directory named like an importable module shadows it (cwd is on sys.path)."""
    if not name.isidentifier():
        return False
    if name in STARTUP_NAMES or name in sys.stdlib_module_names or name in ("sqlglot", "tests"):
        return True
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return True


_PLAIN_DECOS = re.compile(r"^(unittest|pytest|mock|patch|staticmethod|classmethod|property|skip\w*|expectedFailure|functools|cached_property)\b")


def _plain_value(node) -> bool:
    if isinstance(node, (ast.Constant, ast.Name, ast.Attribute)):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_plain_value(e) for e in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is None or _plain_value(k) for k in node.keys) and all(_plain_value(v) for v in node.values)
    if isinstance(node, ast.Call):
        f = ast.unparse(node.func)
        return f.startswith(("os.path.", "re.compile", "Path(", "Path")) and all(_plain_value(a) for a in node.args)
    if isinstance(node, ast.BinOp):
        return _plain_value(node.left) and _plain_value(node.right)
    return False


def _plain_decorators(decos) -> bool:
    return all(_PLAIN_DECOS.match(ast.unparse(d)) for d in decos)


def _plain_body(stmts, in_class=False) -> bool:
    for s in stmts:
        if isinstance(s, (ast.Import, ast.ImportFrom, ast.Pass)):
            continue
        if isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant):
            continue
        if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not _plain_decorators(s.decorator_list):
                return False
            if any(d is not None and not _plain_value(d) for d in s.args.defaults + s.args.kw_defaults):
                return False
            continue
        if isinstance(s, ast.ClassDef):
            if s.decorator_list and not _plain_decorators(s.decorator_list):
                return False
            if s.keywords or not all(isinstance(b, (ast.Name, ast.Attribute)) for b in s.bases):
                return False
            if not _plain_body(s.body, True):
                return False
            continue
        if isinstance(s, ast.Assign) and _plain_value(s.value):
            continue
        if isinstance(s, ast.AnnAssign) and (s.value is None or _plain_value(s.value)):
            continue
        if (not in_class and isinstance(s, ast.If) and "__name__" in ast.unparse(s.test)):
            continue
        return False
    return True


def plain_module(text: str) -> bool:
    """Module-level code is only imports and definitions (nothing runs at collection beyond imports)."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return True  # cannot be collected; other modules still run
    return _plain_body(tree.body)


def _toml_pytest(text: str):
    return tomllib.loads(text).get("tool", {}).get("pytest")


def _cfg_pytest(text: str):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return dict(cp["tool:pytest"]) if cp.has_section("tool:pytest") else None


def pytest_config_same(path: str, old: str | None, new: str | None) -> bool:
    """True when pytest reads the same configuration before and after (root pyproject.toml / setup.cfg)."""
    reader = _toml_pytest if path.endswith(".toml") else _cfg_pytest
    try:
        a = reader(old) if old is not None else None
        b = reader(new) if new is not None else None
    except Exception:
        return False
    return a is None and b is None or a == b


class FileLevel:
    def __init__(self, src, maps, importers_of, tests_module_tests, mentioning, log=lambda *a: None):
        self.src, self.maps = src, maps
        self.importers_of, self.tests_module_tests, self.mentioning = importers_of, tests_module_tests, mentioning
        self.log = log

    # ------------------------------------------------------------ helpers
    def _sqlglotc(self) -> set:
        t = self.maps.file_tests(SQLGLOTC_TEST)
        if not t:
            raise NeedAll("no sqlglotc tests")
        return set(t)

    def _root_importers(self, stem: str) -> set:
        pat = re.compile(rf"(^|\n)\s*(import|from)\s+{re.escape(stem)}\b")
        out: set = set()
        for f in (self.src / "tests").rglob("*.py"):
            try:
                if pat.search(f.read_text(errors="replace")):
                    out |= self.maps.file_tests(str(f.relative_to(self.src)))
            except OSError:
                continue
        return out

    def _pytest_file(self, path: str) -> bool:
        b = _base(path)
        if b in PYTEST_FILES:
            return True
        return path in INI_PATHS

    # ------------------------------------------------------------ data files
    def data_changed(self, path: str, old: str | None, new: str | None) -> set:
        """A non-Python file edited, added or removed."""
        b = _base(path)
        if path.endswith(LOADABLE_EXT) or "__pycache__" in path:
            raise NeedAll(f"loadable file: {path}")
        if b in ("pytest.ini", "tox.ini"):
            raise NeedAll(f"pytest config: {path}")
        if path in INI_PATHS:
            if old is None or new is None or not pytest_config_same(path, old, new):
                raise NeedAll(f"pytest config section: {path}")
            return set()
        if path.startswith("sqlglotc/"):
            return self._sqlglotc()
        opened = self.maps.opened_by(path)
        if opened is not None:
            return set(opened)
        # no test opens it in the pinned tree, and no sqlglot module reads package files
        return set()

    # ------------------------------------------------------------ python files
    def py_removed(self, path: str) -> set:
        return self._py_removed(path) | set(self.maps.opened_by(path) or ())

    def _py_removed(self, path: str) -> set:
        b = _base(path)
        if b in ("__init__.py", "conftest.py"):
            raise NeedAll(f"package/conftest removed: {path}")
        if path.startswith("sqlglot/"):
            imp = self.importers_of(path)
            if not imp:
                raise NeedAll(f"removed module with unknown importers: {path}")
            out = set(imp)
            if path.startswith("sqlglot/typing/"):
                out |= self.maps.file_tests(TYPING_TEST)
            return out
        if path.startswith("tests/"):
            # its own tests stop reporting; importers of a helper fail
            return set(self.maps.file_tests(path)) | set(self.tests_module_tests(self.maps, path))
        if path.startswith("sqlglotc/"):
            return self._sqlglotc()
        if "/" not in path:
            return self._root_importers(path[:-3])
        return set()

    def py_added(self, path: str, text: str) -> set:
        b = _base(path)
        stem = b[:-3]
        if b == "__init__.py":
            raise NeedAll(f"new package: {path}")
        if b == "conftest.py":
            if plain_module(text) and "autouse" not in text and not re.search(r"\bpytest_\w+", text):
                return set()
            raise NeedAll(f"conftest with hooks: {path}")
        if path.startswith("sqlglot/"):
            parts = path[:-3].split("/")
            # a module next to an existing package of the same name loses to the package
            if (self.src / path[:-3]).is_dir():
                return set()
            out = set(self.mentioning({stem})) if stem.isidentifier() else set()
            if len(parts) == 3 and parts[1] == "typing":
                out |= self.maps.file_tests(TYPING_TEST)
            return out
        if path.startswith("tests/"):
            if (b.startswith("test_") or b.endswith("_test.py")) and not plain_module(text):
                raise NeedAll(f"new test module runs code at collection: {path}")
            return set()
        if path.startswith("sqlglotc/"):
            return self._sqlglotc()
        top = path.split("/", 1)[0]
        if "/" not in path:
            if _shadows(stem):
                raise NeedAll(f"root module shadows an import: {path}")
            return set()
        if _shadows(top):
            raise NeedAll(f"root directory shadows an import: {path}")
        return set()

    # ------------------------------------------------------------ entry
    def select(self, fp, new_text_of) -> set:
        """fp is a FilePatch whose paths differ, or that is binary. new_text_of(fp) gives the new text."""
        old_p, new_p = fp.old_path, fp.new_path
        out: set = set()
        if fp.binary:
            for p in {old_p, new_p} - {None}:
                if p.endswith(".py") or self._pytest_file(p):
                    raise NeedAll(f"binary python/config: {p}")
            if old_p and old_p != new_p and not fp.copy:
                out |= self._removed(old_p, new_p)
            if new_p:
                out |= self.data_changed(new_p, None, None)
            return out
        if old_p and old_p != new_p and not fp.copy:
            out |= self._removed(old_p, new_p)
        if new_p and (old_p != new_p or fp.copy):
            text = new_text_of(fp)
            if new_p.endswith(".py"):
                out |= self.py_added(new_p, text)
            else:
                out |= self.data_changed(new_p, None, text)
        return out

    def _removed(self, path: str, moved_to: str | None) -> set:
        if path.endswith(".py"):
            return self.py_removed(path)
        try:
            old = (self.src / path).read_text(errors="replace")
        except OSError:
            old = None
        return self.data_changed(path, old, None)
