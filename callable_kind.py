"""Keep all tests when a patched callable changes its Python execution model."""

from collections import defaultdict
from inspect import CO_ASYNC_GENERATOR, CO_COROUTINE, CO_GENERATOR
import re
from types import CodeType


MARKER = re.compile(r"\b(?:yield|async|await)\b")
KIND_BITS = CO_ASYNC_GENERATOR | CO_COROUTINE | CO_GENERATOR


def generator_kinds(source, path):
    code = compile(source, path, "exec", dont_inherit=True)
    result = defaultdict(list)
    pending = [code]
    while pending:
        scope = pending.pop()
        kind = scope.co_flags & KIND_BITS
        if kind:
            result[scope.co_qualname].append(kind)
        pending.extend(c for c in scope.co_consts if isinstance(c, CodeType))
    return {name: sorted(kinds) for name, kinds in result.items()}


def strengthen(selector, changes, selected, apply):
    if selected == selector.all:
        return selected
    for change in changes:
        path = change["old"]
        if not (path.startswith("sqlglot/") and path.endswith(".py")):
            continue
        if not any(
            line[0] in "+-" and MARKER.search(line[1:])
            for hunk in change["hunks"]
            for line in hunk["lines"]
            if line
        ):
            continue
        if path != change["new"]:
            return set(selector.all)
        original, _ = selector.source(path)
        updated, _, _ = apply(original, change)
        if generator_kinds(original, path) != generator_kinds(updated, path):
            return set(selector.all)
    return selected
