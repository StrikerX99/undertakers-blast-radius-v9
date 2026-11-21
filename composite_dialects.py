"""Bound local dialect edits by complete cold-group inputs and warm-suite consumers."""

import ast
from functools import lru_cache
import json
from pathlib import Path

import module_footprints as modules


def _exp_name(expr):
    return (
        isinstance(expr, ast.Attribute)
        and isinstance(expr.value, ast.Name)
        and expr.value.id == "exp"
        and expr.attr[:1].isupper()
    )


def _enum_name(expr):
    return (
        isinstance(expr, ast.Attribute)
        and _exp_name(expr.value)
        and expr.value.attr == "DType"
    )


def _pure_method(node, helpers):
    # SQLGlot's seq_get is a direct, bounds-checked Sequence indexing helper.
    # The simple-lookup form below is semantically narrower than allowing an
    # arbitrary imported helper or cross-module callable inside a method.
    class PureHelpers(ast.NodeTransformer):
        def visit_Call(self, call):
            self.generic_visit(call)
            if (
                isinstance(call.func, ast.Name)
                and call.func.id in helpers
                and len(call.args) == 2
                and not call.keywords
                and isinstance(call.args[0], ast.Name)
                and isinstance(call.args[1], ast.Constant)
                and type(call.args[1].value) is int
            ):
                return ast.copy_location(
                    ast.Subscript(
                        value=call.args[0], slice=call.args[1], ctx=ast.Load()
                    ),
                    call,
                )
            return call

    if not helpers or not any(
        isinstance(x, ast.Name) and x.id in helpers for x in ast.walk(node)
    ):
        return modules._local_body(node)
    import copy

    return modules._local_body(PureHelpers().visit(copy.deepcopy(node)))


def _method(old, new, helpers, equal):
    if old is not None and new is not None:
        return (
            modules._same_header(old, new, equal)
            and _pure_method(old, helpers)
            and _pure_method(new, helpers)
        )
    node = old or new
    return (
        isinstance(node, ast.FunctionDef)
        and node.name.startswith("_")
        and not node.name.startswith("__")
        and not node.decorator_list
        and not node.type_params
        and all(
            x is None or isinstance(x, ast.Constant)
            for x in [*node.args.defaults, *node.args.kw_defaults]
        )
        and _pure_method(node, helpers)
    )


def _deferred_lambda(node):
    return (
        isinstance(node, ast.Lambda)
        and not node.args.defaults
        and not any(node.args.kw_defaults)
        and modules._local_body(node)
    )


def _table(old, new, known):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return False

    def entries(mapping):
        eager = []
        for k, v in zip(mapping.keys, mapping.values):
            if _exp_name(k) and k.attr in known and _deferred_lambda(v):
                continue
            if isinstance(k, ast.Constant) and _deferred_lambda(v):
                continue
            eager.append((ast.dump(k) if k else None, ast.dump(v)))
        return eager

    return entries(old) == entries(new)


def _class(old, new, suffix, helpers, known, equal):
    if not (
        isinstance(old, ast.ClassDef)
        and isinstance(new, ast.ClassDef)
        and old.name.endswith(suffix)
        and not old.decorator_list
        and not old.keywords
        and all(
            equal(getattr(old, f), getattr(new, f)) for f in old._fields if f != "body"
        )
    ):
        return False

    def key(node):
        if isinstance(node, ast.FunctionDef):
            return ("method", node.name)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            return ("data", node.targets[0].id)
        return ("other", ast.dump(node))

    left = {key(n): n for n in old.body}
    right = {key(n): n for n in new.body}
    # Exclude reassignment and class-scope evaluation that depends on a
    # different declaration order.
    if len(left) != len(old.body) or len(right) != len(new.body):
        return False
    if [k for k in left if k in right] != [k for k in right if k in left]:
        return False
    for k in left.keys() | right.keys():
        a, b = left.get(k), right.get(k)
        if equal(a, b):
            continue
        if k[0] == "method":
            if not _method(a, b, helpers, equal):
                return False
        elif k == ("data", "TRANSFORMS") and a is not None and b is not None:
            if not _table(a.value, b.value, known):
                return False
        else:
            return False
    return True


def _metadata(old, new, equal, known, types):
    def binding(n):
        return (
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
            and n.targets[0].id == "EXPRESSION_METADATA"
            and isinstance(n.value, ast.Dict)
        )

    if not binding(old) or not binding(new):
        return False

    def parts(node):
        items = []
        for k, v in zip(node.value.keys, node.value.values):
            if k is None and isinstance(v, ast.DictComp) and len(v.generators) == 1:
                gen = v.generators[0]
                if (
                    not gen.ifs
                    and not gen.is_async
                    and isinstance(gen.iter, (ast.Set, ast.Tuple, ast.List))
                    and isinstance(gen.target, ast.Name)
                    and isinstance(v.key, ast.Name)
                    and v.key.id == gen.target.id
                    and all(_exp_name(x) and x.attr in known for x in gen.iter.elts)
                    and isinstance(v.value, ast.Dict)
                    and len(v.value.keys) == 1
                    and isinstance(v.value.keys[0], ast.Constant)
                    and v.value.keys[0].value == "returns"
                    and _enum_name(v.value.values[0])
                    and v.value.values[0].attr in types
                ):
                    items.append(("class-returns", v.key.id))
                    continue
            items.append((ast.dump(k) if k else None, ast.dump(v)))
        return items

    return parts(old) == parts(new)


@lru_cache(maxsize=1)
def _names():
    return json.loads(Path(__file__).with_name("dialect-value-names.json").read_bytes())


def supported(tree, updated, path, equal, names):
    if not any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(a.name == "annotations" for a in n.names)
        for n in tree.body
    ):
        return False
    if len(tree.body) != len(updated.body):
        return False
    helpers = {
        a.asname or a.name
        for n in tree.body
        if isinstance(n, ast.ImportFrom)
        and n.level == 0
        and n.module == "sqlglot.helper"
        for a in n.names
        if a.name == "seq_get"
    }
    # Another binding could shadow the helper. Resolving the name from the
    # unmodified, pinned module is safe only in the absence of reassignment.
    helpers -= {
        n.id
        for n in ast.walk(tree)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    }
    changed = False
    for a, b in zip(tree.body, updated.body):
        if equal(a, b):
            continue
        if "/typing/" in path:
            if not _metadata(a, b, equal, names["classes"], names["dtypes"]):
                return False
        else:
            suffix = "Parser" if "/parsers/" in path else "Generator"
            if not _class(a, b, suffix, helpers, names["classes"], equal):
                return False
        changed = True
    return changed


def refine(selector, changes, selected, apply, equal):
    if selector.unknown or len(changes) < 2 or len(selected) < 100:
        return selected
    paths = [c["old"] for c in changes]
    if (
        len(set(paths)) != len(paths)
        or any(c["old"] != c["new"] for c in changes)
        or any(
            len(p.split("/")) != 3
            or not p.startswith(
                ("sqlglot/parsers/", "sqlglot/generators/", "sqlglot/typing/")
            )
            or not p.endswith(".py")
            or p.endswith("/__init__.py")
            for p in paths
        )
        or len({p.rsplit("/", 1)[1] for p in paths}) != 1
    ):
        return selected
    d = modules._catalog()
    names = _names()
    if d["commit"] != selector.mapping["commit"] or names["pin"] != d["commit"]:
        return selected
    users = set()
    for c, p in zip(changes, paths):
        if not d["hashes"].get(p) or d["hashes"][p] != selector.mapping[
            "source_hashes"
        ].get(p):
            return selected
        if (
            p not in selector.files
            and modules._unopened()["unopened"].get(p) != d["hashes"][p]
        ):
            return selected
        text, tree = selector.source(p)
        new, _, _ = apply(text, c)
        if not supported(tree, ast.parse(new), p, equal, names):
            return selected
        users.update(selector.file_tests(p))
        if p in selector.files:
            users.update(selector.sets[selector.files[p]])
        for i in d["readers"][p]:
            users.update(
                selector.by_name[id_]
                for id_ in d["groups"][i]
                if id_ in selector.by_name
            )
        disp = selector.dispatch_map()
        users.update(disp.external)
        users.update(disp.external_by_source.get(p, ()))
    for p, lines in d["registry_read_lines"].items():
        selector.load_shard(p)
        users.update(selector.line_tests(p, lines))
    observed_files = set(d["registry_test_files"])
    users.update(t["number"] for t in selector.tests if t["file"] in observed_files)
    return selected & users
