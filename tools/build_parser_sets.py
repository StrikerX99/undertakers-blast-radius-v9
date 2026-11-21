"""Index inherited token-set declarations whose pinned callers treat the sets as read-only."""

import ast
from collections import defaultdict
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys


def build(root, out):
    root = Path(root).resolve()
    pin = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    if subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip() != pin or subprocess.check_output(
        ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"]
    ):
        raise ValueError("Unmodified pin required")
    sys.path.insert(0, str(root))
    from sqlglot.parser import Parser
    from sqlglot.tokens import TokenType

    def enumset(v):
        return type(v) in (set, frozenset) and all(type(x) is TokenType for x in v)

    fields = {k for k, v in vars(Parser).items() if k.isupper() and enumset(v)}
    parser_methods = {
        n
        for n in dir(Parser)
        if n.startswith("_") and not n.startswith("__") and callable(getattr(Parser, n))
    }
    parents = {}
    methods = defaultdict(list)
    sources = {}
    paths = {}
    uses = defaultdict(list)
    strings = set()
    owners = [Parser]
    headers = []
    from sqlglot import parser

    if type(Parser) is not type or "__init_subclass__" in vars(Parser):
        raise ValueError("Unexpected Parser class construction")

    for prefix in ("sqlglot", "tests"):
        for p in sorted((root / prefix).rglob("*.py")):
            path = p.relative_to(root).as_posix()
            tree = ast.parse(p.read_bytes())
            names = list(ast.walk(tree))
            headers.extend(
                (
                    n.name,
                    {
                        base.id if isinstance(base, ast.Name) else base.attr
                        for base in n.bases
                        if isinstance(base, (ast.Name, ast.Attribute))
                    },
                )
                for n in names
                if isinstance(n, ast.ClassDef)
            )
            strings.update(
                n.value
                for n in names
                if isinstance(n, ast.Constant)
                and type(n.value) is str
                and n.value in fields
            )
            if (
                path != "sqlglot/parser.py"
                and not path.startswith("sqlglot/parsers/")
                and not any(
                    isinstance(n, ast.Attribute) and n.attr in fields for n in names
                )
                and not any(
                    isinstance(n, ast.FunctionDef) and n.name in parser_methods
                    for n in names
                )
            ):
                continue
            parents.update((x, n) for n in names for x in ast.iter_child_nodes(n))
            for n in names:
                if isinstance(n, ast.Attribute) and n.attr in fields:
                    uses[("attr", n.attr)].append(n)
                if isinstance(n, ast.FunctionDef) and n.name in parser_methods:
                    methods[n.name].append(n)
                if isinstance(n, ast.FunctionDef):
                    for sub in ast.walk(n):
                        if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                            uses[(n, sub.id)].append(sub)
            for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
                for st in cls.body:
                    if (
                        isinstance(st, (ast.Assign, ast.AnnAssign))
                        and st.value is not None
                    ):
                        for n in ast.walk(st.value):
                            if isinstance(n, ast.Name) and n.id in fields:
                                uses[("attr", n.id)].append(n)
            if path.startswith("sqlglot/parsers/") and p.stem != "__init__":
                mod = importlib.import_module(path[:-3].replace("/", "."))
                for cls in vars(mod).values():
                    if not (
                        isinstance(cls, type)
                        and cls.__module__ == mod.__name__
                        and issubclass(cls, Parser)
                    ):
                        continue
                    owners.append(cls)
                    if not (
                        type(cls) is type
                        and cls.__bases__ == (Parser,)
                        and "__init_subclass__" not in cls.__dict__
                        and getattr(mod, "parser", None) is parser
                        and getattr(mod, "TokenType", None) is TokenType
                    ):
                        continue
                    available = {
                        k
                        for k in cls.__dict__
                        if k in fields and enumset(cls.__dict__[k])
                    }
                    if available:
                        paths[path] = (cls, available)
                sources[path] = hashlib.sha256(p.read_bytes()).hexdigest()

    family = {"Parser"}
    while True:
        added = {name for name, bases in headers if bases & family} - family
        if not added:
            break
        family.update(added)
    for name, implementations in methods.items():
        methods[name] = [
            n
            for n in implementations
            if isinstance(parents.get(n), ast.ClassDef) and parents[n].name in family
        ]

    def owner(n):
        while n in parents:
            n = parents[n]
            if isinstance(n, (ast.FunctionDef, ast.ClassDef)):
                return n

    def consumption(n):
        p = parents.get(n)
        if (
            isinstance(p, ast.Compare)
            or isinstance(p, ast.UnaryOp)
            and isinstance(p.op, ast.Not)
        ):
            return set()
        if isinstance(p, (ast.If, ast.While)) and p.test is n:
            return set()
        if isinstance(p, ast.BinOp) and isinstance(
            p.op, (ast.BitOr, ast.BitAnd, ast.BitXor, ast.Sub)
        ):
            return set()
        if isinstance(p, ast.Starred) and isinstance(
            parents.get(p), (ast.Set, ast.List, ast.Tuple)
        ):
            return set()
        if isinstance(p, (ast.BoolOp, ast.IfExp)):
            return (
                consumption(p)
                if not (isinstance(p, ast.IfExp) and p.test is n)
                else set()
            )
        if isinstance(p, (ast.Assign, ast.AnnAssign)) and p.value is n:
            targets = p.targets if isinstance(p, ast.Assign) else [p.target]
            frame = owner(p)
            if all(isinstance(t, ast.Name) for t in targets):
                if isinstance(frame, ast.FunctionDef):
                    return {(frame, t.id) for t in targets}
                if isinstance(frame, ast.ClassDef) and all(
                    t.id in fields for t in targets
                ):
                    return {("attr", t.id) for t in targets}
            return None
        kw = None
        if isinstance(p, ast.keyword):
            kw = p.arg
            if kw is None:
                return None
            n, p = p, parents[p]
        if isinstance(p, ast.Call):
            fn = p.func
            if (
                isinstance(fn, ast.Name)
                and fn.id in {"len", "set", "frozenset", "list", "tuple", "bool"}
                and len(p.args) == 1
                and not p.keywords
            ):
                return set()
            if not isinstance(fn, ast.Attribute) or not methods.get(fn.attr):
                return None
            recv = fn.value
            if not (
                isinstance(recv, ast.Name)
                and recv.id == "self"
                or isinstance(recv, ast.Call)
                and isinstance(recv.func, ast.Name)
                and recv.func.id == "super"
                and not recv.args
            ):
                return None
            if kw is None and n not in p.args:
                return None
            deps = set()
            for m in methods[fn.attr]:
                args = m.args.posonlyargs + m.args.args
                if m.decorator_list or not args or args[0].arg != "self":
                    return None
                if kw is None:
                    pos = p.args.index(n) + 1
                    if pos >= len(args) or any(
                        isinstance(a, ast.Starred) for a in p.args[:pos]
                    ):
                        return None
                    var = args[pos].arg
                elif kw in {a.arg for a in args + m.args.kwonlyargs}:
                    var = kw
                else:
                    return None
                deps.add((m, var))
            return deps
        return None

    def safe(k):
        todo = [k]
        visited = set()
        while todo:
            key = todo.pop()
            if key in visited:
                continue
            visited.add(key)
            if key[0] == "attr" and key[1] in strings:
                return False
            for n in uses[key]:
                if isinstance(n, ast.Attribute) and not isinstance(n.ctx, ast.Load):
                    return False
                dep = consumption(n)
                if dep is None:
                    return False
                todo.extend(dep)
        return True

    eligible = {k for k in fields if safe(("attr", k))}
    # Deletion can publish a mutable parent object formerly hidden by a child
    # copy. Check every other spelling that names either actual set object.
    aliases = defaultdict(set)
    for owner_cls in owners:
        for attr, value in vars(owner_cls).items():
            if type(value) in (set, frozenset):
                aliases[id(value)].add(attr)
    rows = {}
    for path, (cls, names) in paths.items():
        safe_names = {
            k
            for k in names & eligible
            if (aliases[id(vars(cls)[k])] | aliases[id(vars(Parser)[k])]) <= eligible
        }
        if safe_names:
            rows[path] = [cls.__name__, sorted(safe_names)]
    Path(out).write_text(
        json.dumps(
            dict(pin=pin, sources={p: sources[p] for p in rows}, rows=rows),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        "settings",
        len(eligible),
        "modules",
        len(rows),
        "overrides",
        sum(len(x[1]) for x in rows.values()),
        flush=True,
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
