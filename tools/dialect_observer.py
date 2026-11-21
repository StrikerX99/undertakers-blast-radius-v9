"""Track scalar attribute resolution and opaque namespace access across processes."""

import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys


def declaration(statement):
    if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
        target = statement.targets[0]
    elif isinstance(statement, ast.AnnAssign):
        target = statement.target
    else:
        return None
    if not isinstance(target, ast.Name) or not target.id.isupper():
        return None
    if not isinstance(statement.value, ast.Constant) or type(
        statement.value.value
    ) not in (bool, str, int):
        return None
    return target.id


def classes(tree, base):
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.decorator_list:
            continue
        if node.name == "Dialect" and base:
            yield node
        elif (
            len(node.bases) == 1
            and isinstance(node.bases[0], ast.Name)
            and not node.keywords
        ):
            yield node


def install(reference_reader=None):
    root = Path(os.environ["BR_DIALECT_ROOT"])
    state = Path(os.environ["BR_DIALECT_STATE"])
    catalog = json.loads((state / "catalog.json").read_bytes())
    identity = str(os.getpid())
    (state / (identity + ".start")).touch()
    created, observed = set(), set()
    sites_by_class = {}
    for path, rows in catalog["sites"].items():
        for line, (cls, name) in rows.items():
            sites_by_class.setdefault((path[:-3].replace("/", "."), cls), {})[name] = (
                f"{path}:{line}"
            )

    class Observed:
        def __init__(self, value, site):
            self.value, self.site = value, site

        def __set_name__(self, owner, name):
            meta = type(owner)
            if (
                meta.__module__ == "sqlglot.dialects.dialect"
                and meta.__name__ == "_Dialect"
            ):
                created.add(self.site)

        def __get__(self, obj, typ=None):
            if reference_reader is None or not reference_reader(self.site):
                observed.add(self.site)
            return self.value

    def class_attr(cls, name):
        original = type.__getattribute__(cls, name)
        if name != "__dict__":
            return original
        key = (
            type.__getattribute__(cls, "__module__"),
            type.__getattribute__(cls, "__qualname__"),
        )
        rows = sites_by_class.get(key)
        if not rows:
            return original
        frame = sys._getframe(2)
        known = (
            catalog["read_keys"].get(str(frame.f_lineno))
            if frame.f_code.co_filename == str(root / "sqlglot/dialects/dialect.py")
            and frame.f_code.co_qualname == "_Dialect.__new__"
            else None
        )
        observed.update(
            rows.values() if known is None else (rows[k] for k in known if k in rows)
        )
        return original

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            path = Path(self.path).relative_to(root).as_posix()
            tree = ast.parse(Path(self.path).read_bytes(), self.path)
            rows = catalog["sites"].get(path, {})
            if path == "sqlglot/dialects/dialect.py":
                cls = next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == "_Dialect"
                )
                method = ast.parse(
                    "def __getattribute__(self, name):\n return _br_class_attr(self, name)"
                ).body[0]
                cls.body.append(method)
            for cls in classes(tree, path == "sqlglot/dialects/dialect.py"):
                for stmt in cls.body:
                    name = declaration(stmt)
                    if list(rows.get(str(stmt.lineno), [])) == [cls.name, name]:
                        site = f"{path}:{stmt.lineno}"
                        stmt.value = ast.copy_location(
                            ast.Call(
                                ast.Name("_br_observed_attr", ast.Load()),
                                [stmt.value, ast.Constant(site)],
                                [],
                            ),
                            stmt.value,
                        )
            return compile(ast.fix_missing_locations(tree), self.path, "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if not name.startswith("sqlglot.dialects."):
                return
            s = importlib.machinery.PathFinder.find_spec(name, path)
            if (
                s
                and s.origin
                and Path(s.origin).is_relative_to(root)
                and Path(s.origin).relative_to(root).as_posix() in catalog["sites"]
            ):
                s.loader = Loader(name, s.origin)
            return s

    builtins._br_class_attr, builtins._br_observed_attr = class_attr, Observed
    sys.meta_path.insert(0, Finder())

    @atexit.register
    def save():
        (state / (identity + ".json")).write_text(
            json.dumps([sorted(created), sorted(observed)])
        )
