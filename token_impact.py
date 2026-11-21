"""Narrow a parser enum edit when an executor test observed neither dispatch token."""

from functools import lru_cache
import json
from pathlib import Path
import re

# The collector records the original AST call's byte column. A text match in
# a comment, a literal, another function or a different call has no trace row.
MATCH = re.compile(r"(?<!\w)self\s*\.\s*_match\s*\(\s*TokenType\s*\.\s*(\w+)")


@lru_cache(maxsize=1)
def reference():
    return json.loads(Path(__file__).with_name("token-cases.json").read_bytes())


def verify(selector, changes, selected, apply):
    if not selected or len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if path != change["new"] or not (
        path == "sqlglot/parser.py"
        or path.startswith("sqlglot/parsers/")
        and path.endswith(".py")
    ):
        return selected
    if len(change["hunks"]) != 1:
        return selected
    lines = change["hunks"][0]["lines"]
    before = [line[1:] for line in lines if line.startswith("-")]
    after = [line[1:] for line in lines if line.startswith("+")]
    if len(before) != 1 or len(after) != 1 or "_match" not in after[0]:
        return selected
    replacements = []
    for old in MATCH.finditer(before[0]):
        new = MATCH.match(after[0], old.start())
        if (
            new
            and before[0][: old.start(1)] == after[0][: new.start(1)]
            and before[0][old.end(1) :] == after[0][new.end(1) :]
            and old[1] != new[1]
        ):
            replacements.append((old, new))
    if len(replacements) != 1:
        return selected
    old, new = replacements[0]
    data = reference()
    if (
        data["commit"] != selector.mapping["commit"]
        or path not in data["sources"]
        or data["sources"][path] != selector.mapping["source_hashes"].get(path)
    ):
        return selected
    source, _ = selector.source(path)
    _, removed, _ = apply(source, change)
    if len(removed) != 1:
        return selected
    line = next(iter(removed))
    if source.splitlines(keepends=True)[line - 1] != before[0]:
        return selected
    column = len(before[0][: old.start()].encode("utf-8"))
    key = f"{path}:{line}:{column}"
    tokens = data["tokens"]
    if old[1] not in tokens or new[1] not in tokens:
        return selected
    changed = {tokens[old[1]], tokens[new[1]], "*"}
    source_readers = (
        selector.sets[selector.files[path]] if path in selector.files else set()
    )
    external = selector.dispatch_map()
    opaque = (
        source_readers
        | selector.unknown
        | external.external
        | external.external_by_source.get(path, set())
    )
    saved = set()
    for test_id, rows in data["tests"].items():
        number = selector.by_name.get(test_id)
        observed = rows.get(key)
        if (
            number in selected
            and number not in opaque
            and observed
            and changed.isdisjoint(observed)
        ):
            saved.add(number)
    return selected - saved
