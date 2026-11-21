"""Replay graph-local optimizer edits in the pinned TPC-DS worker.

A rewritten graph can still return the same table. In that case run the actual
planner and Python executor and compare all ordered rows and column names.
"""

import ast
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import pickle
import tempfile

import forkprobe
from checkpoint_graph import expression_digest
from dormant_settings import _sources
from optimizer_checkpoints import reference
from query_case import CASE, source_environment, table_digest
from workers import schedule


# These pinned transformations only receive an expression graph and locally
# constructed scopes/containers from the optimizer. No database connection,
# schema, dialect configuration or reusable journal is passed to their rules.
GRAPH_RULES = frozenset(
    {
        "eliminate_ctes",
        "eliminate_joins",
        "eliminate_subqueries",
        "optimize_joins",
        "unnest_subqueries",
    }
)


@lru_cache(maxsize=1)
def collection():
    return json.loads(
        Path(__file__).with_name("optimizer-collection.json").read_bytes()
    )


def key(node):
    return ast.dump(node, include_attributes=False)


def short_readonly_helper(fn):
    # A new call to a local helper is admitted only for selector-style helpers
    # reading their argument. Recursion, when present, is bounded by the probe.
    allowed = (
        ast.FunctionDef,
        ast.arguments,
        ast.arg,
        ast.Expr,
        ast.Constant,
        ast.If,
        ast.Return,
        ast.Call,
        ast.Name,
        ast.Load,
        ast.Attribute,
        ast.Tuple,
        ast.IfExp,
        ast.Assign,
        ast.Store,
    )
    args = {p.arg for p in fn.args.args}
    for node in (n for stmt in fn.body for n in ast.walk(stmt)):
        if not isinstance(node, allowed):
            return False
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name) and node.func.id in {"isinstance", fn.name}
        ):
            return False
        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            return False
        if isinstance(node, ast.Assign) and not all(
            isinstance(t, ast.Name) and t.id in args for t in node.targets
        ):
            return False
        if isinstance(node, ast.Name) and node.id not in args | {
            "exp",
            "object",
            "isinstance",
            fn.name,
        }:
            return False

    def allowed_return(n):
        if isinstance(n, ast.Name):
            return n.id in args
        if isinstance(n, ast.Constant):
            return type(n.value) in (str, int, bool, type(None))
        if isinstance(n, ast.Attribute):
            return n.attr in {
                "left",
                "right",
                "parent",
                "this",
                "expression",
            } and allowed_return(n.value)
        if isinstance(n, ast.IfExp):
            return allowed_return(n.body) and allowed_return(n.orelse)
        return (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == fn.name
            and all(allowed_return(a) for a in n.args)
        )

    return all(
        allowed_return(n.value)
        for stmt in fn.body
        for n in ast.walk(stmt)
        if isinstance(n, ast.Return) and n.value is not None
    )


def confined_body(old, new, helpers):
    """Only alter decisions, traversal, local bindings and reads.

    Every new call, mutation or reference is checked even when a surrounding
    guard also changed. Existing update operations must occur verbatim inside
    the same function. New local assignments cannot bind modules or classes.
    """
    old_nodes = {key(n) for stmt in old.body for n in ast.walk(stmt)}
    old_loop_targets = {key(n.target) for n in ast.walk(old) if isinstance(n, ast.For)}
    old_names = {n.id for n in ast.walk(old) if isinstance(n, ast.Name)}
    locals_ = {a.arg for a in old.args.args + old.args.kwonlyargs}
    locals_.update(
        n.id
        for n in ast.walk(old)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
    )
    readonly_calls = {
        "isinstance",
        "any",
        "all",
        "len",
        "list",
        "tuple",
        "reversed",
        "bool",
    }
    lookup_calls = {"get", "find", "find_all", "traverse"}
    fields = {
        "this",
        "left",
        "right",
        "parent",
        "expression",
        "expressions",
        "args",
        "alias",
        "alias_or_name",
        "name",
        "kind",
        "side",
        "method",
        "is_cte",
        "is_union",
        "is_root",
        "scope_type",
        "source",
        "selects",
    }
    fields |= lookup_calls
    simple = (
        ast.Name,
        ast.Attribute,
        ast.Subscript,
        ast.Constant,
        ast.Tuple,
        ast.List,
        ast.UnaryOp,
        ast.BoolOp,
        ast.BinOp,
        ast.Compare,
        ast.IfExp,
        ast.Call,
        ast.GeneratorExp,
        ast.comprehension,
        ast.keyword,
        ast.Load,
        ast.Store,
        ast.boolop,
        ast.operator,
        ast.unaryop,
        ast.cmpop,
    )

    def rooted_in_local(n):
        while isinstance(n, (ast.Subscript, ast.Attribute)):
            n = n.value
        return isinstance(n, ast.Name) and n.id in locals_

    for stmt in new.body:
        for n in ast.walk(stmt):
            if key(n) in old_nodes:
                continue
            if isinstance(n, ast.Name):
                if (
                    n.id.startswith("__")
                    or n.id not in old_names | locals_ | readonly_calls | helpers
                ):
                    return False
            elif isinstance(n, ast.Attribute):
                if n.attr.startswith("_") or not (
                    (rooted_in_local(n) and n.attr in fields)
                    or isinstance(n.value, ast.Name)
                    and n.value.id == "exp"
                ):
                    return False
            elif isinstance(n, ast.Compare):
                # CPython may intern equal string or numeric objects differently
                # across probe and worker processes. Only singletons are
                # portable operands for newly introduced identity comparisons.
                vals = [n.left, *n.comparators]
                for i, op in enumerate(n.ops):
                    if isinstance(op, (ast.Is, ast.IsNot)) and not any(
                        isinstance(v, ast.Constant)
                        and type(v.value) in (type(None), bool)
                        for v in vals[i : i + 2]
                    ):
                        return False
                if any(
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id in {"id", "hash"}
                    for v in ast.walk(n)
                ):
                    return False
            elif isinstance(n, ast.BinOp):
                if any(
                    isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id in {"id", "hash"}
                    for v in ast.walk(n)
                ):
                    return False
            elif isinstance(n, ast.Call):
                if isinstance(n.func, ast.Name):
                    if n.func.id not in readonly_calls | helpers:
                        return False
                elif not (
                    isinstance(n.func, ast.Attribute)
                    and n.func.attr in lookup_calls
                    and rooted_in_local(n.func)
                ):
                    return False
                if any(isinstance(a, ast.Starred) for a in n.args) or any(
                    k.arg is None for k in n.keywords
                ):
                    return False
            elif isinstance(n, ast.Assign):
                if not all(
                    isinstance(t, ast.Name) and t.id in locals_ for t in n.targets
                ):
                    return False
                if not all(isinstance(part, simple) for part in ast.walk(n.value)):
                    return False
                # A new alias of a class or its mutable configuration would let
                # a later verbatim mutation change shared state. Bind only data
                # derived from the rule's own locals, or a proven read helper.
                for part in ast.walk(n.value):
                    if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Load):
                        if part.id not in locals_ | readonly_calls | helpers:
                            return False
                    if isinstance(part, ast.Attribute) and part.attr not in fields:
                        return False
                if isinstance(
                    n.value, (ast.Attribute, ast.Subscript, ast.Name)
                ) and not rooted_in_local(n.value):
                    return False
            elif isinstance(n, ast.For):
                if key(n.target) not in old_loop_targets and (
                    not isinstance(n.target, ast.Name) or n.target.id not in locals_
                ):
                    return False
            elif not isinstance(n, (ast.If, ast.Return, ast.Expr, *simple)):
                return False
            if isinstance(n, (ast.Attribute, ast.Subscript)) and not isinstance(
                n.ctx, ast.Load
            ):
                return False
    return True


def affected(selector, change, apply, equal, baseline, observed, changed_sites=None):
    path = change["old"]
    if (
        path != change["new"]
        or path not in baseline["sources"]
        or path in selector.files
    ):
        return
    if path not in {f"sqlglot/optimizer/{rule}.py" for rule in GRAPH_RULES}:
        return
    original, tree = selector.source(path)
    digest = hashlib.sha256(original.encode()).hexdigest()
    if (
        digest != baseline["sources"][path]
        or _sources()["unopened"].get(path) != digest
    ):
        return
    updated, _, _ = apply(original, change)
    fresh = ast.parse(updated)
    compile(fresh, path, "exec")
    if len(tree.body) != len(fresh.body):
        return
    old_fns = {fn.name: fn for fn in tree.body if type(fn) is ast.FunctionDef}
    new_fns = {fn.name: fn for fn in fresh.body if type(fn) is ast.FunctionDef}
    helpers = {
        name
        for name in old_fns.keys() & new_fns.keys()
        if short_readonly_helper(old_fns[name]) and short_readonly_helper(new_fns[name])
    }
    qids = set()
    for old, new in zip(tree.body, fresh.body):
        if equal(old, new):
            continue
        if not (
            type(old) is type(new) is ast.FunctionDef
            and not old.decorator_list
            and all(
                equal(getattr(old, k), getattr(new, k))
                for k in old._fields
                if k != "body"
            )
            and confined_body(old, new, helpers)
        ):
            return
        site = f"{old.lineno}:{old.name}"
        ids = baseline["sites"].get(path, {}).get(site)
        if not ids or site in observed["sites"].get(path, ()):
            return
        if changed_sites is not None:
            changed_sites.add(Path(path).stem + ":" + old.name)
        qids.update(ids)
    return qids


def run_worker(payload):
    import signal
    import sys

    root, indices, seconds, marker, modules = payload
    os.chdir(root)
    sys.path.insert(0, str(root))
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, seconds)

    from sqlglot import parse_one, transpile
    import sqlglot.executor as ex
    from tests.helpers import load_sql_fixture_pairs

    graphs = reference()
    queries = json.loads(Path(__file__).with_name("executor-queries.json").read_bytes())
    if graphs["commit"] != queries["commit"] or set(graphs["graphs"]) != set(
        queries["queries"]
    ):
        return
    scope = source_environment(root, queries["driver_sha"])
    cases = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    original = ex.optimize
    real_tables = scope["_tables"]
    schema_tables = {
        name: scope["Table"](columns=columns, rows=[])
        for name, columns in scope["_schema"].items()
    }

    from optimizer_suffix import Suffix

    suffix = Suffix(root, modules, graphs["commit"])

    class Changed(Exception):
        pass

    class Matched(Exception):
        pass

    def observe(*args, **kwargs):
        current = original(*args, **kwargs, **options)
        if expression_digest(current) == expected_graph:
            raise Matched
        raise Changed

    ex.optimize = observe
    for index in indices:
        meta, sql, _ = cases[index]
        record = queries["queries"][str(index)]
        options = suffix.options(index, Matched)
        expected_graph = graphs["graphs"][str(index)]
        if (
            not meta.get("execute")
            or hashlib.sha256(sql.encode()).hexdigest() != record["sql"]
        ):
            return
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        scope["_tables"] = schema_tables
        ex.optimize = observe
        try:
            scope["mp_execute"](expr, meta)
        except Matched:
            pass
        except Changed:
            # The pinned executor uses table names/schema before optimization.
            # Repeat the entire worker on real fixture rows for changed graphs.
            ex.optimize = original
            scope["_tables"] = real_tables
            actual_expr = pickle.loads(pickle.dumps(parse_one(sql)))
            value = scope["mp_execute"](actual_expr, meta)
            if value is None or table_digest(value) != record["result"]:
                return
        else:
            return
        ex.optimize = original
        generated = transpile(sql, write="duckdb")
        if (
            len(generated) != 1
            or hashlib.sha256(generated[0].encode()).hexdigest() != record["duck_sql"]
        ):
            return
    marker.touch()


def verify(selector, changes, selected, apply, equal, checkout, remaining):
    number = selector.by_name.get(CASE)
    stems = {f"sqlglot/optimizer/{name}.py" for name in GRAPH_RULES}
    if (
        remaining < 6
        or number not in selected
        or number in selector.unknown
        or not changes
        or len({c["old"] for c in changes}) != len(changes)
        or any(c["old"] not in stems for c in changes)
    ):
        return selected
    baseline, observed = reference(), collection()
    if (
        baseline["commit"] != observed["revision"]
        or baseline["commit"] != selector.mapping["commit"]
    ):
        return selected
    if _sources()["commit"] != baseline["commit"]:
        return selected
    ids = set()
    changed_sites = set()
    for change in changes:
        queries = affected(
            selector, change, apply, equal, baseline, observed, changed_sites
        )
        if not queries:
            return selected
        ids.update(queries)
    saved = (
        schedule([selector.by_number[i] for i in selected])[1]
        - schedule([selector.by_number[i] for i in selected if i != number])[1]
    ) / 1000
    if saved < 8:
        return selected
    cpu = min(10.0, saved * 0.6)
    with tempfile.TemporaryDirectory(prefix="br-executor-") as temp:
        root, marker = Path(temp) / "repo", Path(temp) / "verified"
        checkout(root, changes, selector)
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        if (
            forkprobe.run(
                (root, sorted(ids), cpu, marker, tuple(sorted(changed_sites))),
                env,
                run_worker,
                min(remaining, cpu * 2.5 + 1),
            )
            and marker.is_file()
        ):
            return selected - {number}
    return selected
