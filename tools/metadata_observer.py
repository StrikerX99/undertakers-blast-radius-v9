"""Observe concrete keys and wildcard consumers of SQLGlot's native type metadata."""

import ast
import atexit
import builtins
from collections import defaultdict
import importlib.abc
import importlib.machinery
import importlib._bootstrap_external as ext
import json
import os
from pathlib import Path
import sys
import threading

import profile_pool

FIELD = "EXPRESSION_METADATA"


def install():
    root = Path(os.environ["BR_META_ROOT"])
    state = Path(os.environ["BR_EXPR_STATE"])
    targets = set(json.loads((state / "targets.json").read_bytes()))
    process = str(os.getpid())
    (state / (process + ".start")).touch()
    main = threading.get_ident()
    profile_pool.install()
    reads, created = defaultdict(set), set()
    imports = 0
    native_exec = ext._LoaderBasics.exec_module

    def importing(loader, module):
        nonlocal imports
        imports += 1
        try:
            return native_exec(loader, module)
        finally:
            imports -= 1

    ext._LoaderBasics.exec_module = importing

    def record(key=None):
        name = key.__name__ if isinstance(key, type) else "*"
        ctx = profile_pool.context()
        who, _, phase = ctx.rpartition(" (")
        # Setup/teardown can belong to a process-wide or session-scoped fixture.
        # Retain unowned reads, imports and every non-call phase across all tests.
        if imports or not who or phase != "call)" or threading.get_ident() != main:
            who = "*"
        reads[name].add(who)

    class Table(dict):
        def __getitem__(self, key):
            record(key)
            return dict.__getitem__(self, key)

        def get(self, key, *args):
            record(key)
            return dict.get(self, key, *args)

        def __contains__(self, key):
            record(key)
            return dict.__contains__(self, key)

    def opaque(name):
        op = getattr(dict, name)

        def use(self, *args, **kwargs):
            record()
            return op(self, *args, **kwargs)

        return use

    for name in (
        "__iter__",
        "__len__",
        "__repr__",
        "__eq__",
        "__ne__",
        "keys",
        "values",
        "items",
        "copy",
        "__or__",
        "__ror__",
        "__ior__",
        "__setitem__",
        "__delitem__",
        "__reversed__",
        "clear",
        "pop",
        "popitem",
        "setdefault",
        "update",
        "__sizeof__",
        "__reduce__",
        "__reduce_ex__",
    ):
        setattr(Table, name, opaque(name))

    def raw(table):
        if type(table) is not Table:
            raise ValueError("Unexpected unobserved metadata propagation")
        return {k: v for k, v in dict.items(table)}

    def register(value, name):
        if type(value) is not dict:
            raise ValueError("Unexpected metadata declaration")
        created.add(name)
        return Table(value)

    def call(name, value, *args):
        return ast.copy_location(
            ast.Call(ast.Name(name, ast.Load()), [value, *args], []), value
        )

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_bytes(), self.path)
            count = 0
            for owner in [tree] + [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                for stmt in owner.body:
                    dest = (
                        stmt.target
                        if isinstance(stmt, ast.AnnAssign)
                        else stmt.targets[0]
                        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1
                        else None
                    )
                    if not isinstance(dest, ast.Name) or dest.id != FIELD:
                        continue
                    value = stmt.value
                    if isinstance(value, ast.Dict):
                        # Every dictionary produced by an inherited module spread
                        # is registered anew. Preserve the shared inner values.
                        for i, (k, v) in enumerate(zip(value.keys, value.values)):
                            if k is None and isinstance(v, ast.Name) and FIELD in v.id:
                                value.values[i] = call("_br_meta_raw", v)
                    elif (
                        isinstance(value, ast.Call)
                        and not value.args
                        and not value.keywords
                        and isinstance(value.func, ast.Attribute)
                        and value.func.attr == "copy"
                        and isinstance(value.func.value, ast.Name)
                        and value.func.value.id == FIELD
                    ):
                        value = call("_br_meta_raw", value.func.value)
                    else:
                        raise ValueError("Unknown metadata declaration")
                    name = fullname + ("." + owner.name if owner is not tree else "")
                    stmt.value = call("_br_metadata", value, ast.Constant(name))
                    count += 1
            if not count:
                raise ValueError("Missing expected metadata definition")
            return compile(ast.fix_missing_locations(tree), self.path, "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name not in targets:
                return None
            s = importlib.machinery.PathFinder.find_spec(name, path)
            if (
                not s
                or not s.origin
                or not Path(s.origin).resolve().is_relative_to(root)
            ):
                raise ValueError("Unexpected origin")
            s.loader = Loader(name, s.origin)
            return s

    builtins._br_metadata, builtins._br_meta_raw = register, raw
    sys.meta_path.insert(0, Finder())

    @atexit.register
    def finish():
        (state / (process + ".reads")).write_text(
            json.dumps(
                {
                    "created": sorted(created),
                    "reads": {k: sorted(v) for k, v in reads.items()},
                }
            )
        )
