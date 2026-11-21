"""Propose module-input test groups for validated local dialect implementation edits."""

import ast
from functools import lru_cache
import json
from pathlib import Path
import dialect_initializers
import inherited_methods
import module_footprints
import parser_sets


@lru_cache(maxsize=1)
def _manifest():
    return json.loads(
        Path(__file__).with_name("ordinary-dialect-classes.json").read_bytes()
    )


def _root(n):
    while isinstance(n, (ast.Attribute, ast.Subscript, ast.Call)):
        n = n.func if isinstance(n, ast.Call) else n.value
    return n.id if isinstance(n, ast.Name) else None


def _ordinary(n):
    if (
        not isinstance(n, ast.FunctionDef)
        or n.name.startswith("__")
        or n.decorator_list
        or n.type_params
    ):
        return False
    if not n.args.args or n.args.args[0].arg != "self":
        return False
    for default in n.args.defaults + [d for d in n.args.kw_defaults if d is not None]:
        if not isinstance(default, ast.Constant) or type(default.value) not in (
            bool,
            int,
            float,
            str,
            bytes,
            type(None),
        ):
            return False
    # Keep operations on the current expression/parser local. Disallow direct
    # mutation of a global, registered class, environment, file, or process.
    nodes = [s for stmt in n.body for s in ast.walk(stmt)]
    local = {a.arg for a in ast.walk(n.args) if isinstance(a, ast.arg)}
    local.update(
        s.id for s in nodes if isinstance(s, ast.Name) and isinstance(s.ctx, ast.Store)
    )
    safe_calls = set(
        "abs all any bool bytes dict enumerate filter float frozenset int isinstance issubclass iter len list map max min next range repr reversed round set sorted str sum super tuple type zip".split()
    )
    readonly = {
        "re": {
            "escape",
            "fullmatch",
            "match",
            "search",
            "sub",
            "subn",
            "findall",
            "split",
            "compile",
        }
    }
    changes = {
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
        "__setitem__",
        "__delitem__",
    }
    for v in nodes:
        if isinstance(
            v,
            (
                ast.Global,
                ast.Nonlocal,
                ast.Import,
                ast.ImportFrom,
                ast.With,
                ast.AsyncWith,
                ast.AsyncFunctionDef,
                ast.ClassDef,
            ),
        ):
            return False
        if isinstance(v, ast.Attribute) and (
            v.attr.startswith("__")
            or v.attr
            in {
                "__globals__",
                "__class__",
                "__dict__",
                "__mro__",
                "__subclasses__",
                "_classes",
                "_DISPATCH_CACHE",
                "__getattribute__",
            }
        ):
            return False
        if isinstance(v, (ast.Assign, ast.AnnAssign, ast.NamedExpr)) and isinstance(
            v.value, (ast.Name, ast.Attribute, ast.Subscript)
        ):
            if _root(v.value) not in local or any(
                isinstance(k, ast.Attribute) and k.attr.isupper()
                for k in ast.walk(v.value)
            ):
                return False
        if isinstance(v, (ast.Attribute, ast.Subscript)) and isinstance(
            v.ctx, (ast.Store, ast.Del)
        ):
            if _root(v) not in local or any(
                isinstance(a, ast.Attribute) and a.attr.isupper() for a in ast.walk(v)
            ):
                return False
        if isinstance(v, ast.Call):
            obj = _root(v.func)
            if obj in local:
                if (
                    isinstance(v.func, ast.Attribute)
                    and v.func.attr in changes
                    and any(
                        isinstance(a, ast.Attribute) and a.attr.isupper()
                        for a in ast.walk(v.func)
                    )
                ):
                    return False
                continue
            if obj in safe_calls and isinstance(v.func, ast.Name):
                continue
            if (
                obj == "exp"
                and isinstance(v.func, ast.Attribute)
                and v.func.attr not in changes
            ):
                head = v.func
                while isinstance(head, ast.Attribute) and not isinstance(
                    head.value, ast.Name
                ):
                    head = head.value
                if (
                    isinstance(head, ast.Attribute)
                    and isinstance(head.value, ast.Name)
                    and head.attr[:1].isupper()
                ):
                    continue
            if (
                obj in readonly
                and isinstance(v.func, ast.Attribute)
                and v.func.attr in readonly[obj]
            ):
                continue
            # Generator/parser helper methods may deliberately use other global
            # implementations with unknown side effects. Leave those unchanged.
            return False
    return True


def _pair_members(before, after, equal):
    # Index methods by name, with every non-method retaining its original order
    # and full normalized AST. This allows repeated unrelated table assignments.
    def split(cls):
        methods = {}
        data = []
        for n in cls.body:
            if isinstance(n, ast.FunctionDef):
                if n.name in methods:
                    return None
                methods[n.name] = n
            else:
                data.append(n)
        return methods, data

    a, b = split(before), split(after)
    if a is None or b is None or not equal(a[1], b[1]):
        return None
    old, new = a[0], b[0]
    for name in old.keys() | new.keys():
        f, g = old.get(name), new.get(name)
        if equal(f, g):
            continue
        if (f is not None and not _ordinary(f)) or (g is not None and not _ordinary(g)):
            return None

    # An assignment evaluated in the class body can read a bound method. Keep
    # interleaving (relative to data statements, and to other shared methods);
    # direct class-body cross-references to changed names are disallowed.
    def order(n):
        return [
            ("method", s.name)
            if isinstance(s, ast.FunctionDef)
            else ("data", ast.dump(s))
            for s in n.body
        ]

    overlap = set(old) & set(new)

    def only_shared(parts):
        return [pair for pair in parts if pair[0] != "method" or pair[1] in overlap]

    if only_shared(order(before)) != only_shared(order(after)):
        return None
    delta = {k for k in old.keys() | new.keys() if not equal(old.get(k), new.get(k))}
    if any(
        isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in delta
        for a in [*a[1], *b[1]]
        for x in ast.walk(a)
    ):
        return None
    return True


def _deferred_calls(selector, path, old, new, equal, manifest):
    # A replacement with identical dictionary keys preserves membership and
    # iteration. Unchanged eager expressions must remain in the same order.
    # The dispatch recorder registered these actual callables before the passing
    # full suite and omits any callable executed outside test attribution.
    if len(old.body) != len(new.body):
        return None
    if path in selector.files:
        return None
    affected = set()
    edits = 0
    for a, b in zip(old.body, new.body):
        if equal(a, b):
            continue
        if not (
            isinstance(a, ast.ClassDef)
            and isinstance(b, ast.ClassDef)
            and a.name == b.name
            and module_footprints._same_header(a, b, equal)
        ):
            return None
        deleted = manifest["private_internal"].get(path, {}).get(a.name, [])
        j = 0
        removed_uses = set()
        replaced_spans = []
        for left in a.body:
            right = b.body[j] if j < len(b.body) else None
            if equal(left, right):
                j += 1
                continue
            if (
                isinstance(left, ast.FunctionDef)
                and left.name in deleted
                and _ordinary(left)
            ):
                if (
                    left.decorator_list
                    or left.args.defaults
                    or any(left.args.kw_defaults)
                ):
                    return None
                lines = set(range(left.body[0].lineno, left.end_lineno + 1))
                if set(selector.mapping["import_lines"].get(path, [])) & lines:
                    return None
                removed_uses.update(tuple(pos) for pos in deleted[left.name])
                replaced_spans.append(
                    (
                        (left.lineno, left.col_offset),
                        (left.end_lineno, left.end_col_offset),
                    )
                )
                affected.update(selector.line_tests(path, lines))
                edits += 1
                continue
            if not (
                isinstance(left, (ast.Assign, ast.AnnAssign))
                and type(left) is type(right)
                and all(
                    equal(getattr(left, f), getattr(right, f))
                    for f in left._fields
                    if f != "value"
                )
                and isinstance(left.value, ast.Dict)
                and isinstance(right.value, ast.Dict)
            ):
                return None
            # No new call/closure defaults at definition time. Other deferred
            # forms remain on the module-input bound.
            if len(left.value.keys) != len(right.value.keys) or any(
                not equal(k, m) for k, m in zip(left.value.keys, right.value.keys)
            ):
                return None
            if any(
                not equal(v, w)
                and not (
                    isinstance(v, ast.Lambda)
                    and isinstance(w, ast.Lambda)
                    and equal(v.args, w.args)
                    and not v.args.defaults
                    and not any(v.args.kw_defaults)
                )
                for v, w in zip(left.value.values, right.value.values)
            ):
                return None
            for v, w in zip(left.value.values, right.value.values):
                if not equal(v, w):
                    replaced_spans.append(
                        ((v.lineno, v.col_offset), (v.end_lineno, v.end_col_offset))
                    )
            readers = selector.dispatch_map().changed(
                path, left.value, right.value, equal
            )
            if readers is None:
                return None
            affected.update(readers)
            edits += 1
            j += 1
        if j != len(b.body) or any(
            not any(start <= pos < end for start, end in replaced_spans)
            for pos in removed_uses
        ):
            return None
    return affected if edits else None


def refine(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    c = changes[0]
    path = c["old"]
    if path != c["new"] or not path.startswith(
        ("sqlglot/parsers/", "sqlglot/generators/")
    ):
        return selected
    base = module_footprints._catalog()
    projects = _manifest()
    digest = base["hashes"].get(path)
    if (
        path not in projects["classes"]
        or not digest
        or base["commit"] != selector.mapping["commit"]
        or projects["commit"] != base["commit"]
        or module_footprints._unopened()["unopened"].get(path) != digest
        or selector.mapping["source_hashes"].get(path) != digest
    ):
        return selected
    source, tree = selector.source(path)
    updated, *_ = apply(source, c)
    after = ast.parse(updated)
    if not any(
        isinstance(n, ast.ImportFrom)
        and n.module == "__future__"
        and any(v.name == "annotations" for v in n.names)
        for n in tree.body
    ):
        return selected
    norm = dialect_initializers.validate(selector, path, tree, after, equal)
    if norm is None:
        return selected
    a, b = norm
    if len(a.body) != len(b.body):
        return selected
    related = set()
    before_classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    new_classes = {n.name: n for n in after.body if isinstance(n, ast.ClassDef)}
    if set(before_classes) != set(new_classes):
        return selected
    for name in before_classes:
        if not equal(before_classes[name], new_classes[name]):
            if name not in projects["classes"][path]:
                return selected
            related.update(projects["classes"][path][name])
    if not related:
        return selected
    for old, new in zip(a.body, b.body):
        if equal(old, new):
            continue
        if not (
            isinstance(old, ast.ClassDef)
            and isinstance(new, ast.ClassDef)
            and not old.decorator_list
            and not old.keywords
            and module_footprints._same_header(old, new, equal)
            and (
                _pair_members(old, new, equal)
                or parser_sets.removed(selector, path, tree, after, equal)
            )
        ):
            return selected
    users = set()
    ext = selector.dispatch_map()
    for other in related:
        selector.load_shard(other)
        users.update(selector.file_tests(other))
        users.update(ext.external | ext.external_by_source.get(other, set()))
        for i in base["readers"][other]:
            users.update(
                selector.by_name[n] for n in base["groups"][i] if n in selector.by_name
            )
    invocation = _deferred_calls(selector, path, tree, after, equal, projects)
    if invocation is not None:
        users &= invocation | ext.external | ext.external_by_source.get(path, set())
    seen = set(base["registry_test_files"]) | set(
        inherited_methods.catalog()["reflective_test_files"]
    )
    users.update(t["number"] for t in selector.tests if t["file"] in seen)
    for other, lines in base["registry_read_lines"].items():
        selector.load_shard(other)
        users.update(selector.line_tests(other, lines))
    return selected & users
