"""Add transitive users of published SQLGlot class data when its structure changes."""

import ast
from functools import lru_cache
import hashlib
import json
import re
from pathlib import Path


@lru_cache(maxsize=3)
def observations(name):
    return json.loads(Path(__file__).with_name(name).read_bytes())


def plain_schema(expr):
    if not isinstance(expr, ast.Dict):
        return False
    keys = [
        n.value
        for n in expr.keys
        if isinstance(n, ast.Constant) and type(n.value) is str
    ]
    return len(keys) == len(set(keys)) == len(expr.keys) and all(
        isinstance(v, ast.Constant) and type(v.value) is bool for v in expr.values
    )


def schema_users(selector, path, cls, old, new, text):
    if (old is not None and not plain_schema(old)) or (
        new is not None and not plain_schema(new)
    ):
        return selector.all
    catalog = observations("expression-schemas.json")
    row = catalog.get("classes", {}).get(path, {}).get(cls, {})
    if (
        "schema" not in row
        or catalog["commit"] != selector.mapping["commit"]
        or hashlib.sha256(text.encode()).hexdigest() != catalog["sources"].get(path)
    ):
        return selector.all
    extra = selector.dispatch_map()
    keep = (
        set(selector.unknown)
        | extra.external
        | extra.external_by_source.get(path, set())
    )
    for i in catalog["sets"][row["schema"]]:
        name = catalog["tests"][i]
        if name in selector.by_name:
            keep.add(selector.by_name[name])
    if path in selector.files:
        keep.update(selector.sets[selector.files[path]])
    return keep


def assignments(tree, field):
    values = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for node in cls.body:
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            if any(isinstance(t, ast.Name) and t.id == field for t in targets):
                values[cls.name] = node.value
    return values


def local_keys(node):
    if not isinstance(node, ast.Dict):
        return None
    keys = {}
    for k, v in zip(node.keys, node.values):
        if k is None:  # Mapping expansion is handled separately.
            continue
        if (
            not isinstance(k, ast.Attribute)
            or not isinstance(k.value, ast.Name)
            or k.value.id != "exp"
        ):
            return None
        if k.attr in keys:
            return None
        keys[k.attr] = v
    return keys


def expansions(node):
    if not isinstance(node, ast.Dict):
        return None
    return [ast.dump(v) for k, v in zip(node.keys, node.values) if k is None]


def members(node):
    return list(ast.literal_eval(node).items()) if plain_schema(node) else None


def same_effective_schema(data, path, name, old, new):
    # An override can reproduce the ordered, typed values inherited by the leaf.
    row = data["structural"]["inherited"].get(path, {}).get(name)
    if row is None:
        return False
    inherited = [tuple(x) for x in row]
    a = members(old) if old is not None else inherited
    b = members(new) if new is not None else inherited
    return a is not None and a == b


def only_leaf_change(old, new, name, equal):
    # Reflection and sharing were monitored for leaf schemas in the pinned suite.
    import expression_schema

    if len(old.body) != len(new.body):
        return False
    for a, b in zip(old.body, new.body):
        if equal(a, b):
            continue
        if not (
            isinstance(a, ast.ClassDef)
            and isinstance(b, ast.ClassDef)
            and a.name == b.name == name
            and equal(a.bases, b.bases)
            and expression_schema.fields(a) is not None
            and expression_schema.fields(b) is not None
        ):
            return False
    return True


def table_observers(selector, data, path, owner, keys):
    metadata = data["structural"]
    if keys & set(metadata["dialect_init_keys"]):
        return selector.all
    rows = data["rows"]
    copies = {owner}
    todo = [owner]
    while todo:
        extra = set(metadata["copies"].get(todo.pop(), ())) - copies
        copies.update(extra)
        todo.extend(extra)
    keep = set(selector.unknown)
    external = selector.dispatch_map()
    keep.update(external.external)
    keep.update(external.external_by_source.get(path, ()))
    if path in selector.files:
        keep.update(selector.sets[selector.files[path]])
    for cls in copies:
        groups = rows.get(cls, data["across"])
        for name in keys:
            seen = data["sets"][groups[name]] if name in groups else ()
            if -1 in seen:
                return selector.all
            keep.update(
                selector.by_name[data["tests"][i]]
                for i in seen
                if data["tests"][i] in selector.by_name
            )
    # These consumers bypass Generator.sql and its receiver+expression lookup.
    footprints = observations("module-footprints.json")
    if footprints["commit"] != data["commit"] or path not in footprints["readers"]:
        return selector.all
    module_users = {
        selector.by_name[t]
        for i in footprints["readers"][path]
        for t in footprints["groups"][i]
        if t in selector.by_name
    }
    for src, line, literal_keys in metadata["direct_reads"]:
        if literal_keys and not keys.intersection(literal_keys):
            continue
        selector.load_shard(src)
        if line in selector.mapping.get("import_lines", {}).get(src, ()):
            return selector.all
        keep.update(selector.line_tests(src, [line]) & module_users)
    return keep


def bare_field(node, field):
    if isinstance(node, ast.AnnAssign):
        targets = [node.target]
    elif isinstance(node, ast.Assign):
        targets = node.targets
    else:
        return False
    return (
        len(targets) == 1
        and isinstance(targets[0], ast.Name)
        and targets[0].id == field
    )


def type_mapping_readers(selector, path, old, new, equal):
    # The pinned generator and cast builder read TYPE_MAPPING by dtype. The
    # baseline records those lookups, including origins preserved by dict unpack.
    if not (bare_field(old, "TYPE_MAPPING") and bare_field(new, "TYPE_MAPPING")):
        return None
    if type(old) is not type(new) or not all(
        equal(getattr(old, n), getattr(new, n)) for n in old._fields if n != "value"
    ):
        return None
    a, b = old.value, new.value
    if not (
        isinstance(a, ast.Dict) and isinstance(b, ast.Dict) and equal(a.keys, b.keys)
    ):
        return None
    reads = set()
    for key, x, y in zip(a.keys, a.values, b.values):
        if equal(x, y):
            continue
        if not (
            isinstance(key, ast.Attribute)
            and isinstance(key.value, ast.Attribute)
            and isinstance(key.value.value, ast.Name)
            and key.value.value.id == "exp"
            and key.value.attr == "DType"
            and isinstance(x, ast.Constant)
            and type(x.value) is str
            and isinstance(y, ast.Constant)
            and type(y.value) is str
        ):
            return None
        region = f"{key.lineno}:{key.col_offset}:{x.end_lineno}:{x.end_col_offset}"
        slot = selector.mapping.get("regions", {}).get(path, {}).get(region)
        if slot is None:
            return None
        reads.update(selector.sets[slot])
    return reads


def deferred_handlers_only(old_cls, new_cls, equal):
    # For a compound change, keep import fallbacks if the new table evaluates
    # a different key, default, binding, expansion, or factory at class creation.
    old_values = [n.value for n in old_cls.body if bare_field(n, "TRANSFORMS")]
    new_values = [n.value for n in new_cls.body if bare_field(n, "TRANSFORMS")]
    if len(old_values) != 1 or len(new_values) != 1:
        return False
    before, after = old_values[0], new_values[0]
    if not (
        isinstance(before, ast.Dict)
        and isinstance(after, ast.Dict)
        and equal(before.keys, after.keys)
    ):
        return False
    return all(
        equal(x, y)
        or (
            key is not None
            and isinstance(x, ast.Lambda)
            and isinstance(y, ast.Lambda)
            and equal(x.args, y.args)
        )
        for key, x, y in zip(before.keys, before.values, after.values)
    )


def companion_table_readers(selector, change, changes, old, new, cls, field, equal):
    if field == "arg_types" and only_leaf_change(old, new, cls, equal):
        return set()
    if len(old.body) != len(new.body):
        return None
    extra = set()
    for a, b in zip(old.body, new.body):
        if equal(a, b):
            continue
        if not (
            isinstance(a, ast.ClassDef)
            and isinstance(b, ast.ClassDef)
            and a.name == b.name == cls
            and all(
                equal(getattr(a, n), getattr(b, n)) for n in a._fields if n != "body"
            )
        ):
            return None
        for d in (a, b):
            if any(
                isinstance(n, ast.AnnAssign) and bare_field(n, field) for n in d.body
            ):
                return None
        ca = [
            n for n in a.body if not (bare_field(n, field) or isinstance(n, ast.Pass))
        ]
        cb = [
            n for n in b.body if not (bare_field(n, field) or isinstance(n, ast.Pass))
        ]
        if len(ca) != len(cb):
            return None
        for x, y in zip(ca, cb):
            if equal(x, y):
                continue
            if (
                field != "TRANSFORMS"
                or not deferred_handlers_only(a, b, equal)
                or companion_reads(changes, change, cls, "TYPE_MAPPING")
            ):
                return None
            readers = type_mapping_readers(selector, change["old"], x, y, equal)
            if readers is None:
                return None
            extra.update(readers)
    return extra


def inert_value(node):
    if node is None or isinstance(node, ast.Name):
        return True
    if isinstance(node, ast.Attribute):
        # A static reference to a pinned expression's constructor. Exotic
        # descriptors can run at import and so require full protection.
        return (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "exp"
            and node.attr == "from_arg_list"
        )
    if isinstance(node, ast.Lambda):
        return all(
            v is None or isinstance(v, ast.Constant)
            for v in [*node.args.defaults, *node.args.kw_defaults]
        )
    return False


def safe_initializer(selector, path, key, before, after, equal):
    a, b = before.get(key), after.get(key)
    if inert_value(a) and inert_value(b):
        return True
    if not isinstance(a, ast.Call) or not isinstance(b, ast.Call):
        return False
    from dispatch_coverage import deferred_edit

    if not deferred_edit(a, b, equal):
        return False
    # The original factory returned a callable and passed its separate
    # baseline factory-purity check. The new call has the same factory
    # and only literal argument substitutions.
    location = f"{a.lineno}:"
    for region in selector.dispatch_map().rows.get(path, {}):
        if region.startswith(location) and region.endswith(
            f":{a.end_lineno}:{a.end_col_offset}"
        ):
            return True
    return False


def companion_reads(changes, current, name, field):
    # An accompanying producer or new explicit accessor invalidates a single-table
    # baseline. Independent fixture and production changes keep their own selection.
    search = re.compile(
        r"\b(?:"
        + re.escape(name)
        + "|"
        + field
        + ("|required_args" if field == "arg_types" else "|_dispatch")
        + r")\b"
    )
    for companion in changes:
        if companion is current or not companion["old"].endswith(".py"):
            continue
        if companion["old"] == current["old"]:
            return True
        for h in companion["hunks"]:
            for line in h["lines"]:
                if (
                    line.startswith("+")
                    and not line[1:].lstrip().startswith("#")
                    and search.search(line[1:])
                ):
                    return True
    return False


def strengthen(selector, changes, selected, apply, equal):
    if len(selected) == len(selector.all) or not changes:
        return selected
    result = set(selected)
    for change in changes:
        path = change["old"]
        if (
            path != change["new"]
            or not path.endswith(".py")
            or not path.startswith(("sqlglot/expressions/", "sqlglot/generators/"))
        ):
            continue
        text, old = selector.source(path)
        schema = path.startswith("sqlglot/expressions/")
        field = "arg_types" if schema else "TRANSFORMS"
        before = assignments(old, field)
        edits = [
            line
            for h in change["hunks"]
            for line in h["lines"]
            if line.startswith(("+", "-"))
        ]
        mentions_name = any(field in line for line in edits)
        if not before and not mentions_name:
            continue
        new_text, removed, inserted = apply(text, change)
        spans = [(v.lineno, v.end_lineno) for v in before.values() if v is not None]
        if not mentions_name and not any(
            lo <= n <= hi for n in removed | inserted for lo, hi in spans
        ):
            continue
        new = ast.parse(new_text)
        after = assignments(new, field)
        for cls in before.keys() | after.keys():
            a, b = before.get(cls), after.get(cls)
            if equal(a, b):
                continue
            if companion_reads(changes, change, cls, field):
                return set(selector.all)
            other_readers = companion_table_readers(
                selector, change, changes, old, new, cls, field, equal
            )
            if other_readers is None:
                return set(selector.all)
            result.update(other_readers)
            data = observations("generator-dispatch-reads.json")
            if data["commit"] != selector.mapping["commit"]:
                return set(selector.all)
            if schema:
                oldcls = {v.name for v in old.body if isinstance(v, ast.ClassDef)}
                newcls = {v.name for v in new.body if isinstance(v, ast.ClassDef)}
                if cls not in oldcls & newcls:
                    return set(selector.all)
                keep = schema_users(selector, path, cls, a, b, text)
                if keep is selector.all:
                    return set(selector.all)
                if (
                    (a is None or b is None)
                    and same_effective_schema(data, path, cls, a, b)
                    and only_leaf_change(old, new, cls, equal)
                ):
                    # Baseline leaf reflection and mutation checks passed and ordered
                    # contents match. Preserve passing native probes of this leaf.
                    continue
            else:
                if hashlib.sha256(text.encode()).hexdigest() != data["sources"].get(
                    path
                ):
                    return set(selector.all)
                ka, kb = local_keys(a), local_keys(b)
                if ka is None or kb is None or expansions(a) != expansions(b):
                    return set(selector.all)
                keys = ka.keys() ^ kb.keys()
                keys.update(k for k in ka.keys() & kb.keys() if not equal(ka[k], kb[k]))
                if not keys:
                    # Reordering a literal key relative to an unpack can change the winner.
                    return set(selector.all)
                if not all(
                    safe_initializer(selector, path, k, ka, kb, equal) for k in keys
                ):
                    return set(selector.all)
                owner = path[:-3].replace("/", ".") + ":" + cls
                keep = table_observers(selector, data, path, owner, keys)
            if keep is selector.all:
                return set(selector.all)
            result.update(keep)
    return result
