"""Restrict local dialect class edits using measured module inputs of real pytest groups."""

import ast
from functools import lru_cache
import json
from pathlib import Path

_PREFIXES = ("sqlglot/parsers/", "sqlglot/generators/")


@lru_cache(maxsize=1)
def _catalog():
    return json.loads(Path(__file__).with_name("module-footprints.json").read_bytes())


@lru_cache(maxsize=1)
def _unopened():
    return json.loads(Path(__file__).with_name("unopened-sources.json").read_bytes())


def _members(statements):
    result = {}
    for n in statements:
        if isinstance(n, ast.FunctionDef):
            key = ("method", n.name)
        elif (
            isinstance(n, ast.Assign)
            and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Name)
        ):
            key = ("setting", n.targets[0].id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            key = ("setting", n.target.id)
        else:
            key = ("other", ast.dump(n))
        # Reassignments can depend on intermediate class state. Do not match
        # one changed declaration to an earlier declaration with the same name.
        if key in result:
            return None
        result[key] = n
    return result


def _deferred_value(n):
    if isinstance(n, ast.Lambda):
        return not n.args.defaults and not any(n.args.kw_defaults) and _local_body(n)
    try:
        ast.literal_eval(n)
    except (ValueError, TypeError):
        return False
    return True


def _class_value(old, new):
    if _deferred_value(old) and _deferred_value(new):
        return True
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return False

    # Existing spreads, factory calls and other eager evaluations must keep
    # their order. Only constants and deferred values can appear/disappear.
    def eager(d):
        return [
            (ast.dump(k) if k else None, ast.dump(v))
            for k, v in zip(d.keys, d.values)
            if not (isinstance(k, ast.Constant) and _deferred_value(v))
        ]

    return eager(old) == eager(new)


_LOCAL_BUILTINS = set(
    "abs all any bool dict enumerate filter float int isinstance issubclass len list map "
    "max min next range reversed set sorted str sum super tuple zip".split()
)


def _root_name(expr):
    while isinstance(expr, (ast.Attribute, ast.Subscript, ast.Call)):
        expr = expr.func if isinstance(expr, ast.Call) else expr.value
    return expr.id if isinstance(expr, ast.Name) else None


def _local_body(n):
    # Baseline groups run in separate processes. Keep changes that explicitly
    # mutate shared state, perform IO, import code or call across an unknown
    # module boundary on the original scheduler path.
    if isinstance(n, ast.FunctionDef) and (
        n.decorator_list or not n.args.args or n.args.args[0].arg != "self"
    ):
        return False
    nodes = list(ast.walk(n))
    locals_ = {v.arg for v in nodes if isinstance(v, ast.arg)}
    locals_.update(
        v.id for v in nodes if isinstance(v, ast.Name) and isinstance(v.ctx, ast.Store)
    )
    for v in nodes:
        if isinstance(
            v,
            (
                ast.Global,
                ast.Nonlocal,
                ast.Import,
                ast.ImportFrom,
                ast.AsyncFunctionDef,
            ),
        ):
            return False
        if isinstance(v, ast.Attribute) and v.attr in {
            "__class__",
            "__dict__",
            "__globals__",
            "__subclasses__",
            "__bases__",
            "__mro__",
        }:
            return False
        if isinstance(v, ast.Call):
            root = _root_name(v.func)
            if root not in locals_ and root not in _LOCAL_BUILTINS:
                # Expression subclasses create and populate an expression tree.
                callee = v.func
                while isinstance(callee, (ast.Attribute, ast.Call, ast.Subscript)):
                    if isinstance(callee, ast.Attribute) and isinstance(
                        callee.value, ast.Name
                    ):
                        break
                    callee = (
                        callee.func if isinstance(callee, ast.Call) else callee.value
                    )
                if not (
                    root == "exp"
                    and isinstance(callee, ast.Attribute)
                    and callee.attr[0].isupper()
                ):
                    return False
        if isinstance(v, (ast.Attribute, ast.Subscript)) and isinstance(
            v.ctx, (ast.Store, ast.Del)
        ):
            if _root_name(v) not in locals_:
                return False
    return True


def _plain_private(n):
    return (
        isinstance(n, ast.FunctionDef)
        and n.name.startswith("_")
        and not n.name.startswith("__")
        and not n.decorator_list
        and not n.args.defaults
        and not any(n.args.kw_defaults)
        and not n.type_params
        and _local_body(n)
    )


def _same_header(old, new, equal):
    return all(
        equal(getattr(old, f), getattr(new, f)) for f in old._fields if f != "body"
    )


def _local_class(old, new, equal, tables=True):
    if not _same_header(old, new, equal):
        return False
    a, b = _members(old.body), _members(new.body)
    if a is None or b is None:
        return False
    shared = a.keys() & b.keys()
    if [k for k in a if k in shared] != [k for k in b if k in shared]:
        return False
    for key in a.keys() | b.keys():
        before, after = a.get(key), b.get(key)
        if equal(before, after):
            continue
        if key[0] == "method":
            if before is None or after is None:
                if not _plain_private(after or before):
                    return False
            elif not (
                _same_header(before, after, equal)
                and _local_body(before)
                and _local_body(after)
            ):
                return False
        elif (
            tables and key[0] == "setting" and before is not None and after is not None
        ):
            if type(before) is not type(after) or not key[1].isupper():
                return False
            if not all(
                equal(getattr(before, f), getattr(after, f))
                for f in before._fields
                if f != "value"
            ):
                return False
            if not _class_value(before.value, after.value):
                return False
        else:
            return False
    return True


def local_change(tree, updated, equal, leaf=None, tables=True):
    if not any(
        isinstance(s, ast.ImportFrom)
        and s.module == "__future__"
        and any(n.name == "annotations" for n in s.names)
        for s in tree.body
    ):
        return False
    if len(tree.body) != len(updated.body):
        return False
    changed = False
    for old, new in zip(tree.body, updated.body):
        if equal(old, new):
            continue
        if not (
            isinstance(old, ast.ClassDef)
            and isinstance(new, ast.ClassDef)
            and (leaf is None or old.name == leaf)
            and _local_class(old, new, equal, tables)
        ):
            return False
        changed = True
    return changed


def refine(selector, changes, selected, apply, equal):
    if not selected or not changes or selector.unknown:
        return selected
    paths = [c["old"] for c in changes]
    if any(
        c["old"] != c["new"]
        or not p.startswith(_PREFIXES)
        or not p.endswith(".py")
        or p.endswith(("/__init__.py", "/dialect.py"))
        for p, c in zip(paths, changes)
    ):
        return selected
    if len(set(paths)) != len(paths):
        return selected
    if len(selected) <= 4 and all(
        selector.by_number[n]["seconds"] < 1 for n in selected
    ):
        return selected
    data = _catalog()
    raw = _unopened()
    if data["commit"] != selector.mapping["commit"] or raw["commit"] != data["commit"]:
        return selected
    users = set()
    for path, change in zip(paths, changes):
        if (
            not data["hashes"].get(path)
            or data["hashes"][path] != selector.mapping["source_hashes"].get(path)
            or raw["unopened"].get(path) != data["hashes"][path]
        ):
            return selected
        original, tree = selector.source(path)  # checks source hash
        updated, _, _ = apply(original, change)
        if path not in data["leaf"] or not local_change(
            tree,
            ast.parse(updated),
            equal,
            data["leaf"][path],
            path.startswith("sqlglot/parsers/"),
        ):
            return selected
        # Also retain every positive dependency in V7's original full-suite
        # method/explicit source-read trace, including warm-suite-only use.
        users.update(selector.file_tests(path))
        for i in data["readers"][path]:
            users.update(
                selector.by_name[k] for k in data["groups"][i] if k in selector.by_name
            )
    # Whole-registry enumeration in an already-warm suite can observe a
    # different process history than the native group did. Keep those callers.
    for path, lines in data["registry_read_lines"].items():
        selector.load_shard(path)
        users.update(selector.line_tests(path, lines))
    observed_files = set(data["registry_test_files"])
    users.update(t["number"] for t in selector.tests if t["file"] in observed_files)
    return selected & users
