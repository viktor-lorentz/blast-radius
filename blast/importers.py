"""Which scored tests import a sqlglot module.

A syntax error or an import-time crash is visible only to tests that load the
module. The map is the pinned tree's import graph plus the per-test coverage
of lines that actually ran: nothing here is keyed by a patch.

Import-time lines in the coverage map are not attributed to a test, so the
graph carries those. A dynamic import whose target is not a constant stays
unknown and the caller keeps the wide selection.
"""
from __future__ import annotations

import ast
import json
from collections import defaultdict
from pathlib import Path

_SKIP = frozenset({"TYPE_CHECKING"})


def _mod(path: str) -> str:
    p = path[:-3] if path.endswith(".py") else path
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    return p.replace("/", ".")


def _type_checking(test) -> bool:
    if isinstance(test, ast.Name):
        return test.id in _SKIP
    if isinstance(test, ast.Attribute):
        return test.attr in _SKIP
    return False


def _imports_of(node):
    """sqlglot module names this import statement loads, plus a dynamic flag."""
    found = []
    dynamic = False
    if isinstance(node, ast.Import):
        for alias in node.names:
            if alias.name.startswith("sqlglot"):
                found.append(alias.name)
    elif isinstance(node, ast.ImportFrom) and not node.level and node.module and node.module.startswith("sqlglot"):
        found.append(node.module)
        for alias in node.names:
            found.append(node.module + "." + alias.name)
    elif isinstance(node, ast.Call):
        func = node.func
        name = getattr(func, "attr", None) or getattr(func, "id", None)
        if name == "import_module" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value.startswith("sqlglot"):
                found.append(arg.value)
            elif isinstance(arg, ast.JoinedStr):
                prefix = "".join(v.value for v in arg.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
                if prefix.startswith("sqlglot"):
                    found.append(prefix)
                    dynamic = True
            else:
                dynamic = True
    return found, dynamic


def _import_time(tree):
    """Import nodes that run when the module is imported. Function bodies do not."""
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, ast.If) and _type_checking(child.test):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                out.append(child)
            walk(child)

    walk(tree)
    return out


def _lazy_calls(tree):
    out = []

    def walk(node, in_fn):
        for child in ast.iter_child_nodes(node):
            nxt = in_fn or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            if nxt and isinstance(child, (ast.Import, ast.ImportFrom, ast.Call)):
                found, dynamic = _imports_of(child)
                if found or dynamic:
                    out.append((child.lineno, getattr(child, "end_lineno", child.lineno), found, dynamic))
            walk(child, nxt)

    walk(tree, False)
    return out


def _runtime_executors(maps, rel: str) -> set:
    cov = maps.cov(rel)
    if not cov:
        return set()
    acc: set = set()
    seen = set()
    for code in cov["l"].values():
        if code & 1:
            continue
        si = code >> 1
        if si in seen:
            continue
        seen.add(si)
        acc |= maps._nums(cov["s"][si])
    return acc


class Importers:
    def __init__(self, src: Path, maps, dialect_tests: dict):
        self.src = Path(src)
        self.maps = maps
        self.by_mod: dict[str, set] = defaultdict(set)
        self._build(dialect_tests)

    def _closure(self, starts, top) -> set:
        seen = set()
        stack = list(starts)
        while stack:
            mod = stack.pop()
            if mod in seen or not mod.startswith("sqlglot"):
                continue
            seen.add(mod)
            parts = mod.split(".")
            for i in range(1, len(parts)):
                stack.append(".".join(parts[:i]))
            stack.extend(top.get(mod, ()))
        return seen

    def _resolve(self, names, lazy_attrs, real_mods) -> set:
        """Turn import names into modules that actually get loaded.

        `from pkg import name` loads `pkg.name` when that is a module or a lazy
        attribute, and always loads `pkg` itself. A dotted prefix from an
        f-string (`sqlglot.typing.`) loads every module under it.
        """
        out = set()
        for name in names:
            if name.endswith("."):
                out.update(m for m in real_mods if m.startswith(name))
                continue
            if name in real_mods or name in lazy_attrs.values() or name.count(".") <= 1:
                # `sqlglot.helper` is a module. `sqlglot.helper.merge_ranges` is a
                # function pulled out of that module: load the module, not the name.
                if name in real_mods:
                    out.add(name)
                else:
                    parent, _, leaf = name.rpartition(".")
                    if parent in lazy_attrs and leaf in lazy_attrs[parent]:
                        out.add(lazy_attrs[parent][leaf])
                    elif parent:
                        out.add(parent)
                    else:
                        out.add(name)
            else:
                parent, _, leaf = name.rpartition(".")
                if parent in lazy_attrs and leaf in lazy_attrs[parent]:
                    out.add(lazy_attrs[parent][leaf])
                elif name in real_mods:
                    out.add(name)
                elif parent:
                    out.add(parent)
        return out

    def _method_loads(self, lazy_attrs, real_mods):
        """Per test file: list of (test numbers, modules that test's body loads)."""
        idx = json.loads((self.maps.root / "index.json").read_text())
        by_method = defaultdict(set)
        for i, test_id in enumerate(idx["tests"]):
            num = self.maps.num[i]
            if num < 0:
                continue
            parts = test_id.split("::")
            if len(parts) == 3:
                by_method[(parts[0], parts[1], parts[2])].add(num)
            elif len(parts) == 2:
                by_method[(parts[0], "", parts[1])].add(num)
        out = defaultdict(list)
        for py in (self.src / "tests").rglob("*.py"):
            rel = str(py.relative_to(self.src))
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            pkg_names = set()
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name == "sqlglot.optimizer":
                            pkg_names.add(alias.asname or alias.name.split(".")[-1])
                elif isinstance(node, ast.ImportFrom) and node.module in ("sqlglot", "sqlglot.optimizer"):
                    for alias in node.names:
                        if alias.name == "optimizer" or node.module == "sqlglot.optimizer":
                            pkg_names.add(alias.asname or alias.name)
            dialect_names = {m.split(".")[-1] for m in real_mods if m.startswith("sqlglot.dialects.")}
            classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
            funcs = [(None, n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            for cls in classes:
                # A helper on the class (cached_execute writing "duckdb") loads that
                # dialect for every test on the class. The load map misses the
                # spawned TPC-DS worker, which imports the dialect in the child.
                class_dialects = set()
                for sub in ast.walk(cls):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and sub.value in dialect_names:
                        class_dialects.add("sqlglot.dialects." + sub.value)
                if class_dialects:
                    nums = set()
                    for n in cls.body:
                        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            nums |= by_method.get((rel, cls.name, n.name), set())
                    if nums:
                        out[rel].append((nums, set(class_dialects)))
                for n in cls.body:
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        funcs.append((cls.name, n))
            for cls_name, fn in funcs:
                names = []
                calls_optimize = False
                for sub in ast.walk(fn):
                    found, _dyn = _imports_of(sub)
                    names.extend(found)
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)
                        and sub.func.attr == "optimize"
                        and isinstance(sub.func.value, ast.Name)
                        and sub.func.value.id in pkg_names
                    ):
                        calls_optimize = True
                if calls_optimize:
                    names.append("sqlglot.optimizer.optimize")
                if not names and not calls_optimize:
                    continue
                starts = self._resolve(names, lazy_attrs, real_mods)
                if calls_optimize:
                    starts.add(lazy_attrs.get("sqlglot.optimizer", {}).get("optimize", "sqlglot.optimizer.optimizer"))
                nums = by_method.get((rel, cls_name or "", fn.name), set())
                if nums and starts:
                    out[rel].append((nums, starts))
        return out

    def _build(self, dialect_tests: dict) -> None:
        src = self.src
        real = {}
        lazy_sites = []
        for py in (src / "sqlglot").rglob("*.py"):
            rel = str(py.relative_to(src))
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            real[_mod(rel)] = rel
        # optimizer __getattr__ table: attribute name -> module it imports
        lazy_attrs = defaultdict(dict)
        opt = src / "sqlglot" / "optimizer" / "__init__.py"
        if opt.exists():
            tree = ast.parse(opt.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.AnnAssign) and not isinstance(node, ast.Assign):
                    continue
                target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if node.targets else None)
                if not isinstance(target, ast.Name) or target.id != "_LAZY_ATTRS":
                    continue
                value = node.value
                if not isinstance(value, ast.Dict):
                    continue
                for key, val in zip(value.keys, value.values):
                    if not isinstance(key, ast.Constant) or not isinstance(val, ast.Tuple) or not val.elts:
                        continue
                    mod = val.elts[0]
                    if isinstance(mod, ast.Constant) and isinstance(mod.value, str):
                        lazy_attrs["sqlglot.optimizer"][key.value] = mod.value

        top = defaultdict(set)
        for mod, rel in real.items():
            try:
                tree = ast.parse((src / rel).read_text())
            except SyntaxError:
                continue
            names = []
            for node in _import_time(tree):
                found, _dyn = _imports_of(node)
                names.extend(found)
            top[mod] = self._resolve(names, lazy_attrs, set(real))
            for a, b, found, dynamic in _lazy_calls(tree):
                lazy_sites.append((rel, a, b, found, dynamic))

        test_imports = {}
        for py in (src / "tests").rglob("*.py"):
            rel = str(py.relative_to(src))
            try:
                tree = ast.parse(py.read_text())
            except SyntaxError:
                continue
            names = []
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if isinstance(node, ast.If) and _type_checking(node.test):
                    continue
                found, _dyn = _imports_of(node)
                names.extend(found)
                for sub in ast.walk(node):
                    if isinstance(sub, (ast.Import, ast.ImportFrom)):
                        f2, _d = _imports_of(sub)
                        names.extend(f2)
            test_imports[rel] = self._resolve(names, lazy_attrs, set(real))

        def loaded(starts):
            return self._closure(starts, top)

        by = self.by_mod
        for rel, starts in test_imports.items():
            nums = set(self.maps.file_tests(rel))
            if not nums:
                continue
            for mod in loaded(starts):
                by[mod] |= nums
        # An import or an optimizer.optimize() call inside one test loads that
        # module for that test only. The coverage map does not record these:
        # they run at import time inside the test, with no per-test id.
        for rel, method_loads in self._method_loads(lazy_attrs, set(real)).items():
            for nums, starts in method_loads:
                for mod in loaded(starts):
                    by[mod] |= nums
        for dmod, nums in dialect_tests.items():
            if not nums:
                continue
            for mod in loaded({dmod}):
                by[mod] |= set(nums)
        # The typing suite imports every typing submodule by walking the package.
        typing_tests = set(self.maps.file_tests("tests/test_typing.py"))
        for mod in real:
            if mod.startswith("sqlglot.typing"):
                by[mod] |= typing_tests
        for mod, rel in real.items():
            ran = _runtime_executors(self.maps, rel)
            if not ran:
                continue
            # A test that executed the module loaded everything that module loads.
            for other in loaded({mod}):
                by[other] |= ran
        for rel, a, b, found, dynamic in lazy_sites:
            targets = self._resolve(found, lazy_attrs, set(real))
            if dynamic and not targets:
                continue
            tests, _imp, hit = self.maps.region_tests(rel, a, b)
            if not tests:
                continue
            for mod in loaded(targets):
                by[mod] |= tests

    def tests(self, path: str):
        got = self.by_mod.get(_mod(path))
        if not got:
            return None
        return set(got)


_JSON = None


def tests_importing(path: str, src, maps, dialect_tests):
    """Tests that import `path`, from maps/importers.json. None if unknown.

    The file is the pinned commit's import graph. Building it at score time
    costs more than a second, so a missing file keeps the wide selection.
    `src` and `dialect_tests` are unused here; the map already includes them.
    """
    global _JSON
    if not str(path).startswith("sqlglot/"):
        return None
    if _JSON is None:
        p = Path(maps.root) / "importers.json"
        if not p.exists():
            _JSON = {}
        else:
            raw = json.loads(p.read_text())
            num = maps.num
            _JSON = {
                mod: {num[i] for i in idxs if isinstance(i, int) and 0 <= i < len(num) and num[i] >= 0}
                for mod, idxs in raw["mods"].items()
            }
    got = _JSON.get(_mod(path)) if _JSON else None
    return set(got) if got else None
