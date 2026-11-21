"""Select native readers of generator handlers when their private implementation moves."""

import ast
import copy
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path

import dialect_initializers
import general_dialects
import inherited_methods
import module_footprints
import structural_dependencies


@lru_cache(maxsize=1)
def symbols():
    data = json.loads(Path(__file__).with_name("generator-symbols.json").read_bytes())
    data["private_definitions"] = frozenset(data["private_definitions"])
    return data


def private(name):
    return name.startswith("_") and not name.startswith("__")


def functions(body):
    values = [n for n in body if isinstance(n, ast.FunctionDef) and private(n.name)]
    by_name = {n.name: n for n in values}
    return by_name if len(by_name) == len(values) else None


def changed_functions(before, after, equal):
    old, new = functions(before), functions(after)
    if old is None or new is None:
        return None
    return {
        key for key in old.keys() | new.keys() if not equal(old.get(key), new.get(key))
    }


def type_guard(stmt, aliases):
    return (
        isinstance(stmt, ast.If)
        and not stmt.orelse
        and isinstance(stmt.test, ast.Attribute)
        and stmt.test.attr == "TYPE_CHECKING"
        and isinstance(stmt.test.value, ast.Name)
        and stmt.test.value.id in aliases
        and all(
            isinstance(n, ast.ImportFrom)
            and n.level == 0
            and n.module
            and n.module.startswith("sqlglot.")
            and all(a.name != "*" for a in n.names)
            for n in stmt.body
        )
    )


def import_rows(tree, catalog):
    templates = {}
    decorators = set()
    typing = set()
    imports = {}
    for n in tree.body:
        if isinstance(n, ast.Import):
            typing.update(a.asname or a.name for a in n.names if a.name == "typing")
        if isinstance(n, ast.ImportFrom) and n.level == 0:
            for a in n.names:
                bound = a.asname or a.name
                if a.name in catalog["constants"].get(n.module, []):
                    if bound in templates:
                        return None
                    templates[bound] = (n.module, a.name)
                    imports[n] = True
                if n.module == "sqlglot.generator" and a.name == "unsupported_args":
                    decorators.add(bound)
    watched = set(templates) | decorators | typing
    bindings = Counter()
    for n in tree.body:
        nodes = [n] if isinstance(n, (ast.FunctionDef, ast.ClassDef)) else ast.walk(n)
        for v in nodes:
            if isinstance(v, ast.alias):
                name = v.asname or v.name.split(".")[0]
            elif isinstance(v, (ast.ClassDef, ast.FunctionDef)):
                name = v.name
            elif isinstance(v, ast.Name) and isinstance(v.ctx, (ast.Store, ast.Del)):
                name = v.id
            else:
                continue
            if name in watched:
                bindings[name] += 1
    if any(bindings[k] != 1 for k in watched):
        return None
    return templates, decorators, typing, imports


def safe_helper(node, decorators, templates, owned):
    """Check deferred helpers and allow SQLGlot's passive unsupported-argument decorator."""
    if any(
        not (
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Name)
            and d.func.id in decorators
            and bool(d.args)
            and not d.keywords
            and all(
                isinstance(a, ast.Constant) and type(a.value) is str for a in d.args
            )
        )
        for d in node.decorator_list
    ):
        return False
    if not node.args.args:
        return False
    receiver = node.args.args[0].arg
    # The generator instance can be shared; a changed handler must not install new
    # state on it, its dialect or a process-global mutable container. SQLGlot's
    # rendering methods and its diagnostic reporter are the only admitted calls
    # through that receiver, plus other checked private methods in this change.
    local_copy = {}
    for stmt in node.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Call)
            and not stmt.value.args
            and not stmt.value.keywords
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "copy"
            and isinstance(stmt.value.func.value, ast.Name)
            and stmt.value.func.value.id == stmt.targets[0].id
        ):
            local_copy[stmt.targets[0].id] = stmt.lineno
    mutators = {
        "set",
        "append",
        "extend",
        "insert",
        "add",
        "update",
        "clear",
        "pop",
        "popitem",
        "remove",
        "discard",
        "setdefault",
    }
    allowed = {
        "sql",
        "func",
        "unsupported",
        "expressions",
        "binary",
        "unary",
        "sep",
        "seg",
    }
    for stmt in node.body:
        for v in ast.walk(stmt):
            if isinstance(v, (ast.Attribute, ast.Subscript)) and isinstance(
                v.ctx, (ast.Store, ast.Del)
            ):
                return False
            if not isinstance(v, ast.Call) or not isinstance(v.func, ast.Attribute):
                continue
            f = v.func
            if general_dialects._root(f) == receiver and not (
                isinstance(f.value, ast.Name)
                and f.value.id == receiver
                and (f.attr in allowed or f.attr.endswith("_sql") or f.attr in owned)
            ):
                return False
            if f.attr in mutators and not (
                f.attr == "set"
                and isinstance(f.value, ast.Name)
                and f.value.id in local_copy
                and local_copy[f.value.id] < v.lineno
            ):
                return False

    class LocalReads(ast.NodeTransformer):
        def visit_Attribute(self, value):
            if (
                value.attr == "__name__"
                and isinstance(value.value, ast.Attribute)
                and value.value.attr == "__class__"
                and isinstance(value.value.value, ast.Name)
                and value.value.value.id in {a.arg for a in node.args.args}
            ):
                return ast.copy_location(ast.Constant(value=""), value)
            return self.generic_visit(value)

        def visit_Call(self, value):
            self.generic_visit(value)
            if (
                isinstance(value.func, ast.Attribute)
                and value.func.attr == "format"
                and isinstance(value.func.value, ast.Name)
                and value.func.value.id in templates
            ):
                value.func = ast.Name(id="str", ctx=ast.Load())
            return value

    safe = LocalReads().visit(copy.deepcopy(node))
    safe.decorator_list = []
    return general_dialects._ordinary(safe)


def mentions(node, names):
    return any(
        (isinstance(n, ast.Name) and n.id in names)
        or (isinstance(n, ast.Attribute) and n.attr in names)
        or (isinstance(n, ast.Constant) and type(n.value) is str and n.value in names)
        for n in ast.walk(node)
    )


def class_table(cls):
    tables = [
        n for n in cls.body if structural_dependencies.bare_field(n, "TRANSFORMS")
    ]
    return tables[0] if len(tables) == 1 and isinstance(tables[0], ast.Assign) else None


def normalized(tree, cls, table, top, methods, keys, analyzed, imports):
    # Removing the owned code leaves all remaining module/class statements in order.
    templates, decorators, aliases, imported_constants = imports
    result = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in top:
            if not safe_helper(n, decorators, templates, methods):
                return None
            continue
        if type_guard(n, aliases):
            continue
        if n in imported_constants:
            taken = [a for a in n.names if (a.asname or a.name) not in analyzed]
            if len(taken) != len(n.names):
                if not taken:
                    # Importing a new module could initialize state elsewhere.
                    return None
                n = copy.copy(n)
                n.names = taken
        if n is cls:
            c = copy.copy(cls)
            c.body = []
            for entry in cls.body:
                if isinstance(entry, ast.FunctionDef) and entry.name in methods:
                    if not safe_helper(entry, decorators, templates, methods):
                        return None
                    continue
                if entry is table:
                    ds = copy.copy(table.value)
                    ds.values = [
                        ast.Constant(value=None)
                        if (isinstance(k, ast.Attribute) and k.attr in keys)
                        else v
                        for k, v in zip(ds.keys, ds.values)
                    ]
                    entry = copy.copy(entry)
                    entry.value = ds
                c.body.append(entry)
            n = c
        if mentions(n, top | methods | analyzed):
            return None
        result.append(n)
    return result


def passive_dispatch(selector, path, tree, after, equal):
    """Bound passive transform-table edits even when two dialect helper factories change."""
    if not dialect_initializers.eligible(selector, path, tree, after, equal):
        return None
    old_classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    new_classes = {n.name: n for n in after.body if isinstance(n, ast.ClassDef)}
    changed = [
        name
        for name in old_classes
        if not equal(old_classes[name], new_classes.get(name))
    ]
    if len(changed) != 1:
        return None
    name = changed[0]
    if name not in general_dialects._manifest()["classes"].get(path, {}):
        return None
    a, b = old_classes[name], new_classes[name]
    before, now = class_table(a), class_table(b)
    if before is None or now is None or len(a.body) != len(b.body):
        return None
    # All other class members, including computed companion mappings, retain
    # their source order, declarations and initial values.
    if any(
        (left is before and right is not now)
        or (left is not before and not equal(left, right))
        for left, right in zip(a.body, b.body)
    ):
        return None
    old = structural_dependencies.local_keys(before.value)
    new = structural_dependencies.local_keys(now.value)
    if (
        old is None
        or new is None
        or structural_dependencies.expansions(before.value)
        != structural_dependencies.expansions(now.value)
    ):
        return None
    shared = old.keys() & new.keys()

    def remaining_order(mapping):
        return [
            k.attr if k is not None else None
            for k in mapping.keys
            if k is None or k.attr in shared
        ]

    if remaining_order(before.value) != remaining_order(now.value):
        return None
    keys = old.keys() ^ new.keys()
    keys.update(k for k in shared if not equal(old[k], new[k]))
    return (name, keys, set()) if keys else None


def candidates(selector, path, tree, after, equal, index):
    source = path[:-3].replace("/", ".")
    classes = [
        [
            n
            for n in t.body
            if isinstance(n, ast.ClassDef) and n.name.endswith("Generator")
        ]
        for t in (tree, after)
    ]
    if any(len(row) != 1 for row in classes):
        return None
    a, b = classes[0][0], classes[1][0]
    if (
        a.name != b.name
        or a.name not in general_dialects._manifest()["classes"].get(path, {})
        or not module_footprints._same_header(a, b, equal)
    ):
        return None
    top = changed_functions(tree.body, after.body, equal)
    methods = changed_functions(a.body, b.body, equal)
    if top is None or methods is None:
        return None
    if not top:
        return (
            passive_dispatch(selector, path, tree, after, equal)
            if not methods
            else None
        )
    if not any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(v.name == "annotations" for v in n.names)
        for n in tree.body
    ):
        return None
    private_names = top | methods
    if any(set(index["private_attributes"].get(n, ())) - {path} for n in private_names):
        return None
    # A new class binding must not replace an inherited or dynamically-dispatched
    # method. An existing changed binding is admitted only if there is no other
    # implementation with this name in the pin's full source index.
    if methods & index["private_definitions"]:
        return None
    old_imports, new_imports = import_rows(tree, index), import_rows(after, index)
    if old_imports is None or new_imports is None:
        return None
    constants = {
        k
        for k in old_imports[0].keys() | new_imports[0].keys()
        if old_imports[0].get(k) != new_imports[0].get(k)
    }
    if any(set(index["public_attribute_users"].get(n, ())) - {path} for n in constants):
        return None
    outside_imports = set(index["generator_imports"].get(source, []))
    if (constants and "*" in outside_imports) or (
        (private_names | constants) & outside_imports
    ):
        return None
    ta, tb = class_table(a), class_table(b)
    if ta is None or tb is None or not equal(ta.value.keys, tb.value.keys):
        return None
    ka, kb = (
        structural_dependencies.local_keys(ta.value),
        structural_dependencies.local_keys(tb.value),
    )
    if (
        ka is None
        or kb is None
        or structural_dependencies.expansions(ta.value)
        != structural_dependencies.expansions(tb.value)
    ):
        return None
    keys = {
        key
        for key in ka
        if not equal(ka[key], kb[key])
        or mentions(ka[key], private_names)
        or mentions(kb[key], private_names)
    }
    if not keys:
        return None
    for fn, available, owner, info in [
        (ka, top, tree, old_imports),
        (kb, top, after, new_imports),
    ]:
        valid_top = set(functions(owner.body) or ())
        for key in keys:
            value = fn[key]
            if isinstance(value, ast.Name) and value.id in available & valid_top:
                continue
            if (
                isinstance(value, ast.Lambda)
                and not value.args.defaults
                and not any(value.args.kw_defaults)
            ):
                wrap = ast.FunctionDef(
                    name="_emit",
                    args=value.args,
                    body=[ast.Return(value.body)],
                    decorator_list=[],
                    returns=None,
                    type_comment=None,
                    type_params=[],
                )
                if safe_helper(wrap, set(), info[0], methods):
                    continue
            return None
    left = normalized(tree, a, ta, top, methods, keys, constants, old_imports)
    right = normalized(after, b, tb, top, methods, keys, constants, new_imports)
    if left is None or right is None or not equal(left, right):
        return None
    selector.load_shard(path)
    import_lines = set(selector.mapping["import_lines"].get(path, []))
    extra = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in top:
            body = range(node.body[0].lineno, node.end_lineno + 1)
            if not import_lines.isdisjoint(body):
                return None
            extra.update(selector.line_tests(path, body))
    return a.name, keys, extra


def refine(selector, changes, selected, apply, equal):
    if len(selected) < 20 or len(changes) != 1 or selector.unknown:
        return selected
    c = changes[0]
    path = c["old"]
    if (
        path != c["new"]
        or not path.startswith("sqlglot/generators/")
        or not path.endswith(".py")
    ):
        return selected
    data = symbols()
    if (
        data["pin"] != selector.mapping["commit"]
        or not data["sources"].get(path)
        or data["sources"][path] != selector.mapping["source_hashes"].get(path)
    ):
        return selected
    text, tree = selector.source(path)
    after = ast.parse(apply(text, c)[0])
    delta = candidates(selector, path, tree, after, equal, data)
    if delta is None:
        return selected
    owner, keys, extra = delta
    observed = structural_dependencies.observations("generator-dispatch-reads.json")
    if (
        observed["commit"] != data["pin"]
        or observed["sources"].get(path) != data["sources"][path]
    ):
        return selected
    users = (
        structural_dependencies.table_observers(
            selector, observed, path, path[:-3].replace("/", ".") + ":" + owner, keys
        )
        | extra
    )
    base = module_footprints._catalog()
    seen = set(base["registry_test_files"]) | set(
        inherited_methods.catalog()["reflective_test_files"]
    )
    users.update(t["number"] for t in selector.tests if t["file"] in seen)
    for other, lines in base["registry_read_lines"].items():
        selector.load_shard(other)
        users.update(selector.line_tests(other, lines))
    return selected & users
