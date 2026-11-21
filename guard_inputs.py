"""Refine a newly added short-circuit guard using baseline primitive field reads."""

import ast
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _data():
    return json.loads(Path(__file__).with_name("guard-inputs.json").read_bytes())


def _primitive_condition(expr):
    """Return the field and accepted truth states for plain Python str values."""
    if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
        base = _primitive_condition(expr.operand)
        if base is None:
            return None
        field, allowed = base
        return field, {"0", "1"} - allowed
    if isinstance(expr, ast.Attribute):
        return expr, {"1"}
    if (
        not isinstance(expr, ast.Compare)
        or len(expr.ops) != 1
        or len(expr.comparators) != 1
    ):
        return None
    left, right = expr.left, expr.comparators[0]
    if isinstance(left, ast.Constant):
        left, right = right, left
    if not isinstance(left, ast.Attribute) or not isinstance(right, ast.Constant):
        return None
    op = type(expr.ops[0])
    if right.value == "" and op in (ast.Eq, ast.NotEq):
        return left, {"0" if op is ast.Eq else "1"}
    if right.value is None and op in (ast.Is, ast.IsNot):
        return left, set() if op is ast.Is else {"0", "1"}
    return None


def analyze(selector, path, old, new, equal):
    if not (
        isinstance(old, ast.If)
        and isinstance(new, ast.If)
        and isinstance(old.test, ast.Call)
        and isinstance(new.test, ast.BoolOp)
        and isinstance(new.test.op, ast.And)
        and len(new.test.values) == 2
    ):
        return None
    original = old.test
    expr, addition = new.test.values
    if not (
        equal(expr, original)
        and equal(old.body, new.body)
        and equal(old.orelse, new.orelse)
        and len(original.args) == 2
        and isinstance(original.args[0], ast.Name)
    ):
        return None
    name = original.args[0].id
    result = _primitive_condition(addition)
    if result is None:
        return None
    attr, allowed = result
    if not (
        isinstance(attr, ast.Attribute)
        and attr.attr in {"name", "this"}
        and isinstance(attr.value, ast.Name)
        and attr.value.id == name
    ):
        return None
    data = _data()
    if data["commit"] != selector.mapping["commit"] or data["sources"].get(
        path
    ) != selector.mapping["source_hashes"].get(path):
        return None
    key = f"{original.lineno}:{original.col_offset}"
    row = data["rows"].get(path, {}).get(key)
    if not row:
        return None
    index = 0 if attr.attr == "this" else 1
    safe, uncertain = set(), set()
    for observation, names in row.items():
        # A false first operand suppresses the field lookup in both programs.
        value = observation[index]
        matched = observation == "--" or value in allowed
        if not matched and "*" in names:
            return None
        for slot in names:
            if isinstance(slot, int):
                number = selector.sets.numbers[slot]
                if number is not None:
                    (safe if matched else uncertain).add(number)
            elif slot.endswith("::*") and not matched:
                uncertain.update(
                    t["number"] for t in selector.tests if t["file"] == slot[:-3]
                )
    if not safe:
        return None
    external = selector.dispatch_map()
    uncertain.update(external.external)
    uncertain.update(external.external_by_source.get(path, ()))
    return selector.line_tests(
        path, range(old.test.lineno, old.test.end_lineno + 1)
    ) - (safe - uncertain)


def verify(selector, changes, selected, apply, equal):
    if len(changes) != 1 or not selected or selector.unknown or selector.import_modules:
        return selected
    change = changes[0]
    path = change["old"]
    if path != change["new"] or not path.startswith("sqlglot/optimizer/"):
        return selected
    # Restrict to a local, one-line change and compare the entire surrounding function.
    hunks = change["hunks"]
    if len(hunks) != 1:
        return selected
    added = [line[1:] for line in hunks[0]["lines"] if line.startswith("+")]
    removed = [line[1:] for line in hunks[0]["lines"] if line.startswith("-")]
    if len(added) != 1 or len(removed) != 1 or "isinstance(" not in removed[0]:
        return selected
    from source_fast import local_function

    original, tree = selector.source(path)
    updated, removed, inserted = apply(original, change)
    pair = local_function(
        tree, original.splitlines(True), updated.splitlines(True), removed | inserted
    )
    if pair is None:
        return selected
    left, right = pair
    for a, b in zip(ast.walk(left), ast.walk(right)):
        if not (isinstance(a, ast.If) and isinstance(b, ast.If)) or equal(
            a.test, b.test
        ):
            continue
        remaining = analyze(selector, path, a, b, equal)
        if remaining is None:
            return selected
        changed_test = b.test
        b.test = a.test
        try:
            if not equal(left, right):
                return selected
        finally:
            b.test = changed_test
        reach = selector.line_tests(path, range(a.test.lineno, a.test.end_lineno + 1))
        raw = selector.sets[selector.files[path]] if path in selector.files else set()
        return selected - (reach - remaining - raw)
    return selected
