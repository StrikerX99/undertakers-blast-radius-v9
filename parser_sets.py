"""Constrain verified removals of read-only token sets to the owning parser users."""

import ast
from functools import lru_cache
import json
from pathlib import Path

import module_footprints


@lru_cache(maxsize=1)
def index():
    return json.loads(Path(__file__).with_name("parser-sets.json").read_bytes())


def ordinary_value(node, known, earlier):
    if isinstance(node, ast.Name):
        return node.id in earlier
    if isinstance(node, ast.Attribute):
        base = node.value
        return (
            node.attr in known
            and isinstance(base, ast.Attribute)
            and base.attr == "Parser"
            and isinstance(base.value, ast.Name)
            and base.value.id == "parser"
        )
    if isinstance(node, ast.Set):
        return all(
            isinstance(item, ast.Attribute)
            and isinstance(item.value, ast.Name)
            and item.value.id == "TokenType"
            and item.attr.isupper()
            for item in node.elts
        )
    return (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.BitOr, ast.BitAnd, ast.Sub, ast.BitXor))
        and ordinary_value(node.left, known, earlier)
        and ordinary_value(node.right, known, earlier)
    )


def removed(selector, path, old, new, equal):
    if not path.startswith("sqlglot/parsers/") or len(old.body) != len(new.body):
        return False
    manifest = index()
    leaf = module_footprints._catalog()["leaf"].get(path)
    if (
        not leaf
        or path not in manifest["rows"]
        or manifest["pin"] != selector.mapping["commit"]
        or manifest["sources"][path] != selector.mapping["source_hashes"].get(path)
    ):
        return False
    name, candidates = manifest["rows"][path]
    if name != leaf:
        return False
    known = set(candidates)
    changes = [(a, b) for a, b in zip(old.body, new.body) if not equal(a, b)]
    if len(changes) != 1:
        return False
    a, b = changes[0]
    if not (
        isinstance(a, ast.ClassDef)
        and isinstance(b, ast.ClassDef)
        and a.name == name
        and not a.keywords
        and not a.decorator_list
        and module_footprints._same_header(a, b, equal)
        and len(a.body) > len(b.body)
    ):
        return False
    # Direct imports were checked in the pristine module by identity. A
    # shadowed name, or an eager inspection of the class being assembled,
    # could make an apparently ordinary removed initializer observable.
    for stmt in a.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(stmt):
            if isinstance(n, ast.Name) and (
                n.id in {"parser", "TokenType"}
                and not isinstance(n.ctx, ast.Load)
                or n.id in {"locals", "vars", "eval", "exec", "globals"}
            ):
                return False
    i, removed_names, earlier = 0, set(), set()
    for stmt in a.body:
        if i < len(b.body) and equal(stmt, b.body[i]):
            i += 1
            continue
        if not (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id in known
            and stmt.targets[0].id not in removed_names
            and ordinary_value(stmt.value, known, earlier)
        ):
            return False
        removed_names.add(stmt.targets[0].id)
        earlier.add(stmt.targets[0].id)
    if i != len(b.body) or not removed_names:
        return False
    # A surviving bare name could resolve to a different class/global binding;
    # a repeated assignment could replace an intermediate value with new state.
    return not any(
        isinstance(n, ast.Name) and n.id in removed_names for n in ast.walk(b)
    )
