"""Evaluate simple edits to baseline class settings with no observed consumers."""

import ast
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _profile():
    return json.loads(Path(__file__).with_name("dialect-reads.json").read_bytes())


@lru_cache(maxsize=1)
def _sources():
    return json.loads(Path(__file__).with_name("unopened-sources.json").read_bytes())


def _literal_assignment(text):
    statements = ast.parse(text.strip()).body
    if len(statements) != 1:
        return None
    item = statements[0]
    if isinstance(item, ast.Assign) and len(item.targets) == 1:
        target = item.targets[0]
    elif isinstance(item, ast.AnnAssign):
        target = item.target
    else:
        return None
    if not isinstance(target, ast.Name) or not isinstance(item.value, ast.Constant):
        return None
    return target.id, type(item.value.value), item


def verify(selector, changes, selected, apply, equal):
    if not selected or len(changes) != 1 or selector.unknown:
        return selected
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/dialects/")
        or not path.endswith(".py")
        or len(change["hunks"]) != 1
    ):
        return selected
    hunk = change["hunks"][0]["lines"]
    deleted = [s[1:] for s in hunk if s.startswith("-")]
    added = [s[1:] for s in hunk if s.startswith("+")]
    if len(deleted) != 1 or len(added) != 1:
        return selected
    try:
        before, after = _literal_assignment(deleted[0]), _literal_assignment(added[0])
    except (SyntaxError, ValueError, RecursionError):
        return selected
    if (
        not before
        or not after
        or before[:2] != after[:2]
        or before[1] not in (bool, str, int)
    ):
        return selected
    a, b = before[2], after[2]
    if type(a) is not type(b) or any(
        not equal(getattr(a, k), getattr(b, k)) for k in a._fields if k != "value"
    ):
        return selected
    # A change in indentation can change containment while leaving the line's
    # stripped assignment unchanged.
    if (
        deleted[0].partition(deleted[0].lstrip())[0]
        != added[0].partition(added[0].lstrip())[0]
    ):
        return selected
    data, raw = _profile(), _sources()
    digest = selector.mapping["source_hashes"].get(path)
    if (
        not digest
        or data["commit"] != selector.mapping["commit"]
        or data["sources"].get(path) != digest
        or raw["commit"] != data["commit"]
        or raw["unopened"].get(path) != digest
        or path in selector.files
    ):
        return selected
    external = selector.dispatch_map()
    if external.external or external.external_by_source.get(path):
        return selected
    source, _ = selector.source(path)
    _, modified, _ = apply(source, change)
    if len(modified) != 1:
        return selected
    line = next(iter(modified))
    row = data["unused"].get(path, {}).get(str(line))
    if not row or row[1] != before[0]:
        return selected
    # Preserve the independent baseline recorder's positive dependencies,
    # including tests which explicitly import and inspect a dialect module.
    return selected & selector.line_tests(path, {line})
