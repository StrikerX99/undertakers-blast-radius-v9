"""Select original users of independently traced, class-local SQL generator changes."""

import ast
from functools import lru_cache
import json
import re
from pathlib import Path

import module_footprints


@lru_cache(maxsize=2)
def data(filename):
    return json.loads(Path(__file__).with_name(filename).read_bytes())


def header(left, right, equal):
    return type(left) is type(right) and all(
        equal(getattr(left, k), getattr(right, k)) for k in left._fields if k != "body"
    )


def setting(node):
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        tag = node.targets[0]
    elif isinstance(node, ast.AnnAssign):
        tag = node.target
    else:
        return None
    if not isinstance(tag, ast.Name) or not tag.id.isupper():
        return None
    return tag.id


def simple_result(node, local):
    """A read-only name computation or formatting return belonging to this call."""
    if isinstance(node, ast.Constant):
        return type(node.value) in (bool, int, str, float, type(None))
    if isinstance(node, ast.Name):
        return node.id in local
    if isinstance(node, ast.Attribute):
        return not node.attr.startswith("_") and simple_result(node.value, local)
    if isinstance(node, ast.BoolOp):
        return all(simple_result(v, local) for v in node.values)
    if isinstance(node, ast.Call):
        return (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "self"
            and node.func.attr in {"function_name", "normalize_func"}
            and all(simple_result(x, local) for x in node.args)
            and all(
                k.arg is not None and simple_result(k.value, local)
                for k in node.keywords
            )
        )
    return False


def rendered_only(left, right, equal):
    # Normalize each body's purely local rendering assignments and terminal
    # returns. Only variables used by erased output expressions can vary.
    if not header(left, right, equal):
        return False

    old_attrs = {v.attr for v in ast.walk(left) if isinstance(v, ast.Attribute)}
    new_attrs = {v.attr for v in ast.walk(right) if isinstance(v, ast.Attribute)}
    if new_attrs - old_attrs:
        return False

    # Assignment order and scoping can be observable. A changed rendering
    # temporary must have one simple definition and no uses in surviving code.
    def definitions(fn):
        result = {}
        for statement in fn.body:
            for n in ast.walk(statement):
                if isinstance(
                    n,
                    (
                        ast.FunctionDef,
                        ast.ClassDef,
                        ast.Lambda,
                        ast.Nonlocal,
                        ast.Global,
                    ),
                ):
                    return None
                if (
                    isinstance(n, ast.Assign)
                    and len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                ):
                    result.setdefault(n.targets[0].id, []).append(ast.dump(n.value))
        return result

    la, lb = definitions(left), definitions(right)
    if la is None or lb is None:
        return False
    changed = {k for k in la.keys() | lb.keys() if la.get(k) != lb.get(k)}
    if any(max(len(la.get(k, ())), len(lb.get(k, ()))) != 1 for k in changed):
        return False

    def canonical(fn):
        args = {a.arg for a in ast.walk(fn.args) if isinstance(a, ast.arg)}
        local = args | {
            n.id
            for n in ast.walk(fn)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)
        }

        class Mask(ast.NodeTransformer):
            def visit_Return(self, n):
                # Only the SQL function's rendered name may vary. Remaining
                # arguments can themselves be expression trees; serializing a
                # different argument or introducing an extra call can mutate it.
                call = n.value
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == "self"
                    and call.func.attr == "func"
                    and call.args
                    and simple_result(call.args[0], local)
                ):
                    call.args[0] = ast.Constant(None)
                return n

            def visit_Assign(self, n):
                if (
                    len(n.targets) == 1
                    and isinstance(n.targets[0], ast.Name)
                    and n.targets[0].id in changed - args
                    and simple_result(n.value, local)
                ):
                    return None
                return n

        import copy

        return Mask().visit(copy.deepcopy(fn))

    a, b = canonical(left), canonical(right)
    return equal(a, b) and not any(
        isinstance(n, ast.Name) and n.id in changed for n in ast.walk(a)
    )


def alias_import(node):
    return (
        isinstance(node, ast.ImportFrom)
        and not node.level
        and node.module == "sqlglot.generator"
        and len(node.names) == 1
        and node.names[0].name == "Generator"
        and node.names[0].asname is None
    )


def differences(path, tree, after, graph, profile, equal):
    before_nodes = tree.body
    if len(before_nodes) != len(after.body):
        if path not in graph["unreferenced_base_import"] or not any(
            alias_import(n) for n in before_nodes
        ):
            return None
        # All uses of this import must have disappeared with the changed body.
        if any(
            isinstance(n, ast.Name)
            and n.id == "Generator"
            or isinstance(n, ast.Constant)
            and type(n.value) is str
            and re.search(r"\bGenerator\b", n.value)
            for n in ast.walk(after)
        ):
            return None
        before_nodes = [n for n in before_nodes if not alias_import(n)]
    if len(before_nodes) != len(after.body):
        return None
    edits = []
    scalars = set()
    known = graph["types"].get(path, {})
    for left, right in zip(before_nodes, after.body):
        if equal(left, right):
            continue
        if not (
            isinstance(left, ast.ClassDef)
            and isinstance(right, ast.ClassDef)
            and left.name in known
            and header(left, right, equal)
        ):
            return None
        # Pair supported members in order; unrelated repeated class assignments
        # stay paired positionally. Only methods with known base implementations
        # can disappear, so every dispatch attribute remains callable.
        names = known[left.name]
        i = 0
        for old in left.body:
            new = right.body[i] if i < len(right.body) else None
            if equal(old, new):
                i += 1
                continue
            name = setting(old)
            if (
                name
                and name in profile["settings"]
                and path == "sqlglot/generator.py"
                and setting(new) == name
            ):
                if not (
                    type(old) is type(new)
                    and all(
                        equal(getattr(old, k), getattr(new, k))
                        for k in old._fields
                        if k != "value"
                    )
                    and isinstance(old.value, ast.Constant)
                    and isinstance(new.value, ast.Constant)
                    and type(old.value.value) is type(new.value.value)
                    and type(old.value.value) in (str, bool, int, float, type(None))
                ):
                    return None
                # The observer installs descriptors as soon as the Generator
                # class exists. Reads from its own class-definition namespace
                # precede installation and can initialize unrelated attributes.
                for item in left.body:
                    if not isinstance(
                        item, (ast.FunctionDef, ast.AsyncFunctionDef)
                    ) and any(
                        isinstance(n, ast.Name)
                        and isinstance(n.ctx, ast.Load)
                        and n.id == name
                        for n in ast.walk(item)
                    ):
                        return None
                scalars.add(name)
                i += 1
                continue
            if (
                not isinstance(old, ast.FunctionDef)
                or old.name not in names
                or old.decorator_list
                or old.type_params
            ):
                return None
            if any(
                n is not None and not isinstance(n, ast.Constant)
                for n in [*old.args.defaults, *old.args.kw_defaults]
            ):
                return None
            is_deletion = not (
                isinstance(new, ast.FunctionDef) and new.name == old.name
            )
            if is_deletion:
                if not names[old.name][1] or any(
                    isinstance(n, ast.FunctionDef) and n.name == old.name
                    for n in right.body
                ):
                    return None
            else:
                if not rendered_only(old, new, equal):
                    return None
                i += 1
            edits.append((left.name, old, names[old.name][0], is_deletion))
        if i != len(right.body):
            return None
    return (edits, scalars) if edits or scalars else None


def refine(selector, changes, selected, apply, equal):
    if len(selected) < 10 or selector.unknown or not changes:
        return selected
    paths = [c["old"] for c in changes]
    if len(set(paths)) != len(paths) or any(
        c["new"] != p
        or not (
            p == "sqlglot/generator.py"
            or p.startswith("sqlglot/generators/")
            and p.endswith(".py")
        )
        for c, p in zip(changes, paths)
    ):
        return selected
    profile = data("generator-contexts.json")
    graph = data("generator-hierarchy.json")
    if graph["pin"] != profile["pin"] or profile["pin"] != selector.mapping["commit"]:
        return selected
    rows = profile["rows"]
    observed = set()

    def take(key):
        if key not in rows:
            return True
        ids = profile["sets"][rows[key]]
        if -1 in ids:
            return False
        observed.update(
            selector.by_name[n]
            for i in ids
            if (n := profile["tests"][i]) in selector.by_name
        )
        return True

    ext = selector.dispatch_map()
    observed.update(ext.external)
    namespace = False
    for c, path in zip(changes, paths):
        if path not in graph["types"] or profile["sources"].get(
            path
        ) != selector.mapping["source_hashes"].get(path):
            return selected
        if path in selector.files:
            observed.update(selector.sets[selector.files[path]])
        elif (
            module_footprints._unopened()["unopened"].get(path)
            != profile["sources"][path]
        ):
            return selected
        src, tree = selector.source(path)
        updated, *_ = apply(src, c)
        result = differences(path, tree, ast.parse(updated), graph, profile, equal)
        if not result:
            return selected
        methods, fields = result
        observed.update(ext.external_by_source.get(path, ()))
        if fields:
            if profile.get("field_values", False):
                if not all(take("namespace:" + v) for v in fields):
                    return selected
            elif not (take("inspection") and take("namespace-keys-only")):
                return selected
            if not all(take("value:" + v) for v in fields):
                return selected
        for cls, node, expr, removed in methods:
            if not take(path + ":" + cls + "." + node.name):
                return selected
            body = range(node.body[0].lineno, node.end_lineno + 1)
            if set(body) & set(selector.mapping.get("import_lines", {}).get(path, ())):
                return selected
            observed.update(selector.line_tests(path, body))
            # Retain independently observed dynamic and direct callers, including
            # subclasses and calls in test module collection.
            dispatch = data_dispatch()
            if dispatch["commit"] != profile["pin"]:
                return selected
            groups = (
                dispatch["across"]
                if path == "sqlglot/generator.py"
                else dispatch["rows"].get(
                    path[:-3].replace("/", ".") + ":" + cls, dispatch["across"]
                )
            )
            if expr in groups:
                ids = dispatch["sets"][groups[expr]]
                if -1 in ids:
                    return selected
                observed.update(
                    selector.by_name[n]
                    for i in ids
                    if (n := dispatch["tests"][i]) in selector.by_name
                )
            if removed:
                namespace = True
                for other, lines in graph["noncall"].get(node.name, {}).items():
                    if other.startswith("tests/"):
                        observed.update(
                            t["number"] for t in selector.tests if t["file"] == other
                        )
                        continue
                    selector.load_shard(other)
                    if set(lines) & set(
                        selector.mapping["import_lines"].get(other, ())
                    ):
                        return selected
                    observed.update(selector.line_tests(other, lines))
    if namespace:
        sensitive = set(graph["reflected"]) | set(
            module_footprints._catalog()["registry_test_files"]
        )
        observed.update(t["number"] for t in selector.tests if t["file"] in sensitive)
        for path, lines in module_footprints._catalog()["registry_read_lines"].items():
            selector.load_shard(path)
            observed.update(selector.line_tests(path, lines))
    # Restrict only a schedule already covering the independently observed
    # dependencies. An unrelated precise path can keep its earlier result.
    return observed if observed <= selected else selected


def data_dispatch():
    from structural_dependencies import observations

    return observations("generator-dispatch-reads.json")
