"""Verify bounded optimizer edits at the input to the unchanged relational planner."""

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
from query_case import CASE, source_environment
from arg_lookups import _single_key
from dormant_settings import _sources
from query_impact import comparison_edit
from source_fast import local_function
from workers import schedule


@lru_cache(maxsize=1)
def reference():
    return json.loads(
        Path(__file__).with_name("optimizer-checkpoints.json").read_bytes()
    )


def metadata_projection_edit(old, new, equal):
    """Recognize a change to the order of a comprehension stored as node metadata."""
    changes = 0

    def visit(a, b):
        nonlocal changes
        if equal(a, b):
            return True
        if type(a) is not type(b):
            return False
        if (
            isinstance(a, ast.Assign)
            and len(a.targets) == 1
            and equal(a.targets, b.targets)
        ):
            target = a.targets[0]
            x, y = a.value, b.value
            if (
                isinstance(target, ast.Subscript)
                and isinstance(target.value, ast.Attribute)
                and target.value.attr == "meta"
                and isinstance(target.slice, ast.Constant)
                and type(target.slice.value) is str
                and isinstance(x, ast.ListComp)
                and isinstance(y, ast.ListComp)
                and len(x.generators) == len(y.generators) == 1
                and equal(x.elt, y.elt)
                and isinstance(x.elt, (ast.Name, ast.Attribute))
            ):
                u, v = x.generators[0], y.generators[0]

                def strip(expr):
                    return (
                        expr.args[0]
                        if isinstance(expr, ast.Call)
                        and isinstance(expr.func, ast.Name)
                        and expr.func.id == "reversed"
                        and len(expr.args) == 1
                        and not expr.keywords
                        else expr
                    )

                if (
                    not u.is_async
                    and not v.is_async
                    and not u.ifs
                    and not v.ifs
                    and equal(u.target, v.target)
                    and not equal(u.iter, v.iter)
                    and equal(strip(u.iter), strip(v.iter))
                ):
                    changes += 1
                    return True
        if isinstance(a, ast.AST):
            return all(visit(getattr(a, k), getattr(b, k)) for k in a._fields)
        if isinstance(a, list) and len(a) == len(b):
            return all(visit(x, y) for x, y in zip(a, b))
        return False

    return visit(old, new) and changes == 1


@lru_cache(maxsize=1)
def unused_modules():
    return json.loads(
        Path(__file__).with_name("executor-unused-modules.json").read_bytes()
    )


def local_replacement(selector, change, apply, equal):
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return
    source, tree = selector.source(path)
    updated, old_lines, new_lines = apply(source, change)
    pair = local_function(
        tree, source.splitlines(True), updated.splitlines(True), old_lines | new_lines
    )
    if pair is None and len(old_lines) == 1 and old_lines == new_lines:
        # The source-fast path excludes the final line of a function. A fixed
        # one-line replacement can also be checked against both complete ASTs.
        (line,) = old_lines
        fresh = ast.parse(updated)
        compile(fresh, path, "exec")

        def bodies(tree):
            return [
                n
                for n in ast.walk(tree)
                if type(n) is ast.FunctionDef
                and n.body
                and n.lineno < line
                and n.body[0].lineno <= line <= n.end_lineno
            ]

        before, after = bodies(tree), bodies(fresh)
        if len(before) == len(after) == 1:
            a, b = before[0], after[0]
            if (
                a.name == b.name
                and a.lineno == b.lineno
                and a.end_lineno == b.end_lineno
            ):
                pair = a, b
    if pair is None:
        return
    a, b = pair
    if (
        type(a) is not ast.FunctionDef
        or a.decorator_list
        or ast.get_docstring(a, clean=False) != ast.get_docstring(b, clean=False)
        or not all(
            equal(getattr(a, k), getattr(b, k)) for k in a._fields if k != "body"
        )
    ):
        return
    return a, b


def literal_change(a, b, equal, allowed_parent):
    # Accept one immutable scalar replacement and no other AST change.
    delta = []

    def same(x, y, parent=None):
        if type(x) is not type(y):
            return False
        if type(x) is ast.Constant:
            if equal(x, y):
                return True
            if (
                allowed_parent(parent)
                and type(x.value) is type(y.value)
                and type(x.value)
                in (
                    str,
                    bool,
                    int,
                    float,
                )
            ):
                delta.append(1)
                return True
            return False
        if isinstance(x, ast.AST):
            return all(same(getattr(x, k), getattr(y, k), x) for k in x._fields)
        if isinstance(x, list):
            return len(x) == len(y) and all(same(u, v, parent) for u, v in zip(x, y))
        return x == y

    return same(a, b) and len(delta) == 1


def boolean_operator_change(hunks):
    """Accept one Python boolean operator edit outside comments and strings."""
    if len(hunks) != 1:
        return False
    import difflib
    import io
    import tokenize

    def changed_tokens(prefix):
        lines = [line[1:] for line in hunks[0]["lines"] if line.startswith(prefix)]
        if len(lines) != 1:
            raise ValueError
        lexer = tokenize.generate_tokens(io.StringIO(lines[0].strip()).readline)
        return [(t.type, t.string) for t in lexer]

    try:
        old, new = changed_tokens("-"), changed_tokens("+")
    except (tokenize.TokenError, IndentationError, ValueError):
        return False
    changes = [
        (old[start:end], new[new_start:new_end])
        for tag, start, end, new_start, new_end in difflib.SequenceMatcher(
            None, old, new, autojunk=False
        ).get_opcodes()
        if tag != "equal"
    ]
    name = tokenize.NAME
    return len(changes) == 1 and changes[0] in (
        ([(name, "and")], [(name, "or")]),
        ([(name, "or")], [(name, "and")]),
        ([(name, "not")], []),
        ([], [(name, "not")]),
    )


def touched_queries(selector, change, apply, equal, data):
    path = change["old"]
    if path != change["new"] or not path.endswith(".py"):
        return
    source, _ = selector.source(path)
    if data["sources"].get(path) != hashlib.sha256(source.encode()).hexdigest():
        return
    pair = local_replacement(selector, change, apply, equal)
    if not pair:
        return
    a, b = pair
    if not (
        metadata_projection_edit(a, b, equal)
        or boolean_operator_change(change["hunks"])
        or _single_key(a, b, equal)
        or literal_change(a, b, equal, lambda parent: isinstance(parent, ast.Return))
    ):
        try:
            operations = comparison_edit(a, b, equal)
        except (TypeError, ValueError, RecursionError):
            return
        if len(operations) != 1:
            return
        node, operation, replacements = operations[0]
        before = type(node.ops[0]).__name__
        identity = {"Is", "IsNot"}
        ordinary = {"Eq", "NotEq", "Lt", "LtE", "Gt", "GtE"}
        if before in identity:
            if operation not in identity or any(x is not None for x in replacements):
                return
        elif before not in ordinary or operation not in ordinary:
            return
        elif not any(
            isinstance(expr, ast.Constant) and type(expr.value) in (int, str)
            for expr in (node.left, node.comparators[0])
        ):
            return
        elif any(
            replacement is not None and replacement[0] not in ("int", "str")
            for replacement in replacements
        ):
            return
    indices = data["sites"].get(path, {}).get(f"{a.lineno}:{a.name}")
    return indices


def candidate_queries(selector, changes, selected, apply, equal):
    case = selector.by_name.get(CASE)
    if case not in selected or not any(
        c["old"].startswith("sqlglot/optimizer/") for c in changes
    ):
        return
    saved = (
        schedule([selector.by_number[i] for i in selected])[1]
        - schedule([selector.by_number[i] for i in selected if i != case])[1]
    ) / 1000
    if saved < 8:
        return
    data = reference()
    if selector.mapping["commit"] != data["commit"]:
        return
    indices = set()
    companions = []
    for edit in changes:
        path = edit["old"]
        if path.startswith("sqlglot/optimizer/"):
            # Charge all potentially reached queries across all edits; run the
            # fully patched optimizer on the union, preserving interactions.
            for hunk in edit["hunks"]:
                ids = touched_queries(
                    selector, dict(edit, hunks=[hunk]), apply, equal, data
                )
                if not ids:
                    return
                indices.update(ids)
        else:
            # This companion's code and source text were unused by the actual
            # passing test in its driver and child processes.
            digest = selector.mapping["source_hashes"].get(path)
            raw = _sources()
            if (
                not digest
                or raw["commit"] != data["commit"]
                or raw["unopened"].get(path) != digest
                or unused_modules().get(path) != digest
            ):
                return
            pair = local_replacement(selector, edit, apply, equal)
            if not pair or not literal_change(*pair, equal, lambda parent: True):
                return
            companions.append(path[:-3].replace("/", "."))
    if indices:
        return case, sorted(indices), min(8.5, saved * 0.60), companions


def check(payload, env):
    import signal
    import sys

    root, indices, seconds, marker, companions = payload
    os.environ.update(env)
    os.chdir(root)
    sys.path.insert(0, str(root))
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, seconds)

    from sqlglot import parse_one
    import sqlglot.executor as executor
    from tests.helpers import load_sql_fixture_pairs

    data = reference()
    query_data = json.loads(
        Path(__file__).with_name("executor-queries.json").read_bytes()
    )
    if query_data["commit"] != data["commit"] or set(query_data["queries"]) != set(
        data["graphs"]
    ):
        return
    scope = source_environment(root, query_data["driver_sha"])
    # The pinned execute() uses only table names and schema depth before
    # optimize() when the caller supplies its complete nonempty schema. Its
    # table rows and column iterators are consumed by the downstream executor,
    # after the graph comparison terminates this worker.
    scope["_tables"] = {
        name: scope["Table"](columns=columns, rows=[])
        for name, columns in scope["_schema"].items()
    }
    original = executor.optimize
    queries = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))

    class Matched(Exception):
        pass

    def compare(*args, **kwargs):
        graph = original(*args, **kwargs)
        if expression_digest(graph) != expected:
            raise ValueError("Optimizer output changed")
        raise Matched

    executor.optimize = compare
    for i in indices:
        meta, sql, _ = queries[i]
        if (
            hashlib.sha256(sql.encode()).hexdigest()
            != query_data["queries"][str(i)]["sql"]
        ):
            return
        expected = data["graphs"][str(i)]
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        try:
            scope["mp_execute"](expr, meta)
        except Matched:
            continue
        return
    if not any(name in sys.modules for name in companions):
        marker.touch()


def verify(selector, changes, selected, apply, equal, checkout, remaining):
    if remaining < 4:
        return selected
    request = candidate_queries(selector, changes, selected, apply, equal)
    if request is None:
        return selected
    number, indices, cpu_limit, companions = request
    with tempfile.TemporaryDirectory(prefix="br-plan-") as tmp:
        root = Path(tmp) / "repo"
        marker = Path(tmp) / "verified"
        checkout(root, changes, selector)
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")

        def worker(payload):
            check(payload, env)

        ok = forkprobe.run(
            (root, indices, cpu_limit, marker, companions),
            env,
            worker,
            min(remaining, cpu_limit * 3 + 1),
        )
        if ok and marker.is_file():
            return selected - {number}
    return selected
