"""Trace the normalized tokens compared at literal lookahead call sites on the pinned suite."""

import ast
import atexit
import builtins
from collections import defaultdict
import importlib.abc
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import sys
import threading
import types


def literal_call(node):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_match_text_seq"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
        and 1 <= len(node.args) <= 8
        and all(
            isinstance(arg, ast.Constant) and type(arg.value) is str
            for arg in node.args
        )
        and all(
            k.arg == "advance"
            and isinstance(k.value, ast.Constant)
            and type(k.value.value) is bool
            for k in node.keywords
        )
    )


def install():
    import profile_pool

    profile_pool.install()
    root = Path(os.environ["BR_LEX_ROOT"])
    state = Path(os.environ["BR_LEX_STATE"])
    pid = os.getpid()
    (state / f"{pid}.start").touch()
    main = threading.get_ident()
    local = threading.local()
    targets = {}
    audited = {}
    rows = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    unsafe = defaultdict(set)
    compiled = set()
    pool = [False]

    def owner():
        if threading.get_ident() != main:
            return "*"
        context = profile_pool.context()
        name, _, phase = context.rpartition(" (")
        if not name:
            return "*"
        return name if phase == "call)" else name.partition("::")[0] + "::*"

    def call(site, fn, *args, **kwargs):
        key = owner()
        if (
            not pool[0]
            and os.getpid() != int(os.environ["BR_POOL_OWNER"])
            and key != "*"
        ):
            pool[0] = True
            (state / f"{os.getpid()}.pool").touch()
        previous = getattr(local, "event", None)
        valid = (
            type(fn) is types.MethodType
            and fn.__func__ is audited.get("_match_text_seq")
            and type(fn.__self__).__getattribute__ is object.__getattribute__
            and getattr(type(fn.__self__), "_advance", None) is audited.get("_advance")
            and getattr(type(fn.__self__), "_retreat", None) is audited.get("_retreat")
        )
        if not valid:
            unsafe[site].add(key)
        local.event = [site, key, 0] if valid else None
        try:
            return fn(*args, **kwargs)
        finally:
            local.event = previous

    def compare(actual, expected):
        event = getattr(local, "event", None)
        if event:
            site, test, pos = event
            event[2] += 1
            if type(actual) is not str:
                unsafe[site].add(test)
            elif actual and len(actual) <= 48 and actual.isascii():
                row = rows[site][pos]
                # Saturated positions are opaque. Retain the other argument positions.
                if "\x00" not in row:
                    row[actual].add(test)
                    if len(row) > 96:
                        row.clear()
                        row["\x00"].add("*")
        return actual == expected

    class Calls(ast.NodeTransformer):
        def __init__(self, name):
            self.name = name

        def visit_Call(self, node):
            self.generic_visit(node)
            if not literal_call(node):
                return node
            key = f"{self.name}:{node.lineno}:{node.col_offset}"
            compiled.add(key)
            return ast.copy_location(
                ast.Call(
                    ast.Name("_br_lex_call", ast.Load()),
                    [ast.Constant(key), node.func, *node.args],
                    node.keywords,
                ),
                node,
            )

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tr = ast.parse(Path(self.path).read_bytes(), self.path)
            path = Path(self.path).relative_to(root).as_posix()
            Calls(path).visit(tr)
            if fullname == "sqlglot.parser":
                fn = next(
                    n
                    for c in tr.body
                    if isinstance(c, ast.ClassDef) and c.name == "Parser"
                    for n in c.body
                    if isinstance(n, ast.FunctionDef) and n.name == "_match_text_seq"
                )
                loop = next(n for n in fn.body if isinstance(n, ast.For))
                comparison = loop.body[0].test.values[1]
                assert ast.unparse(comparison) == "self._curr.text.upper() == text"
                loop.body[0].test.values[1] = ast.copy_location(
                    ast.Call(
                        ast.Name("_br_lex_compare", ast.Load()),
                        [comparison.left, comparison.comparators[0]],
                        [],
                    ),
                    comparison,
                )
            return compile(
                ast.fix_missing_locations(tr), self.path, "exec", dont_inherit=True
            )

        def exec_module(self, module):
            super().exec_module(module)
            if module.__name__ == "sqlglot.parser":
                for n in ("_match_text_seq", "_advance", "_retreat"):
                    audited[n] = getattr(module.Parser, n)

    for p in [root / "sqlglot/parser.py", *(root / "sqlglot/parsers").glob("*.py")]:
        if p.name != "__init__.py":
            targets[".".join(p.relative_to(root).with_suffix("").parts)] = str(p)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in targets:
                return importlib.util.spec_from_file_location(
                    fullname,
                    targets[fullname],
                    loader=Loader(fullname, targets[fullname]),
                )

    builtins._br_lex_call = call
    builtins._br_lex_compare = compare
    sys.meta_path.insert(0, Finder())

    @atexit.register
    def save():
        (state / f"{pid}.end").write_text(
            json.dumps(
                [
                    sorted(compiled),
                    {k: sorted(v) for k, v in unsafe.items()},
                    {
                        k: {
                            str(p): {t: sorted(s) for t, s in v.items()}
                            for p, v in parts.items()
                        }
                        for k, parts in rows.items()
                    },
                ],
                separators=(",", ":"),
            )
        )
