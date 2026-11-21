"""Describe plain inherited overrides and all source-visible noncall observations."""

import ast
from collections import defaultdict
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
import textwrap

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def toplevel_use_index(root):
    escaped = set()
    by_name = defaultdict(lambda: defaultdict(set))
    test_reflection = set()
    special = {
        "_build_dispatch",
        "_DISPATCH_CACHE",
        "_dispatch",
        "__dict__",
        "__subclasses__",
        "__code__",
        "__func__",
    }
    for prefix in ("sqlglot", "tests"):
        for f in sorted((root / prefix).rglob("*.py")):
            path = f.relative_to(root).as_posix()
            tree = ast.parse(f.read_bytes())
            parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            for n in ast.walk(tree):
                par = parent.get(n)
                if (
                    isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value.isidentifier()
                ):
                    escaped.add(n.value)
                if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
                    if not (isinstance(par, ast.Call) and par.func is n):
                        by_name[n.attr][path].add(n.lineno)
                    if prefix == "tests" and n.attr in special:
                        test_reflection.add(path)
                if (
                    prefix == "tests"
                    and isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Name)
                    and n.func.id in {"getattr", "hasattr", "vars", "dir"}
                ):
                    test_reflection.add(path)
                if (
                    prefix == "tests"
                    and isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr
                    in {
                        "getmembers",
                        "getattr_static",
                        "getsource",
                        "getsourcefile",
                        "signature",
                        "getfullargspec",
                    }
                ):
                    test_reflection.add(path)
    return escaped, by_name, sorted(test_reflection)


def ordinary_effects(fn):
    """Exclude direct global/class writes and process effects in either implementation."""
    body = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if not isinstance(body, ast.FunctionDef) or body.decorator_list:
        return False
    locals_ = {v.arg for v in ast.walk(body) if isinstance(v, ast.arg)}
    locals_.update(
        v.id
        for v in ast.walk(body)
        if isinstance(v, ast.Name) and isinstance(v.ctx, ast.Store)
    )
    readonly = {
        "re": {
            "sub",
            "escape",
            "fullmatch",
            "match",
            "search",
            "split",
            "subn",
            "findall",
        }
    }
    mutators = {
        "append",
        "extend",
        "insert",
        "add",
        "update",
        "clear",
        "pop",
        "popitem",
        "remove",
        "discard",
        "setdefault",
        "__setitem__",
        "__delitem__",
    }

    def root(v):
        while isinstance(v, (ast.Attribute, ast.Subscript, ast.Call)):
            v = v.func if isinstance(v, ast.Call) else v.value
        return v.id if isinstance(v, ast.Name) else None

    for n in ast.walk(body):
        if isinstance(
            n,
            (
                ast.Import,
                ast.ImportFrom,
                ast.Global,
                ast.Nonlocal,
                ast.AsyncFunctionDef,
                ast.With,
            ),
        ):
            return False
        if isinstance(n, ast.Attribute) and n.attr in {
            "__dict__",
            "__class__",
            "__globals__",
            "__subclasses__",
            "__mro__",
            "_DISPATCH_CACHE",
            "_classes",
        }:
            return False
        if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(
            n.ctx, (ast.Store, ast.Del)
        ):
            if root(n) not in locals_ or any(
                isinstance(v, ast.Attribute) and v.attr.isupper() for v in ast.walk(n)
            ):
                return False
        if isinstance(n, ast.Call):
            head = root(n.func)
            if head in {
                "exec",
                "eval",
                "globals",
                "setattr",
                "delattr",
                "open",
                "compile",
                "__import__",
                "sys",
                "os",
                "subprocess",
                "pathlib",
                "random",
                "socket",
                "time",
                "logging",
            }:
                return False
            if isinstance(n.func, ast.Attribute):
                if head in readonly and n.func.attr not in readonly[head]:
                    return False
                if n.func.attr in mutators and (
                    head not in locals_
                    or any(
                        isinstance(v, ast.Attribute) and v.attr.isupper()
                        for v in ast.walk(n.func)
                    )
                ):
                    return False
    return True


def build(root, leaf_map, output):
    from importlib import import_module

    root, leaf_map, output = (Path(p).resolve() for p in (root, leaf_map, output))
    pin = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if pin != PIN:
        raise ValueError(pin)
    sys.path.insert(0, str(root))
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser

    if (
        Path(inspect.getsourcefile(Generator)).resolve()
        != root / "sqlglot/generator.py"
    ):
        raise ValueError("Wrong project on import path")
    data = json.loads(leaf_map.read_bytes())
    strings, escapes, reflected = toplevel_use_index(root)
    methods = {}
    for path, name in sorted(data["leaf"].items()):
        if not path.startswith(("sqlglot/generators/", "sqlglot/parsers/")):
            continue
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != data["hashes"][path]:
            raise ValueError(path)
        c = getattr(import_module(path[:-3].replace("/", ".")), name)
        if type(c) is not type or not issubclass(c, (Parser, Generator)):
            continue
        table = {}
        for sub, fn in vars(c).items():
            if sub.startswith("__") or not inspect.isfunction(fn) or sub in strings:
                continue
            if (
                inspect.getsourcefile(fn) != str(root / path)
                or fn.__qualname__ != f"{name}.{sub}"
            ):
                continue
            ancestor = next(
                (x.__dict__[sub] for x in c.__mro__[1:] if sub in x.__dict__), None
            )
            if not inspect.isfunction(ancestor) or not inspect.getsourcefile(ancestor):
                continue
            if (
                not Path(inspect.getsourcefile(ancestor))
                .resolve()
                .is_relative_to(root / "sqlglot")
            ):
                continue
            if not (ordinary_effects(fn) and ordinary_effects(ancestor)):
                continue
            table[sub] = {p: sorted(ns) for p, ns in escapes.get(sub, {}).items()}
        if table:
            methods[path] = table
    output.write_text(
        json.dumps(
            {"commit": pin, "methods": methods, "reflective_test_files": reflected},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
