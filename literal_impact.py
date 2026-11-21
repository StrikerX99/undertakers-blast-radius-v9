"""Observed values and conservative integer intervals at saturated comparison sites."""

import ast
from functools import lru_cache
import json
import operator
from pathlib import Path


ORDER = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}
MISSING = object()


@lru_cache(maxsize=1)
def _observations():
    return json.loads(Path(__file__).with_name("literals.json").read_bytes())


def _scalar(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, str):
        return node.value
    if (
        isinstance(node, ast.UnaryOp)
        and isinstance(node.op, (ast.UAdd, ast.USub))
        and isinstance(node.operand, ast.Constant)
        and type(node.operand.value) is int
    ):
        return (
            -node.operand.value if isinstance(node.op, ast.USub) else node.operand.value
        )
    return MISSING


def _changed_interval(lo, hi, before, after, side, oldop, newop):
    # Integer comparison truth can only change at an operand or its neighbours.
    for value in (lo, hi, before - 1, before, before + 1, after - 1, after, after + 1):
        if not lo <= value <= hi:
            continue
        if side == "left":
            different = oldop(value, before) != newop(value, after)
        else:
            different = oldop(before, value) != newop(after, value)
        if different:
            return True
    return False


def affected(selector, path, old, new, same):
    if not (
        isinstance(old, ast.Compare)
        and isinstance(new, ast.Compare)
        and len(old.ops) == len(new.ops) == 1
        and type(old.ops[0]) in ORDER
        and type(new.ops[0]) in ORDER
    ):
        return None
    a, b = old.left, old.comparators[0]
    c, d = new.left, new.comparators[0]
    if same(a, c) and _scalar(b) is not MISSING and _scalar(d) is not MISSING:
        before, after, side = _scalar(b), _scalar(d), "left"
    elif same(b, d) and _scalar(a) is not MISSING and _scalar(c) is not MISSING:
        before, after, side = _scalar(a), _scalar(c), "right"
    else:
        return None
    if type(before) is not type(after):
        return None
    op_a, op_b = type(old.ops[0]), type(new.ops[0])
    if op_a is op_b and before == after:
        return None
    key = f"{old.lineno}:{old.col_offset}:{old.end_lineno}:{old.end_col_offset}"
    original = selector.mapping.get("comparisons", {}).get(path, {}).get(key)
    if not original or not selector.sets[original["unknown"]]:
        return None
    data = _observations()
    if (
        data["commit"] != selector.mapping["commit"]
        or path not in data["sources"]
        or data["sources"][path] != selector.mapping["source_hashes"].get(path)
    ):
        return None
    row = data["rows"].get(path, {}).get(key)
    if not row or row[0] != side:
        return None
    _, kind, uncertain, values = row
    if kind == "range" and type(before) is int:
        slots = {
            slot
            for lo, hi, slot in values
            if _changed_interval(lo, hi, before, after, side, ORDER[op_a], ORDER[op_b])
        }
    elif kind == type(before).__name__ and op_a in (ast.Eq, ast.NotEq) and op_a is op_b:
        slots = {values.get(str(before)), values.get(str(after))} - {None}
    else:
        return None
    slots.add(uncertain)
    indices = {i for slot in slots for i in data["sets"][slot]}
    readers = {selector.sets.numbers[i] for i in indices} - {None}
    external = selector.dispatch_map()
    readers.update(external.external)
    readers.update(external.external_by_source.get(path, ()))
    return readers
