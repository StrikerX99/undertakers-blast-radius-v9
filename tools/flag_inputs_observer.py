"""Attribute pinned dialect scalar reads and record inputs to string normalization."""

import ast
import atexit
from collections import defaultdict
import importlib._bootstrap_external as ext
import json
import os
from pathlib import Path
import string
import sys
import threading

import flag_namespace_observer
import profile_pool


def normalization_sites(path):
    tree = ast.parse(path.read_bytes())
    found = {}
    for n in ast.walk(tree):
        if not (isinstance(n, ast.IfExp) and isinstance(n.test, ast.Attribute)):
            continue
        if not all(
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Attribute)
            and not a.keywords
            for a in (n.body, n.orelse)
        ):
            continue
        a, b = n.body, n.orelse
        if b.func.attr == "translate":
            a, b = b, a
        if (
            a.func.attr != "translate"
            or b.func.attr not in ("lower", "upper")
            or len(a.args) != 1
            or b.args
            or not isinstance(a.args[0], ast.Name)
        ):
            continue
        source = a.func.value
        if not (
            isinstance(source, ast.Attribute)
            and source.attr == "this"
            and isinstance(source.value, ast.Name)
            and ast.dump(source) == ast.dump(b.func.value)
        ):
            continue
        found[n.test.lineno] = (n.test.attr, source.value.id, a.args[0].id, b.func.attr)
    return found


def install():
    root = Path(os.environ["BR_DIALECT_ROOT"])
    state = Path(os.environ["BR_DIALECT_STATE"])
    catalog = json.loads((state / "catalog.json").read_bytes())
    expected = {
        path + ":" + line: name
        for path, rows in catalog["sites"].items()
        for line, (_, name) in rows.items()
    }
    profile_pool.install()
    source = root / "sqlglot/dialects/dialect.py"
    sites = normalization_sites(source)
    imports = 0
    original = ext._LoaderBasics.exec_module

    def track(self, module):
        nonlocal imports
        imports += 1
        try:
            return original(self, module)
        finally:
            imports -= 1

    ext._LoaderBasics.exec_module = track
    rows = defaultdict(lambda: defaultdict(set))
    main = threading.get_ident()
    tables = {
        "lower": str.maketrans(string.ascii_uppercase, string.ascii_lowercase),
        "upper": str.maketrans(string.ascii_lowercase, string.ascii_uppercase),
    }

    def owner():
        ctx = profile_pool.context()
        who, _, phase = ctx.rpartition(" (")
        if imports or not who or threading.get_ident() != main:
            return "*"
        return who if phase == "call)" else who.partition("::")[0] + "::*"

    raw_events, literal_events = set(), set()

    def literal_reader(cls, key, frame):
        literal_events.add((cls.__module__, cls.__qualname__, str(frame.f_code.co_filename), frame.f_code.co_qualname, frame.f_lineno, key))

    def namespace_reader(sites, frame):
        who = owner()
        for site in sites:
            rows[site]["read"].add(who)
            if site.startswith("sqlglot/dialects/dialect.py:"):
                raw_events.add((str(frame.f_code.co_filename),frame.f_code.co_qualname,frame.f_lineno,who))
    def observed(site):
        frame = sys._getframe(2)
        mode = "read"
        shape = (
            sites.get(frame.f_lineno)
            if frame.f_code.co_filename == str(source)
            else None
        )
        if shape and shape[0] == expected[site]:
            _, name, table, op = shape
            mod = sys.modules.get("sqlglot.expressions.core")
            obj = frame.f_locals.get(name)
            mapping = frame.f_globals.get(table)
            if (
                mod is not None
                and type(obj) is getattr(mod, "Identifier", None)
                and type(obj).__getattribute__ is object.__getattribute__
            ):
                prop = getattr(type(obj), "this", None)
                if (
                    isinstance(prop, property)
                    and prop.fget is mod.Expression.this.fget
                    and type(obj.args) is dict
                ):
                    s = obj.args.get("this")
                    if (
                        type(s) is str
                        and s.isascii()
                        and type(mapping) is dict
                        and mapping == tables[op]
                    ):
                        mode = "ascii_string"
        rows[site][mode].add(owner())
        return True

    flag_namespace_observer.install(observed, namespace_reader, literal_reader)

    @atexit.register
    def save():
        (state / (str(os.getpid()) + ".literal")).write_text(json.dumps(sorted(literal_events)))
        (state / (str(os.getpid()) + ".namespace")).write_text(json.dumps(sorted(raw_events)))
        (state / (str(os.getpid()) + ".reads")).write_text(
            json.dumps(
                {k: {m: sorted(x) for m, x in v.items()} for k, v in rows.items()}
            )
        )
