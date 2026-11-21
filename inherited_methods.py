"""Narrow deletions of ordinary method overrides using pristine caller observations."""

import ast
from functools import lru_cache
import json
from pathlib import Path
import module_footprints


@lru_cache(maxsize=1)
def catalog():
    return json.loads(Path(__file__).with_name("inherited-methods.json").read_bytes())


def deleted_methods(tree, modified, cls, equal):
    if len(tree.body) != len(modified.body):
        return None
    cuts = []
    for old, new in zip(tree.body, modified.body):
        if equal(old, new):
            continue
        if not (
            isinstance(old, ast.ClassDef)
            and isinstance(new, ast.ClassDef)
            and old.name == cls
            and not old.decorator_list
            and not old.keywords
            and module_footprints._same_header(old, new, equal)
        ):
            return None
        rest = iter(new.body)
        current = next(rest, None)
        for n in old.body:
            if equal(n, current):
                current = next(rest, None)
            elif (
                isinstance(n, ast.FunctionDef)
                and not n.decorator_list
                and not n.type_params
                and not n.args.defaults
                and not any(n.args.kw_defaults)
                and n.args.args
                and n.args.args[0].arg == "self"
                and not n.name.startswith("__")
            ):
                cuts.append(n)
            else:
                return None
        if current is not None:
            return None
    return cuts or None


def refine(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    c = changes[0]
    path = c["old"]
    if path != c["new"] or not path.startswith(
        ("sqlglot/generators/", "sqlglot/parsers/")
    ):
        return selected
    base = module_footprints._catalog()
    digest = base["hashes"].get(path)
    if (
        not digest
        or base["commit"] != selector.mapping["commit"]
        or path not in base["leaf"]
        or path in selector.files
        or module_footprints._unopened()["unopened"].get(path) != digest
        or selector.mapping["source_hashes"].get(path) != digest
    ):
        return selected
    source, tree = selector.source(path)
    modified, removed, added = apply(source, c)
    if added or not removed:
        return selected
    edits = deleted_methods(tree, ast.parse(modified), base["leaf"][path], equal)
    if not edits:
        return selected
    observations = catalog()
    if observations["commit"] != base["commit"]:
        return selected
    full = observations["methods"].get(path, {})
    if any(n.name not in full for n in edits):
        return selected
    groups = base["readers"][path]
    possible = {
        selector.by_name[name]
        for g in groups
        for name in base["groups"][g]
        if name in selector.by_name
    }
    possible.update(selector.file_tests(path))
    live = set()
    import_lines = set(selector.mapping["import_lines"].get(path, []))
    for n in edits:
        body = set(range(n.body[0].lineno, n.end_lineno + 1))
        if import_lines & body:
            return selected
        live.update(selector.line_tests(path, body))
        for other, lines in full[n.name].items():
            if not other.startswith("sqlglot/") and not other.startswith("tests/"):
                return selected
            selector.load_shard(other)
            if set(lines) & set(selector.mapping["import_lines"].get(other, [])):
                return selected
            if other.startswith("tests/"):
                live.update(t["number"] for t in selector.tests if t["file"] == other)
            else:
                live.update(selector.line_tests(other, lines))
    # Preserve subprocess observations and whole-registry or reflective users.
    ext = selector.dispatch_map()
    live.update(ext.external | ext.external_by_source.get(path, set()))
    sensitive = set(observations["reflective_test_files"]) | set(
        base["registry_test_files"]
    )
    for other, lines in base["registry_read_lines"].items():
        selector.load_shard(other)
        live.update(possible & selector.line_tests(other, lines))
    live.update(
        t["number"]
        for t in selector.tests
        if t["file"] in sensitive and t["number"] in possible
    )
    return selected & live
