"""Check an expensive integration test at its independently submitted SQL query boundaries."""

import ast
from functools import lru_cache
import json
import os
from pathlib import Path
import tempfile

import forkprobe
from query_case import CASE, check
from value_profile import compare, snapshot
from workers import schedule


@lru_cache(maxsize=1)
def reference_data():
    return json.loads(Path(__file__).with_name("executor-queries.json").read_bytes())


def comparison_edit(a, b, equal):
    if equal(a, b):
        return []
    if (
        isinstance(a, ast.Compare)
        and isinstance(b, ast.Compare)
        and len(a.ops) == len(b.ops) == 1
    ):
        old_args = (a.left, a.comparators[0])
        new_args = (b.left, b.comparators[0])
        replacements = []
        for x, y in zip(old_args, new_args):
            if equal(x, y):
                replacements.append(None)
            elif isinstance(x, ast.Constant) and isinstance(y, ast.Constant):
                value = snapshot(y.value)
                if value[0] in ("unknown", "identity"):
                    raise ValueError
                replacements.append(value)
            else:
                raise ValueError
        op = type(b.ops[0]).__name__
        if op in ("Is", "IsNot") and any(v is not None for v in replacements):
            raise ValueError
        return [(a, op, replacements)]
    if type(a) is not type(b):
        raise ValueError
    if isinstance(a, ast.AST):
        return [
            change
            for key in a._fields
            for change in comparison_edit(getattr(a, key), getattr(b, key), equal)
        ]
    if isinstance(a, list) and len(a) == len(b):
        return [change for x, y in zip(a, b) for change in comparison_edit(x, y, equal)]
    raise ValueError


def same_result(sample, op, replacements):
    if not sample:
        return False
    left, right, identical, before = sample
    try:
        return (
            compare(op, replacements[0] or left, replacements[1] or right, identical)
            == before
        )
    except (TypeError, ValueError, KeyError):
        return False


def select_queries(selector, change, selected, apply, equal):
    nr = selector.by_name.get(CASE)
    path = change["old"]
    if (
        nr not in selected
        or change["new"] != path
        or not path.startswith("sqlglot/optimizer/")
        or not path.endswith(".py")
    ):
        return None
    data = reference_data()
    if (
        data["commit"] != selector.mapping["commit"]
        or data["sources"].get(path) != selector.mapping["source_hashes"].get(path)
        or path not in data["sources"]
    ):
        return None
    others = [selector.by_number[t] for t in selected if t != nr]
    saved_ms = schedule(others + [selector.by_number[nr]])[1] - schedule(others)[1]
    if saved_ms < 6000:
        return None
    cpu_limit = min(7.0, 0.34 * saved_ms / 1000)
    source, tree = selector.source(path)
    updated, removed, inserted = apply(source, change)
    from source_fast import local_function

    pair = local_function(
        tree, source.splitlines(True), updated.splitlines(True), removed | inserted
    )
    if pair is None:
        return None
    try:
        edits = comparison_edit(*pair, equal)
    except (ValueError, RecursionError):
        return None
    if len(edits) != 1:
        return None
    node, op, replacements = edits[0]
    site = f"{path}|{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
    rows = data["rows"].get(site)
    if not rows:
        return None
    affected = [
        int(i)
        for i, variants in rows.items()
        if any(not same_result(s, op, replacements) for s in variants)
    ]
    if (
        len(affected) > 12
        or sum(data["queries"][str(i)]["cpu"] for i in affected) > cpu_limit - 1.1
    ):
        return None
    return nr, sorted(affected), data, cpu_limit


def verify(selector, changes, selected, apply, equal, checkout, remaining):
    if remaining < 6 or selector.by_name.get(CASE) not in selected:
        return selected
    code = []
    for item in changes:
        path = item["old"]
        if path != item["new"]:
            return selected
        if path.startswith("tests/fixtures/"):
            # This test class loads both benchmark corpora during setup.
            if (
                path.startswith("tests/fixtures/optimizer/tpc-")
                or path not in selector.files
                or selector.by_name[CASE] in selector.file_tests(path)
            ):
                return selected
        else:
            code.append(item)
    if len(code) != 1:
        return selected
    plan = select_queries(selector, code[0], selected, apply, equal)
    if plan is None:
        return selected
    number, queries, data, cpu_limit = plan
    if not queries:
        return selected - {number}
    with tempfile.TemporaryDirectory(prefix="br-sql-case-") as tmp:
        root = Path(tmp) / "repo"
        checkout(root, changes, selector)
        marker = Path(tmp) / "verified"
        payload = root, data, queries, marker, cpu_limit
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")

        def worker(request):
            check(request, env)

        if forkprobe.run(payload, env, worker, min(11, remaining)) and marker.is_file():
            return selected - {number}
    return selected
