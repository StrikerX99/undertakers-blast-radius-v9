"""Collect concrete consumers of primitive class attributes in unmodified SQLGlot."""

import ast
import builtins
import concurrent.futures
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
import pytest

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
root = Path(sys.argv[1]).resolve()
base = json.loads(Path(sys.argv[2]).read_bytes())
out = Path(sys.argv[3]).resolve()
PRIMITIVES = (int, str, bool, type(None), float)


def scalar(node):
    try:
        value = ast.literal_eval(node)
    except (TypeError, ValueError, SyntaxError):
        return False, None
    return type(value) in PRIMITIVES, value


declarations = {}
names = set()
counts = defaultdict(lambda: defaultdict(set))
lookup = {}
current = ""
import_depth = 0
external = set()
handled = set()


def read(obj, name, mode, guard=None, conjunction=True):
    value = getattr(obj, name)
    owner = obj if isinstance(obj, type) else type(obj)
    pair = (owner, name)
    if pair not in lookup:
        site = None
        for cls in owner.__mro__:
            if name in vars(cls):
                site = declarations.get((cls.__module__, cls.__qualname__, name))
                break
        lookup[pair] = site
    site = lookup[pair]
    if site:
        if guard is not None:
            try:
                builtin, checked, cls = guard()
                if builtin is isinstance and type(cls) is type:
                    # An ordinary isinstance on a built-in type metaclass is a
                    # side-effect-free guard for the SQLGlot expression domain.
                    actual = type(checked)
                    if (
                        type(actual) is type
                        and actual.__getattribute__ is object.__getattribute__
                    ):
                        matches = cls in actual.__mro__
                        if matches != conjunction:
                            mode = "masked"
            except (NameError, AttributeError):
                pass
        row, kind, expected = site
        if type(value) is not kind or value != expected:
            mode = "raw"
        counts[row][mode].add("" if import_depth else current)
    return value


class Transform(ast.NodeTransformer):
    def __init__(self, tree):
        self.parents = {c: p for p in ast.walk(tree) for c in ast.iter_child_nodes(p)}

    def mode(self, n):
        p = self.parents.get(n)
        while isinstance(p, ast.BoolOp):
            n, p = p, self.parents.get(p)
        if isinstance(p, (ast.If, ast.IfExp, ast.While, ast.Assert)) and n is p.test:
            return "bool"
        if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
            return "bool"
        if (
            isinstance(p, ast.Compare)
            and len(p.ops) == 1
            and isinstance(
                p.ops[0],
                (
                    ast.Eq,
                    ast.NotEq,
                    ast.Is,
                    ast.IsNot,
                    ast.Lt,
                    ast.Gt,
                    ast.LtE,
                    ast.GtE,
                ),
            )
        ):
            # A BoolOp can return either operand, not necessarily this attribute.
            if isinstance(n, ast.BoolOp):
                return "raw"
            left = n is p.left
            other = p.comparators[0] if left else p.left
            ok, val = scalar(other)
            if ok and (
                not isinstance(p.ops[0], (ast.Is, ast.IsNot))
                or type(val) in (bool, type(None))
            ):
                return json.dumps(
                    [type(p.ops[0]).__name__, left, val], separators=(",", ":")
                )
        return "raw"

    def guard(self, node, mode):
        if mode == "raw":
            return None
        part = node
        above = self.parents.get(part)
        if isinstance(above, ast.Compare):
            if not isinstance(above.ops[0], (ast.Is, ast.IsNot, ast.Eq, ast.NotEq)):
                return None
            part = above
        elif isinstance(above, ast.UnaryOp) and isinstance(above.op, ast.Not):
            part = above
        outer = self.parents.get(part)
        if not isinstance(outer, ast.BoolOp):
            return None
        # When the whole BoolOp is used only for control flow, an immediately
        # following false AND guard or true OR guard fixes the final truth
        # value, as long as it has no other visible effects.
        ancestor = self.parents.get(outer)
        if (
            not isinstance(ancestor, (ast.If, ast.IfExp, ast.While, ast.Assert))
            or ancestor.test is not outer
        ):
            return None
        following = outer.values[outer.values.index(part) + 1 :]
        if not following:
            return None
        call = following[0]
        if not (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "isinstance"
            and len(call.args) == 2
            and not call.keywords
            and isinstance(call.args[0], ast.Name)
            and isinstance(call.args[1], ast.Attribute)
            and isinstance(call.args[1].value, ast.Name)
            and call.args[1].value.id == "exp"
        ):
            return None
        import copy

        args = ast.arguments(
            posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]
        )
        fields = ast.Tuple(
            elts=[copy.deepcopy(x) for x in (call.func, *call.args)], ctx=ast.Load()
        )
        return [
            ast.Lambda(args=args, body=fields),
            ast.Constant(isinstance(outer.op, ast.And)),
        ]

    def visit_Attribute(self, node):
        mode = self.mode(node)
        guard = self.guard(node, mode) or []
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and node.attr in names:
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="_br_scalar_read", ctx=ast.Load()),
                    args=[
                        node.value,
                        ast.Constant(node.attr),
                        ast.Constant(mode),
                        *guard,
                    ],
                    keywords=[],
                ),
                node,
            )
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tree = Transform(tree).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")

    def exec_module(self, module):
        global import_depth
        import_depth += 1
        try:
            return super().exec_module(module)
        finally:
            import_depth -= 1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not name.startswith(("sqlglot", "tests.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if (
            spec
            and spec.origin
            and spec.origin.endswith(".py")
            and Path(spec.origin).is_relative_to(root)
        ):
            spec.loader = Loader(name, spec.origin)
        return spec


class Plugin:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global current
        handled.add(item.nodeid)
        current = item.nodeid
        try:
            yield
        finally:
            current = ""

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global current
        old = current
        current = "@" + item.nodeid.rsplit("::", 1)[0]
        try:
            yield
        finally:
            current = old


def index(module, path, cls, scope):
    for stmt in cls.body:
        if isinstance(stmt, ast.ClassDef):
            index(module, path, stmt, scope + "." + stmt.name)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            ts = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            ok, value = scalar(stmt.value)
            if (
                len(ts) == 1
                and isinstance(ts[0], ast.Name)
                and ts[0].id.isupper()
                and ok
            ):
                name = ts[0].id
                declarations[module, scope, name] = (
                    (path, stmt.lineno),
                    type(value),
                    value,
                )
                names.add(name)


def main():
    if base["commit"] != PIN or base["baseline_failures"]:
        raise RuntimeError("Invalid baseline data")
    for path in sorted((root / "sqlglot").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        src = path.read_bytes()
        if rel not in base["source_hashes"]:
            continue
        if hashlib.sha256(src).hexdigest() != base["source_hashes"][rel]:
            raise RuntimeError("Source mismatch " + rel)
        module = rel[:-3].replace("/", ".").removesuffix(".__init__")
        tree = ast.parse(src)
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef):
                index(module, rel, cls, cls.name)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    from build_map import InlinePool

    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._br_scalar_read = read
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(
        lambda event, args: (
            external.add(current) if current and event == "subprocess.Popen" else None
        )
    )
    status = pytest.main(
        ["tests", "-q", "--tb=short", "-p", "no:cacheprovider"], plugins=[Plugin()]
    )
    if status:
        raise SystemExit(status)
    tests = base["tests"]
    ids = {n: i for i, n in enumerate(tests)}
    if handled != set(tests):
        raise RuntimeError("Test inventory mismatch")

    def expand(ns):
        result = set()
        for n in ns:
            if n.startswith("@"):
                result.update(
                    i for i, test in enumerate(tests) if test.startswith(n[1:] + "::")
                )
            elif n in ids:
                result.add(ids[n])
        return sorted(result)

    rows = {}
    for (path, line), modes in counts.items():
        if "" in modes.get("raw", ()):
            continue
        # A comparison or truth check during import can feed any later user.
        # No narrowing if the new scalar could change such a check.
        if not any(mode != "raw" for mode in modes):
            continue
        rows.setdefault(path, {})[str(line)] = {
            mode: [bool("" in ns), expand(ns)] for mode, ns in modes.items()
        }
    out.write_text(
        json.dumps(
            {"commit": PIN, "tests": tests, "rows": rows, "external": expand(external)},
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        "Scalar declarations with observation data:", sum(len(v) for v in rows.values())
    )


if __name__ == "__main__":
    main()
