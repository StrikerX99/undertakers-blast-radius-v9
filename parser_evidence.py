"""Use baseline parser call footprints and observed control flow for local body edits."""

import ast
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import types

from parser_sites import guard, optional, site


@lru_cache(maxsize=1)
def profile():
    return json.loads(Path(__file__).with_name("parser-evidence.json").read_bytes())


def _pick_methods(old, new, equal):
    if len(old.body) != len(new.body):
        return None
    pairs = {}
    for a, b in zip(old.body, new.body):
        if (
            isinstance(a, ast.ClassDef)
            and isinstance(b, ast.ClassDef)
            and a.name == b.name == "Parser"
        ):
            if len(a.body) != len(b.body) or not all(
                equal(getattr(a, f), getattr(b, f)) for f in a._fields if f != "body"
            ):
                return None
            for fn, edited in zip(a.body, b.body):
                if equal(fn, edited):
                    continue
                if not (
                    isinstance(fn, ast.FunctionDef)
                    and isinstance(edited, ast.FunctionDef)
                    and not fn.decorator_list
                    and all(
                        equal(getattr(fn, f), getattr(edited, f))
                        for f in fn._fields
                        if f != "body"
                    )
                ):
                    return None
                # A moved docstring is directly observable via class introspection.
                if ast.get_docstring(fn, clean=False) != ast.get_docstring(
                    edited, clean=False
                ):
                    return None
                pairs[fn.name] = fn, edited
        elif not equal(a, b):
            return None
    return pairs or None


def _effects(old, new, sites, equal):
    """Allow any combination of covered guarded-block removals and optional argument omissions."""
    used = set()

    def walk(a, b):
        if equal(a, b):
            return True
        if isinstance(a, list) and isinstance(b, list):
            i = j = 0
            while i < len(a):
                if j < len(b) and walk(a[i], b[j]):
                    i += 1
                    j += 1
                elif isinstance(a[i], ast.If) and guard(a[i]) and site(a[i]) in sites:
                    used.add(site(a[i]))
                    i += 1
                else:
                    return False
            return j == len(b)
        if isinstance(a, ast.AST) and type(a) is type(b):
            opt = optional(a)
            if (
                opt is not None
                and site(a) in sites
                and equal(a.func, b.func)
                and equal(a.args, b.args)
                and equal([k for k in a.keywords if k is not opt], b.keywords)
            ):
                used.add(site(a))
                return True
            return all(walk(getattr(a, f), getattr(b, f)) for f in a._fields)
        return False

    return used if walk(old, new) else None


def _compiled_scopes(source, name):
    co = compile(source, name, "exec", dont_inherit=True)
    root = next(
        c
        for c in co.co_consts
        if isinstance(c, types.CodeType) and c.co_name == "Parser"
    )
    return {
        c.co_name: (c.co_flags, c.co_varnames, c.co_freevars, c.co_cellvars)
        for c in root.co_consts
        if isinstance(c, types.CodeType)
    }


def verify(selector, changes, selected, apply, equal):
    name = "sqlglot/parser.py"
    if (
        not selected
        or len(changes) != 1
        or changes[0]["old"] != name
        or changes[0]["new"] != name
        or selector.unknown
        or name in selector.files
    ):
        return selected
    d = profile()
    if d["pin"] != selector.mapping["commit"]:
        return selected
    raw, tree = selector.source(name)
    if hashlib.sha256(raw.encode()).hexdigest() != d["source"]:
        return selected
    new, removed, inserted = apply(raw, changes[0])
    hits = removed | inserted
    if not hits:
        return selected
    klass = next(
        (x for x in tree.body if isinstance(x, ast.ClassDef) and x.name == "Parser"),
        None,
    )
    if klass is None:
        return selected
    touched = [
        (i, f)
        for i, f in enumerate(klass.body)
        if isinstance(f, ast.FunctionDef)
        and any(f.lineno <= n <= f.end_lineno for n in hits)
    ]
    if not touched:
        return selected
    _, first = touched[0]
    _, last = touched[-1]
    if first.decorator_list or last.decorator_list:
        return selected
    lines = raw.splitlines(keepends=True)
    prefix, suffix = (
        "".join(lines[: first.lineno - 1]),
        "".join(lines[last.end_lineno :]),
    )
    if (
        not new.startswith(prefix)
        or not new.endswith(suffix)
        or len(new) < len(prefix) + len(suffix)
    ):
        return selected
    # Exact unchanged surrounding bytes certify that the reconstructed branch
    # contains the entire patch. Parse only this slice of the 11k-line parser.
    old_src = "class Parser:\n" + "".join(lines[first.lineno - 1 : last.end_lineno])
    new_src = (
        "class Parser:\n"
        + new[len(prefix) : len(new) - len(suffix) if suffix else None]
    )
    try:
        partial = ast.parse(old_src)
        ast.increment_lineno(partial, first.lineno - 2)
        pairs = _pick_methods(partial, ast.parse(new_src), equal)
        if not pairs:
            return selected
        a, b = _compiled_scopes(old_src, name), _compiled_scopes(new_src, name)
    except (StopIteration, ValueError, SyntaxError, RecursionError):
        return selected
    if any(a.get(fn) != b.get(fn) for fn in pairs):
        return selected

    # The parser class uses lazy helpers and dialect-shared state. Restrict
    # broad ownership pruning to expressions whose only syntactic changes are
    # local literals, operators, and primitive predicates.
    def local_change(x, y):
        if equal(x, y):
            return True
        if isinstance(x, ast.Constant) and isinstance(y, ast.Constant):
            return type(x.value) is type(y.value) and type(x.value) in (
                bool,
                str,
                int,
                float,
                type(None),
            )
        if isinstance(x, ast.cmpop) and isinstance(y, ast.cmpop):
            return True
        if (
            isinstance(x, ast.UnaryOp)
            and isinstance(x.op, ast.Not)
            and equal(x.operand, y)
        ):
            return True
        if (
            isinstance(y, ast.UnaryOp)
            and isinstance(y.op, ast.Not)
            and equal(y.operand, x)
        ):
            return True
        if isinstance(x, ast.AST) and type(x) is type(y):
            # Changing the argument to reflective or mutating calls can have
            # process-wide consequences even if the return value is ignored.
            if isinstance(
                x,
                (
                    ast.Call,
                    ast.Raise,
                    ast.Global,
                    ast.Nonlocal,
                    ast.Import,
                    ast.ImportFrom,
                    ast.Delete,
                    ast.With,
                    ast.Await,
                ),
            ):
                return False
            if isinstance(x, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = x.targets if isinstance(x, ast.Assign) else [x.target]
                if not all(isinstance(t, ast.Name) for t in targets):
                    return False
            return all(local_change(getattr(x, f), getattr(y, f)) for f in x._fields)
        if isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
            return all(local_change(xi, yi) for xi, yi in zip(x, y))
        return False

    sites = d["sites"]
    groups = set()
    for fn, (lf, rt) in pairs.items():
        used = _effects(lf, rt, sites, equal)
        if used:
            for k in used:
                groups.add(sites[k])
                # Unbound loads, custom accessors, and any other raised exception
                # at a removed expression can be an asserted behavior.
                row = int(k.partition(":")[0])
                pairs_for_site = [
                    n
                    for n in ast.walk(lf)
                    if getattr(n, "lineno", None) == row
                    and site(n) == k
                    and (guard(n) or optional(n))
                ]
                for match in pairs_for_site:
                    span = match.test if guard(match) else match
                    groups.update(
                        d["throws"].get(str(l), 0)
                        for l in range(span.lineno, span.end_lineno + 1)
                        if str(l) in d["throws"]
                    )
        elif local_change(lf, rt):
            groups.add(d["owners"].get(fn, None))
        else:
            return selected
    if -1 in groups:
        return selected
    names = d["names"]
    keep = set()
    for g in groups:
        if g is None:
            continue
        if d["groups"][g]:
            keep.update(
                selector.by_name[names[int(i)]]
                for i in d["groups"][g].split(",")
                if names[int(i)] in selector.by_name
            )
    deps = selector.dispatch_map()
    keep.update(deps.external)
    keep.update(deps.external_by_source.get(name, ()))
    return selected & keep
