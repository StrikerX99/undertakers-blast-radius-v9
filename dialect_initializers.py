"""Admit passive dialect transform-table closures and alias changes at the pinned revision."""

import ast
import copy
from functools import lru_cache
import json
import module_footprints
from pathlib import Path


@lru_cache(maxsize=1)
def _data():
    return json.loads(
        Path(__file__).with_name("initializer-bindings.json").read_bytes()
    )


def _literal(n):
    if isinstance(n, ast.Constant):
        return type(n.value) in (bool, int, float, str, bytes, type(None))
    if isinstance(n, (ast.List, ast.Tuple)):
        return all(_literal(v) for v in n.elts)
    return False


def _scope(tree, cls, current, data):
    funcs = {}
    mods = {}
    exprs = set()
    for n in tree.body[: tree.body.index(cls)]:
        if (
            isinstance(n, ast.FunctionDef)
            and n.name in data["modules"][current]["functions"]
        ):
            funcs[n.name] = (current, n.name)
        elif isinstance(n, ast.ImportFrom) and n.level == 0:
            for a in n.names:
                dest = a.asname or a.name
                if n.module == "sqlglot" and a.name == "exp":
                    exprs.add(dest)
                elif n.module == "sqlglot" and ("sqlglot." + a.name) in data["modules"]:
                    mods[dest] = "sqlglot." + a.name
                elif (
                    n.module in data["modules"]
                    and a.name in data["modules"][n.module]["functions"]
                ):
                    funcs[dest] = (n.module, a.name)
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.asname and a.name in data["modules"]:
                    mods[a.asname] = a.name
    # Module/class assignments and conditional imports can shadow these names.
    # Prune any ambiguous binding; take only direct preceding imports and
    # definitions, never assume a symbol hidden behind an if/import fallback.
    candidates = set(funcs) | set(mods) | exprs
    prior = tree.body[: tree.body.index(cls)]
    count = {k: 0 for k in candidates}
    for n in prior:
        if isinstance(n, ast.ClassDef):
            items = [n]
        elif isinstance(n, ast.FunctionDef):
            items = [n]
        else:
            items = list(ast.walk(n))
        for v in items:
            if isinstance(v, (ast.ClassDef, ast.FunctionDef)):
                name = v.name
            elif isinstance(v, ast.Name) and isinstance(v.ctx, (ast.Store, ast.Del)):
                name = v.id
            elif isinstance(v, ast.alias):
                name = v.asname or v.name.split(".")[0]
            else:
                continue
            if name in count:
                count[name] += 1
    shadows = set()
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
            shadows.add(n.name)
        elif isinstance(n, ast.Assign):
            shadows.update(
                x.id
                for t in n.targets
                for x in ast.walk(t)
                if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)
            )
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            shadows.add(n.target.id)
    banned = {k for k, v in count.items() if v != 1} | shadows
    return (
        {k: v for k, v in funcs.items() if k not in banned},
        {k: v for k, v in mods.items() if k not in banned},
        exprs - banned,
    )


def _callable(n, scope, data):
    funcs, mods, _ = scope
    if isinstance(n, ast.Name):
        return funcs.get(n.id)
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
        module = mods.get(n.value.id)
        if module and n.attr in data["modules"][module]["functions"]:
            return module, n.attr
    return None


def _expression(n, scope, data):
    return (
        isinstance(n, ast.Attribute)
        and isinstance(n.value, ast.Name)
        and n.value.id in scope[2]
        and n.attr in data["expressions"]
    )


def _passive(n, scope, data):
    if _literal(n) or _callable(n, scope, data) or _expression(n, scope, data):
        return True
    if isinstance(n, (ast.Tuple, ast.List)):
        return all(_passive(v, scope, data) for v in n.elts)
    if isinstance(n, ast.Lambda):
        if not module_footprints._local_body(n) or any(
            isinstance(v, ast.Attribute)
            and (
                v.attr.startswith("__")
                or v.attr.isupper()
                or v.attr.startswith("_DISPATCH")
            )
            for v in ast.walk(n.body)
        ):
            return False
        return all(
            x is None or _literal(x) for x in [*n.args.defaults, *n.args.kw_defaults]
        )
    if isinstance(n, ast.Call):
        target = _callable(n.func, scope, data)
        if not target or target[1] not in data["modules"][target[0]]["factories"]:
            return False
        return all(_passive(x, scope, data) for x in n.args) and all(
            k.arg is not None and _passive(k.value, scope, data) for k in n.keywords
        )
    return False


def _safe_table(before, after, scopes, data):
    if not isinstance(before, ast.Dict) or not isinstance(after, ast.Dict):
        return False

    def sequence(d, scope):
        items = []
        for k, v in zip(d.keys, d.values):
            # A spread must remain bytecode-equivalent and in the same
            # position relative to other nontrivial expressions.
            if (
                k is not None
                and (_expression(k, scope, data) or _literal(k))
                and _passive(v, scope, data)
            ):
                continue
            items.append((ast.dump(k) if k else None, ast.dump(v)))
        return items

    return sequence(before, scopes[0]) == sequence(after, scopes[1])


def _field(n):
    if not isinstance(n, (ast.Assign, ast.AnnAssign)):
        return None
    targets = n.targets if isinstance(n, ast.Assign) else [n.target]
    if len(targets) != 1 or not isinstance(targets[0], ast.Name):
        return None
    return targets[0].id


def validated(selector, path, old, new, equal):
    data = _data()
    current = path[:-3].replace("/", ".")
    if (
        not path.startswith(("sqlglot/generators/", "sqlglot/parsers/"))
        or current not in data["modules"]
        or data["pin"] != selector.mapping["commit"]
        or data["modules"][current]["sha256"]
        != selector.mapping["source_hashes"].get(path)
        or len(old.body) != len(new.body)
    ):
        return None
    before = copy.copy(old)
    before.body = old.body.copy()
    after = copy.copy(new)
    after.body = new.body.copy()
    fields = {
        "TRANSFORMS",
        "FUNCTIONS",
        "FUNCTION_PARSERS",
        "PROPERTY_PARSERS",
        "STATEMENT_PARSERS",
        "NO_PAREN_FUNCTION_PARSERS",
        "NO_PAREN_FUNCTIONS",
        "CONFLUENT_PARSERS",
    }
    checked_tables = set()
    patch_aliases = set()
    for i, (a, b) in enumerate(zip(old.body, new.body)):
        if equal(a, b):
            continue
        if isinstance(a, ast.ImportFrom) or isinstance(b, ast.ImportFrom):
            if not (
                isinstance(a, ast.ImportFrom)
                and isinstance(b, ast.ImportFrom)
                and a.module == b.module
                and a.module in data["modules"]
                and a.level == b.level == 0
                and a.names
                and b.names
            ):
                return None

            def aliases(node):
                return {(v.name, v.asname) for v in node.names}

            delta = aliases(a) ^ aliases(b)
            if not delta or any(
                k not in data["modules"][a.module]["functions"] for k, _ in delta
            ):
                return None
            # Loading an already-present module can still call its __getattr__
            # for missing names. The pinned catalog records actual globals.
            for node in (a, b):
                for alias in node.names:
                    dest = alias.asname or alias.name
                    if (
                        dest.startswith("__")
                        or dest
                        in {
                            "exp",
                            "generator",
                            "parser",
                            "transforms",
                            "typing",
                            "builtins",
                        }
                        and (alias.name, alias.asname) in delta
                    ):
                        return None
                bindings = [x.asname or x.name for x in node.names]
                if len(bindings) != len(set(bindings)):
                    return None
            patch_aliases.update(alias or name for name, alias in delta)
            after.body[i] = a
        elif (
            isinstance(a, ast.ClassDef)
            and isinstance(b, ast.ClassDef)
            and a.name == b.name
        ):
            scopes = (_scope(old, a, current, data), _scope(new, b, current, data))
            # Some dialects derive their final table from an earlier binding of
            # the same name. Compare matching occurrences, preserving order.
            old_fields = [(_field(n), n) for n in a.body if _field(n) in fields]
            new_fields = [_field(n) for n in b.body if _field(n) in fields]
            if [key for key, _ in old_fields] != new_fields:
                return None
            tables = iter(old_fields)
            cleaned = b.body.copy()
            for j, n in enumerate(b.body):
                key = _field(n)
                if key not in fields:
                    continue
                _, prev = next(tables)
                if equal(prev, n):
                    continue
                if (
                    type(prev) is type(n)
                    and all(
                        equal(getattr(prev, f), getattr(n, f))
                        for f in n._fields
                        if f != "value"
                    )
                    and _safe_table(prev.value, n.value, scopes, data)
                ):
                    checked_tables.add(prev)
                    checked_tables.add(n)
                    cleaned[j] = prev
            if any(x is not y for x, y in zip(cleaned, b.body)):
                cls = copy.copy(b)
                cls.body = cleaned
                after.body[i] = cls
    if patch_aliases:
        # If the patched tree still reads a removed binding anywhere beyond a
        # validated table entry, a later method could fail indirectly.
        def count_names(tree):
            return {
                n.id
                for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            } & patch_aliases

        # Normalize certified tables out of both trees for this check: any
        # other use of a changed alias keeps the entire patch conservative.
        for tree in (old, new):
            for n in tree.body:
                if isinstance(n, ast.ClassDef):
                    for item in n.body:
                        if item in checked_tables:
                            continue
                        if count_names(item):
                            return None
                elif count_names(n):
                    return None
    return before, after


validate = validated


def eligible(selector, path, old_tree, new_tree, equal):
    pair = validated(selector, path, old_tree, new_tree, equal)
    return pair is not None and not equal(old_tree, new_tree) and equal(*pair)
