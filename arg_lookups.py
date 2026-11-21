"""Use complete baseline key observations to certify an equivalent argument lookup."""

import ast
from functools import lru_cache
import json
from pathlib import Path

from source_fast import local_function


@lru_cache(maxsize=1)
def _reference():
    return json.loads(Path(__file__).with_name("arg-lookups.json").read_bytes())


def _single_key(old, new, equal):
    edits = []

    def visit(a, b):
        if equal(a, b):
            return True
        if type(a) is not type(b):
            return False
        if (
            isinstance(a, ast.Call)
            and isinstance(a.func, ast.Attribute)
            and a.func.attr == "get"
            and isinstance(a.func.value, ast.Attribute)
            and a.func.value.attr == "args"
            and equal(a.func, b.func)
            and not a.keywords
            and not b.keywords
            and len(a.args) == len(b.args) == 1
            and isinstance(a.args[0], ast.Constant)
            and isinstance(b.args[0], ast.Constant)
            and type(a.args[0].value) is type(b.args[0].value) is str
        ):
            edits.append((a, a.args[0].value, b.args[0].value))
            return True
        if isinstance(a, ast.AST):
            return all(visit(getattr(a, k), getattr(b, k)) for k in a._fields)
        if isinstance(a, list) and len(a) == len(b):
            return all(visit(x, y) for x, y in zip(a, b))
        return False

    return edits[0] if visit(old, new) and len(edits) == 1 else None


def verify(selector, changes, selected, apply, equal):
    if len(changes) != 1 or not selected:
        return selected
    change = changes[0]
    # A key substitution is local to one physical line. Separate textual
    # edits can affect source-inspecting tests and traceback positions.
    if len(change["hunks"]) != 1:
        return selected
    lines = change["hunks"][0]["lines"]
    added = [line for line in lines if line.startswith("+")]
    removed = [line for line in lines if line.startswith("-")]
    if len(added) != 1 or len(removed) != 1:
        return selected
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return selected
    if not any(
        ".args.get(" in line
        for h in change["hunks"]
        for line in h["lines"]
        if line.startswith(("-", "+"))
    ):
        return selected
    data = _reference()
    if selector.mapping["commit"] != data["commit"] or path not in data["rows"]:
        return selected
    if data["sources"][path] != selector.mapping["source_hashes"].get(path):
        return selected
    original, tree = selector.source(path)
    updated, removed, inserted = apply(original, change)
    pair = local_function(
        tree, original.splitlines(True), updated.splitlines(True), removed | inserted
    )
    if pair is None:
        return selected
    edit = _single_key(*pair, equal)
    if edit is None:
        return selected
    node, old, new = edit
    key = f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
    row = data["rows"][path].get(key)
    if row is None:
        return selected
    plain_key, excluded, allowed = row
    if (
        plain_key != old
        or new in excluded
        or (allowed is not None and new not in allowed)
    ):
        return selected
    # Both lookups returned the identical object at every recorded invocation
    # in the passing suite, including collection and setup. Keep all tests with
    # untraced processes or dependencies upon the edited Python source text.
    external = selector.dispatch_map()
    keep = (
        set(selector.unknown)
        | external.external
        | external.external_by_source.get(path, set())
    )
    if path in selector.files:
        keep.update(selector.sets[selector.files[path]])
    return set(selected) & keep
