"""Select actual consumers of changed fixed expression return types in SQLGlot metadata."""

import ast
from collections import defaultdict
from functools import lru_cache
import json
from pathlib import Path

import composite_dialects
import dormant_settings
import inherited_methods

FIELD = "EXPRESSION_METADATA"


@lru_cache(maxsize=1)
def profile():
    return json.loads(Path(__file__).with_name("metadata-inputs.json").read_bytes())


def returned(value, types):
    if not isinstance(value, ast.Dict) or len(value.keys) != 1:
        return None
    field, spec = value.keys[0], value.values[0]
    if not isinstance(field, ast.Constant) or field.value != "returns":
        return None
    if composite_dialects._enum_name(spec) and spec.attr in types:
        return spec.attr
    return None


def entries(mapping, names):
    spine = []
    effects = defaultdict(list)
    for key, value in zip(mapping.keys, mapping.values):
        exprs = []
        dtype = None
        if composite_dialects._exp_name(key):
            exprs = [key]
            dtype = returned(value, names["dtypes"])
        elif (
            key is None
            and isinstance(value, ast.DictComp)
            and len(value.generators) == 1
        ):
            loop = value.generators[0]
            if (
                not loop.ifs
                and not loop.is_async
                and isinstance(loop.target, ast.Name)
                and loop.target.id != "exp"
                and isinstance(value.key, ast.Name)
                and loop.target.id == value.key.id
                and isinstance(loop.iter, (ast.Set, ast.List, ast.Tuple))
            ):
                exprs = loop.iter.elts
                dtype = returned(value.value, names["dtypes"])
        if (
            dtype
            and exprs
            and all(
                composite_dialects._exp_name(x) and x.attr in names["classes"]
                for x in exprs
            )
        ):
            for x in exprs:
                effects[x.attr].append((len(spine), dtype))
        else:
            spine.append((ast.dump(key) if key else None, ast.dump(value)))
    return spine, effects


def changed_keys(before, after, names, equal):
    if len(before.body) != len(after.body):
        return None
    changes = [(a, b) for a, b in zip(before.body, after.body) if not equal(a, b)]
    if len(changes) != 1:
        return None
    left, right = changes[0]
    if type(left) is not type(right) or not isinstance(
        left, (ast.Assign, ast.AnnAssign)
    ):
        return None
    if any(
        not equal(getattr(left, f), getattr(right, f))
        for f in left._fields
        if f != "value"
    ):
        return None
    target = (
        left.target
        if isinstance(left, ast.AnnAssign)
        else left.targets[0]
        if len(left.targets) == 1
        else None
    )
    if (
        not isinstance(target, ast.Name)
        or target.id != FIELD
        or not all(isinstance(n.value, ast.Dict) for n in (left, right))
    ):
        return None
    a, old = entries(left.value, names)
    b, new = entries(right.value, names)
    if a != b:
        return None
    return {k for k in old.keys() | new.keys() if old.get(k) != new.get(k)}


def verify(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if not path.startswith("sqlglot/typing/") or path != change["new"]:
        return selected
    data = profile()
    names = composite_dialects._names()
    raw = dormant_settings._sources()
    pin = selector.mapping["commit"]
    src = data["sources"].get(path)
    if (
        not src
        or data["pin"] != pin
        or names["pin"] != pin
        or raw["commit"] != pin
        or selector.mapping["source_hashes"].get(path) != src
        or raw["unopened"].get(path) != src
    ):
        return selected
    text, tree = selector.source(path)
    keys = changed_keys(tree, ast.parse(apply(text, change)[0]), names, equal)
    if not keys:
        return selected
    indices = set()
    for name in keys | {"*"}:
        indices.update(data["readers"].get(name, ()))
    if -1 in indices:
        return selected
    external = selector.dispatch_map()
    keep = set(external.external) | external.external_by_source.get(path, set())
    if path in selector.files:
        keep.update(selector.sets[selector.files[path]])
    keep.update(
        selector.by_name[name]
        for i in indices
        if (name := data["names"][i]) in selector.by_name
    )
    sensitive = set(inherited_methods.catalog()["reflective_test_files"])
    keep.update(t["number"] for t in selector.tests if t["file"] in sensitive)
    return selected & keep
