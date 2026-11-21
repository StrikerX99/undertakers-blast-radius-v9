"""Resolve changes to leaf expression schemas against their observed consumers."""

import ast
from functools import lru_cache
import json
from pathlib import Path


def fields(cls):
    if (
        not isinstance(cls, ast.ClassDef)
        or cls.decorator_list
        or cls.keywords
        or not cls.bases
        or not all(isinstance(base, ast.Name) for base in cls.bases)
        or getattr(cls, "type_params", None)
    ):
        return None
    doc, schema = "", None
    for idx, node in enumerate(cls.body):
        if isinstance(node, ast.Pass):
            continue
        if (
            idx == 0
            and isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and type(node.value.value) is str
        ):
            doc = node.value.value
            continue
        if (
            not isinstance(node, ast.Assign)
            or len(node.targets) != 1
            or not isinstance(node.targets[0], ast.Name)
            or node.targets[0].id != "arg_types"
            or not isinstance(node.value, ast.Dict)
            or schema is not None
        ):
            return None
        schema = []
        for k, v in zip(node.value.keys, node.value.values):
            if (
                not isinstance(k, ast.Constant)
                or type(k.value) is not str
                or not k.value.isidentifier()
                or not isinstance(v, ast.Constant)
                or type(v.value) is not bool
            ):
                return None
            schema.append((k.value, v.value))
        if len(schema) != len(dict(schema)):
            return None
    return doc, schema


@lru_cache(maxsize=2)
def read(name):
    return json.loads(Path(__file__).with_name(name).read_bytes())


def verify(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/expressions/")
        or not path.endswith(".py")
    ):
        return selected
    data = read("expression-schemas.json")
    raw = read("unopened-sources.json")
    digest = selector.mapping["source_hashes"].get(path)
    if (
        not digest
        or data["commit"] != selector.mapping["commit"]
        or raw["commit"] != data["commit"]
        or digest != raw["unopened"].get(path)
        or digest != data["sources"].get(path)
        or path in selector.files
    ):
        return selected
    source, old = selector.source(path)
    after, _, _ = apply(source, change)
    new = ast.parse(after)
    if len(old.body) != len(new.body):
        return selected
    ids = set()
    touched = False
    for a, b in zip(old.body, new.body):
        if equal(a, b):
            continue
        left, right = fields(a), fields(b)
        if (
            left is None
            or right is None
            or a.name != b.name
            or not equal(a.bases, b.bases)
        ):
            return selected
        row = data["classes"].get(path, {}).get(a.name)
        if row is None:
            return selected
        for key, x, y in zip(("doc", "schema"), left, right):
            if x == y:
                continue
            group = row.get(key)
            if group is None:
                return selected
            ids.update(data["tests"][j] for j in data["sets"][group])
            touched = True
    if not touched:
        return selected
    if any(i not in selector.by_name for i in ids):
        return selected
    dispatch = selector.dispatch_map()
    return selected & (
        dispatch.external
        | dispatch.external_by_source.get(path, set())
        | {selector.by_name[i] for i in ids}
    )
