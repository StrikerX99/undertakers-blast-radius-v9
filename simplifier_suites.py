"""Select the pinned TPC-DS executor only if a changed Simplifier branch was reached."""

import ast
from functools import lru_cache
import hashlib
import json
from pathlib import Path
from types import CodeType

from dormant_settings import _sources
from optimizer_replay import collection
from query_case import CASE

PATH = "sqlglot/optimizer/simplify.py"


@lru_cache(maxsize=1)
def evidence():
    return json.loads(Path(__file__).with_name("simplifier-suites.json").read_bytes())


def equal_on_reached_suites(a, b, unentered, equal):
    """Mask a suite only when the same guard protects both versions."""
    if equal(a, b):
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.If) and equal(a.test, b.test):
        return all(
            f"{a.lineno}:{a.col_offset}:{field}" in unentered
            or equal_on_reached_suites(
                getattr(a, field), getattr(b, field), unentered, equal
            )
            for field in ("body", "orelse")
        )
    if isinstance(a, list):
        return len(a) == len(b) and all(
            equal_on_reached_suites(x, y, unentered, equal) for x, y in zip(a, b)
        )
    if isinstance(a, ast.AST):
        return all(
            equal_on_reached_suites(x, getattr(b, name), unentered, equal)
            for name, x in ast.iter_fields(a)
        )
    return False


def changed_method(before, after, equal):
    if len(before.body) != len(after.body):
        return
    where = [(x, y) for x, y in zip(before.body, after.body) if not equal(x, y)]
    if len(where) != 1:
        return
    cls, edited = where[0]
    if not (
        isinstance(cls, ast.ClassDef)
        and isinstance(edited, ast.ClassDef)
        and cls.name == "Simplifier"
        and all(
            equal(getattr(cls, k), getattr(edited, k))
            for k in cls._fields
            if k != "body"
        )
        and len(cls.body) == len(edited.body)
    ):
        return
    delta = [(x, y) for x, y in zip(cls.body, edited.body) if not equal(x, y)]
    if len(delta) != 1:
        return
    a, b = delta[0]
    if not (
        type(a) is type(b) is ast.FunctionDef
        and a.name[0] != "_"
        and all(equal(getattr(a, k), getattr(b, k)) for k in a._fields if k != "body")
        and ast.get_docstring(a, clean=False) == ast.get_docstring(b, clean=False)
    ):
        return
    return a, b


def code_layout(tree, name):
    """An unreachable edit can still change scope, closures or whether a function yields."""
    module = compile(tree, PATH, "exec")
    (cls,) = (
        c
        for c in module.co_consts
        if isinstance(c, CodeType) and c.co_name == "Simplifier"
    )
    (fn,) = (c for c in cls.co_consts if isinstance(c, CodeType) and c.co_name == name)

    def layout(c):
        return (
            c.co_flags,
            c.co_varnames,
            c.co_cellvars,
            c.co_freevars,
            tuple(
                (q.co_name, layout(q)) for q in c.co_consts if isinstance(q, CodeType)
            ),
        )

    # A removed guard can eliminate constants from the compiler's pool. Any
    # reachable expression is compared structurally. Disallow fresh constants
    # as an additional check against new import-time allocation or folding.
    constants = {
        (type(k), repr(k)) for k in fn.co_consts if not isinstance(k, CodeType)
    }
    return layout(fn), constants


def verify(selector, changes, selected, apply, equal):
    case = selector.by_name.get(CASE)
    if (
        case is None
        or case not in selected
        or case in selector.unknown
        or len(changes) != 1
    ):
        return selected
    change = changes[0]
    if change["old"] != PATH or change["new"] != PATH or PATH in selector.files:
        return selected
    profile = evidence()
    orig, tree = selector.source(PATH)
    if (
        profile["pin"] != selector.mapping["commit"]
        or profile["sha"] != hashlib.sha256(orig.encode()).hexdigest()
    ):
        return selected
    fresh = ast.parse(apply(orig, change)[0])
    pair = changed_method(tree, fresh, equal)
    if pair is None:
        return selected
    old, new = pair
    if "*" in profile["introspected"] or old.name in profile["introspected"]:
        return selected

    def safe_decorator(d):
        return (isinstance(d, ast.Name) and d.id == "annotate_types_on_change") or (
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Name)
            and d.func.id == "catch"
            and not d.keywords
            and all(isinstance(x, ast.Name) for x in d.args)
        )

    if not all(safe_decorator(d) for d in old.decorator_list):
        return selected
    if not equal_on_reached_suites(
        old.body, new.body, frozenset(profile["unentered"]), equal
    ):
        return selected
    old_layout, old_constants = code_layout(tree, old.name)
    new_layout, new_constants = code_layout(fresh, old.name)
    if old_layout != new_layout or not new_constants <= old_constants:
        return selected
    if any(
        isinstance(
            v,
            (
                ast.Global,
                ast.Nonlocal,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
                ast.NamedExpr,
                ast.ClassDef,
                ast.Lambda,
            ),
        )
        or (isinstance(v, ast.FunctionDef) and v is not old and v is not new)
        for fn in pair
        for v in ast.walk(fn)
    ):
        return selected
    collection_data = collection()
    site = f"{min([old.lineno, *[d.lineno for d in old.decorator_list]])}:{old.name}"
    if collection_data["revision"] != profile["pin"] or site in collection_data[
        "sites"
    ].get(PATH, ()):
        return selected
    source_data = _sources()
    if (
        source_data["commit"] != profile["pin"]
        or source_data["unopened"].get(PATH) != profile["sha"]
    ):
        return selected
    return selected - {case}
