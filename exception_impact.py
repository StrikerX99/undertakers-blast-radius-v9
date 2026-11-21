"""Use baseline exceptions reaching a try block to evaluate changes to its catch types."""

import ast
import builtins
from functools import lru_cache
import json
from pathlib import Path

# Plain builtin exception lookups and an ordinary except clause have no custom
# match protocol. Arbitrary names, exception groups and qualified aliases keep
# their existing schedule.
KINDS = (
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BaseException",
    "Exception",
    "IndexError",
    "KeyError",
    "LookupError",
    "NotImplementedError",
    "OSError",
    "OverflowError",
    "RuntimeError",
    "StopIteration",
    "TypeError",
    "ValueError",
    "ZeroDivisionError",
)
BUILTINS = {name: getattr(builtins, name) for name in KINDS}


def names(expr):
    parts = expr.elts if isinstance(expr, ast.Tuple) else [expr]
    if parts and all(
        isinstance(node, ast.Name) and node.id in BUILTINS for node in parts
    ):
        return frozenset(node.id for node in parts)
    return None


def changed_handler(before, after, equal):
    if equal(before, after):
        return []
    if isinstance(before, ast.ExceptHandler) and isinstance(after, ast.ExceptHandler):
        a, b = names(before.type), names(after.type)
        if (
            a is not None
            and b is not None
            and before.name == after.name
            and equal(before.body, after.body)
        ):
            return [(before, a, b)]
    if type(before) is not type(after):
        raise ValueError("unsupported edit")
    if isinstance(before, list) and len(before) == len(after):
        return [x for a, b in zip(before, after) for x in changed_handler(a, b, equal)]
    if isinstance(before, ast.AST):
        return [
            x
            for k in before._fields
            for x in changed_handler(getattr(before, k), getattr(after, k), equal)
        ]
    raise ValueError("different code outside the catch type")


@lru_cache(maxsize=1)
def profile():
    return json.loads(Path(__file__).with_name("exceptions.json").read_bytes())


def refine(selector, changes, selected, apply_change, equal):
    if len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return selected
    # Cheap guard before loading profiles or parsing patched source.
    if not any(
        line.startswith(("+", "-")) and line[1:].lstrip().startswith("except ")
        for hunk in change["hunks"]
        for line in hunk["lines"]
    ):
        return selected
    try:
        data = profile()
        source, old = selector.source(path)
        if (
            data["commit"] != selector.mapping["commit"]
            or data["sources"].get(path) != selector.mapping["source_hashes"].get(path)
            or path not in data["sites"]
        ):
            return selected
        # Extra inserted lines can change locations reported by runtime inspection.
        if any(
            sum(line.startswith("+") for line in h["lines"])
            != sum(line.startswith("-") for line in h["lines"])
            for h in change["hunks"]
        ):
            return selected
        new = ast.parse(apply_change(source, change)[0])
        edits = changed_handler(old, new, equal)
        if not edits:
            return selected
        keep = set(selector.unknown)
        if path in selector.files:
            keep.update(selector.sets[selector.files[path]])
        by_name = selector.by_name

        def add_ids(ids):
            keep.update(
                by_name[data["tests"][i]] for i in ids if data["tests"][i] in by_name
            )

        if -1 in data["opaque"]:
            return selected
        add_ids(data["opaque"])
        for node, before, after in edits:
            key = f"{node.lineno}:{node.col_offset}"
            site = data["sites"][path].get(key)
            if site is None or (before | after) & set(data["bindings"].get(path, [])):
                return selected
            # A caught error can influence shared state or caches used by later
            # tests. The test executing the exception is not its only consumer.
            if any(
                not any(issubclass(BUILTINS[name], BUILTINS[other]) for other in after)
                for name in before
            ):
                keep.update(selected)
            for lineage, ids in site.items():
                parents = set(lineage.split("|"))
                if lineage == "*" or bool(before & parents) != bool(after & parents):
                    keep.update(selected)
                    if -1 in ids:
                        # Collection or module initialization can affect every test.
                        return set(selector.all)
                    add_ids(ids)
        # These positive exception observations are independent recall evidence.
        return keep
    except (
        OSError,
        KeyError,
        ValueError,
        TypeError,
        IndexError,
        UnicodeError,
        SyntaxError,
        RecursionError,
    ):
        return selected
