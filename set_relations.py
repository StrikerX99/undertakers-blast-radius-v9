"""Observed partial-order relationships between two plain builtin sets."""

from functools import lru_cache
import json
import operator
from pathlib import Path

OPS = {
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
    "Eq": operator.eq,
    "NotEq": operator.ne,
}
# Bit order: equal, proper subset, proper superset, incomparable; 16 means opaque.
TRUE_FOR = {"Eq": 1, "NotEq": 14, "Lt": 2, "LtE": 3, "Gt": 4, "GtE": 5}


def relation(left, right):
    if type(left) not in (set, frozenset) or type(right) not in (set, frozenset):
        return 16
    if len(left) + len(right) > 256 or any(
        type(i) not in (bool, int, str, bytes, type(None))
        for items in (left, right)
        for i in items
    ):
        return 16
    if left == right:
        return 1
    if left < right:
        return 2
    if left > right:
        return 4
    return 8


def refine(selector, path, key, old_op, new_op, candidates):
    if (
        not candidates
        or old_op == new_op
        or old_op not in TRUE_FOR
        or new_op not in TRUE_FOR
    ):
        return candidates
    data = _profile()
    if data["commit"] != selector.mapping["commit"] or data["sources"].get(
        path
    ) != selector.mapping["source_hashes"].get(path):
        return candidates
    observed = data["sites"].get(path + "|" + key)
    if not observed:
        return candidates

    differs = TRUE_FOR[old_op] ^ TRUE_FOR[new_op]
    return {
        test
        for test in candidates
        if (mask := observed.get(selector.by_number[test]["id"])) is None
        or mask & (differs | 16)
    }


@lru_cache(maxsize=1)
def _profile():
    return json.loads(Path(__file__).with_name("set-relations.json").read_bytes())
