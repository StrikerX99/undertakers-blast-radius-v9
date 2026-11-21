"""Limit a literal parser lookahead change to tests which could take a different comparison."""

import ast
from functools import lru_cache
import hashlib
import io
import json
from pathlib import Path
import tokenize

from source_fast import ancestors_at


@lru_cache(maxsize=1)
def _profile():
    return json.loads(Path(__file__).with_name("parser-lookahead.json").read_bytes())


def _literal(text):
    """Require an isolated ordinary string token with no surrounding Python expression."""
    try:
        items = list(tokenize.tokenize(io.BytesIO(text).readline))
        meaningful = [
            t
            for t in items
            if t.type not in (tokenize.ENCODING, tokenize.NEWLINE, tokenize.ENDMARKER)
        ]
        if len(meaningful) == 1 and meaningful[0].type == tokenize.STRING:
            val = ast.literal_eval(meaningful[0].string)
            if type(val) is str and val.isascii() and 1 <= len(val) <= 48:
                return val
    except (UnicodeError, SyntaxError, ValueError, tokenize.TokenError):
        pass
    return None


def verify(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    ch = changes[0]
    path = ch["old"]
    if path != ch["new"] or not (
        path == "sqlglot/parser.py" or path.startswith("sqlglot/parsers/")
    ):
        return selected
    if len(ch["hunks"]) != 1:
        return selected
    lines = ch["hunks"][0]["lines"]
    if (
        sum(s.startswith("-") for s in lines) != 1
        or sum(s.startswith("+") for s in lines) != 1
    ):
        return selected
    if not any("_match_text_seq" in s for s in lines):
        return selected
    data = _profile()
    if data["pin"] != selector.mapping["commit"] or data["sources"].get(
        path
    ) != selector.mapping["source_hashes"].get(path):
        return selected
    old, tree = selector.source(path)
    if hashlib.sha256(old.encode()).hexdigest() != data["sources"].get(path):
        return selected
    new, removed, inserted = apply(old, ch)
    if len(removed) != 1 or len(inserted) != 1:
        return selected
    row = next(iter(removed))
    before = old.splitlines(keepends=True)
    after = new.splitlines(keepends=True)
    if (
        len(before) != len(after)
        or before[: row - 1] != after[: row - 1]
        or before[row:] != after[row:]
    ):
        return selected
    original, replacement = before[row - 1].encode(), after[row - 1].encode()
    for node in ancestors_at(tree, row):
        if not isinstance(node, ast.Call):
            continue
        site = f"{path}:{node.lineno}:{node.col_offset}"
        obs = data["sites"].get(site)
        if obs is None:
            continue
        for position, arg in enumerate(node.args):
            if (
                arg.lineno != row
                or arg.end_lineno != row
                or not isinstance(arg, ast.Constant)
                or type(arg.value) is not str
            ):
                continue
            prefix = original[: arg.col_offset]
            suffix = original[arg.end_col_offset :]
            if not replacement.startswith(prefix) or not replacement.endswith(suffix):
                continue
            start = len(prefix)
            end = len(replacement) - len(suffix)
            if end <= start:
                continue
            prior = _literal(original[arg.col_offset : arg.end_col_offset])
            current = _literal(replacement[start:end])
            if prior != arg.value or current is None or prior == current:
                continue
            # Both executions take the same steps until this particular comparison.
            # If neither literal matched the normalized token, both then retreat to
            # exactly the same index. A skipped or excluded token cannot distinguish
            # the literals. Keep both directions of changes and all opaque owners.
            uncertain, positions = obs
            words = positions.get(str(position), {})
            if words is None:
                return selected
            ids = set(data["groups"][uncertain])
            for token in (prior, current):
                group = words.get(token)
                if group is not None:
                    ids.update(data["groups"][group])
            if -1 in ids:
                return selected
            deps = selector.dispatch_map()
            keep = set(deps.external)
            keep.update(deps.external_by_source.get(path, ()))
            if path in selector.files:
                keep.update(selector.sets[selector.files[path]])
            keep.update(
                selector.by_name[data["tests"][i]]
                for i in ids
                if data["tests"][i] in selector.by_name
            )
            return selected & keep
    return selected
