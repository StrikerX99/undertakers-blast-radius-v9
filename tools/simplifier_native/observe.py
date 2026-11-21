import ast
import atexit
import builtins
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import sys
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor

root = Path(os.environ["OPT_ROOT"])
out = Path(os.environ["OPT_OUT"])
index = -1
seen = {}
results = {}
source_hash = None


introspected = set()


def audit(event, args):
    if event != "object.__getattr__" or len(args) != 2:
        return
    obj, attr = args
    if (
        attr == "__code__"
        and getattr(obj, "__module__", None) == "sqlglot.optimizer.simplify"
    ):
        qname = getattr(obj, "__qualname__", "")
        if qname.startswith("Simplifier."):
            introspected.add(qname.split(".")[1])
    elif (
        attr == "f_code"
        and obj.f_globals.get("__name__") == "sqlglot.optimizer.simplify"
    ):
        introspected.add("*")


sys.addaudithook(audit)


def mark(key):
    seen.setdefault(key, set()).add(index)


builtins._br_opt_suite = mark


def finish():
    (out / (str(os.getpid()) + ".json")).write_text(
        json.dumps(
            [
                results,
                {k: sorted(v) for k, v in seen.items()},
                source_hash,
                sorted(introspected),
            ]
        )
    )


atexit.register(finish)


class Mark(ast.NodeTransformer):
    def visit_If(self, node):
        self.generic_visit(node)
        for field in ("body", "orelse"):
            items = getattr(node, field)
            if items:
                call = ast.Expr(
                    value=ast.Call(
                        func=ast.Name(id="_br_opt_suite", ctx=ast.Load()),
                        args=[
                            ast.Constant(
                                value=f"{node.lineno}:{node.col_offset}:{field}"
                            )
                        ],
                        keywords=[],
                    )
                )
                items.insert(0, ast.copy_location(call, items[0]))
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        global source_hash
        src = Path(self.path)
        if src.resolve() != root / "sqlglot/optimizer/simplify.py":
            raise ValueError("Wrong SQLGlot module")
        data = src.read_bytes()
        source_hash = hashlib.sha256(data).hexdigest()
        tree = ast.parse(data, str(src))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "Simplifier"
        )
        for method in cls.body:
            if isinstance(method, ast.FunctionDef):
                Mark().visit(method)
        return compile(ast.fix_missing_locations(tree), str(src), "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "sqlglot.optimizer.simplify":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            spec.loader = Loader(fullname, spec.origin)
            return spec


def run_case(i, fn, a, kw):
    global index
    from query_case import table_digest

    index = i
    try:
        value = fn(*a, **kw)
        results[i] = None if value is None else table_digest(value)
        return value
    finally:
        index = -1


def install():
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise ValueError("Native observer started too late")
    sys.meta_path.insert(0, Finder())
    original = ProcessPoolExecutor.submit

    def submit(pool, fn, *a, **kw):
        if fn.__module__ == "tests.test_executor" and fn.__name__ == "mp_execute":
            i = getattr(pool, "_br_next", 0)
            pool._br_next = i + 1
            return original(pool, run_case, i, fn, a, kw)
        return original(pool, fn, *a, **kw)

    ProcessPoolExecutor.submit = submit
