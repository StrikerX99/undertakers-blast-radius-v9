"""Describe ordinary SQLGlot dialect parser/generator classes at the pinned revision."""

import ast
from collections import defaultdict
import hashlib
from importlib import import_module
import inspect
import json
from pathlib import Path
import subprocess
import sys

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def build(root, dependencies, output):
    root = Path(root).resolve()
    data = json.loads(Path(dependencies).read_bytes())
    if (
        data["commit"] != PIN
        or subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
    ):
        raise ValueError("Wrong pinned sources")
    sys.path.insert(0, str(root))
    from sqlglot.generator import Generator
    from sqlglot.parser import Parser

    if Path(inspect.getsourcefile(Parser)).resolve() != root / "sqlglot/parser.py":
        raise ValueError("A different SQLGlot installation was imported")
    classes = {}
    # Only ordinary Python inheritance is supported. Generators and parsers
    # can have descendants in dialect submodules. All per-group module opens
    # and full-suite reads are unioned across these descendants at runtime.
    for path, digest in data["hashes"].items():
        if not path.startswith(
            ("sqlglot/generators/", "sqlglot/parsers/")
        ) or path.endswith("/__init__.py"):
            continue
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError(path)
        mod = import_module(path[:-3].replace("/", "."))
        for v in vars(mod).values():
            if not (
                type(v) is type
                and v.__module__ == mod.__name__
                and "." not in v.__qualname__
                and issubclass(v, (Parser, Generator))
            ):
                continue
            if any(
                k in vars(cls)
                for cls in v.__mro__
                if cls is not object
                for k in (
                    "__init_subclass__",
                    "__getattr__",
                    "__getattribute__",
                    "__setattr__",
                    "__delattr__",
                )
            ):
                continue
            classes[(path, v.__name__)] = v
    opaque = set()
    for path, sha in data["hashes"].items():
        tree = ast.parse((root / path).read_bytes())
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef) and (n.keywords or n.decorator_list):
                for base in n.bases:
                    if isinstance(base, ast.Name):
                        opaque.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        opaque.add(base.attr)
    out = defaultdict(dict)
    for (p, n), c in classes.items():
        family = [(path, cl) for (path, cl), v in classes.items() if issubclass(v, c)]
        if any(cl in opaque for path, cl in family):
            continue
        # Each descendant also causes its concrete dialect to be registered.
        related = {p}
        for path, cl in family:
            if path in data["hashes"]:
                related.add(path)
            d = "sqlglot/dialects/" + path.rsplit("/", 1)[-1]
            if d in data["hashes"]:
                related.add(d)
        out[p][n] = sorted(related)
    # Track when every static use of a private helper is an immediate call
    # through self in its own class. Computed attribute lookups, literals naming
    # the helper, explicit reflection and public namespace consumers are handled
    # conservatively by the selector.
    names = {
        method
        for cls in classes.values()
        for method in vars(cls)
        if method.startswith("_") and not method.startswith("__")
    }
    literals = set()
    unsafe = defaultdict(set)
    direct = defaultdict(lambda: defaultdict(set))
    for section in ("tests", "sqlglot"):
        for f in sorted((root / section).rglob("*.py")):
            source = f.read_bytes()
            tree = ast.parse(source)
            parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            path = f.relative_to(root).as_posix()
            for n in ast.walk(tree):
                if (
                    isinstance(n, ast.Constant)
                    and isinstance(n.value, str)
                    and n.value in names
                ):
                    literals.add(n.value)
                if isinstance(n, ast.Attribute) and n.attr in names:
                    par = parent[n]
                    if (
                        isinstance(par, ast.Call)
                        and par.func is n
                        and isinstance(n.value, ast.Name)
                        and n.value.id == "self"
                    ):
                        direct[n.attr][path].add((n.lineno, n.col_offset))
                    else:
                        unsafe[n.attr].add(path)
    private = {
        p: {
            c: {
                n: sorted(direct[n][p])
                for n, fn in vars(cls).items()
                if n in names
                and n not in literals
                and not unsafe[n]
                and set(direct[n]) == {p}
                and inspect.isfunction(fn)
                and not any(n in vars(a) for a in cls.__mro__[1:])
            }
            for (q, c), cls in classes.items()
            if q == p
        }
        for p in out
    }
    Path(output).write_text(
        json.dumps(
            {"commit": PIN, "classes": out, "private_internal": private},
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
