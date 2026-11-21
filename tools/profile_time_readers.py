"""Refine the shared time-conversion dependency by the actual baseline receiver.

The first stage gives a superset of possible consumers. A baseline conversion
with an unrecognized mapping or trie keeps that test for every time table.
"""

import ast
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
import pytest

from build_time_dependencies import time_table

root = Path(sys.argv[1]).resolve()
data = json.loads(Path(sys.argv[2]).read_bytes())
out = Path(sys.argv[3]).resolve()
index = defaultdict(set)
dependencies = {}
readers = defaultdict(set)
seen = set()
unknown = set()
current = None


def origin(obj):
    cls = obj if isinstance(obj, type) else type(obj)
    return dependencies.get(id(cls))


def record(tables):
    if current is not None:
        seen.add(current)
        if tables is None:
            unknown.add(current)
        else:
            for p in tables:
                readers[p].add(current)


def get_time_attr(obj, name):
    result = getattr(obj, name)
    if current is not None:
        record(origin(obj))
    return result


def conversion(mapping, trie):
    if current is not None:
        record(index.get(id(mapping)))
        if trie:
            record(index.get(id(trie)))


class Transform(ast.NodeTransformer):
    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and time_table(node.attr):
            return ast.copy_location(
                ast.Call(
                    func=ast.Name("_br_time_attr", ast.Load()),
                    args=[node.value, ast.Constant(node.attr)],
                    keywords=[],
                ),
                node,
            )
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        tree = Transform().visit(ast.parse(self.get_data(self.path)))
        if name == "sqlglot.time":
            fn = next(
                n
                for n in tree.body
                if isinstance(n, ast.FunctionDef) and n.name == "format_time"
            )
            fn.body.insert(1, ast.parse("_br_time_conversion(mapping, trie)").body[0])
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


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
        current = item.nodeid
        try:
            yield
        finally:
            current = None

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global current
        current = "@" + item.nodeid.rsplit("::", 1)[0]
        try:
            yield
        finally:
            current = item.nodeid


def main():
    os.chdir(root)
    import concurrent.futures

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from build_map import InlinePool

    concurrent.futures.ProcessPoolExecutor = InlinePool
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    builtins._br_time_attr = get_time_attr
    builtins._br_time_conversion = conversion
    sys.meta_path.insert(0, Finder())
    from sqlglot.dialects.dialect import Dialect

    # Resolve inherited and cross-module aliases from the actual loaded class
    # hierarchy. Taking all imported dialects is conservative for arbitrary
    # class-level copies, reversals, merges and explicit override dictionaries.
    classes = {*Dialect.classes.values(), Dialect}
    module_paths = {
        cls.__module__: cls.__module__.replace(".", "/") + ".py" for cls in classes
    }
    edges = defaultdict(set)
    for cls in classes:
        for value in [*vars(sys.modules[cls.__module__]).values(), *cls.__mro__]:
            if isinstance(value, type) and issubclass(value, Dialect):
                edges[cls.__module__].add(value.__module__)

    def sources(module):
        todo = {module}
        visited = set()
        while todo:
            name = todo.pop()
            if name in visited:
                continue
            visited.add(name)
            todo.update(edges[name] - visited)
        return {module_paths[m] for m in visited if m in module_paths}

    for cls in classes:
        paths = sources(cls.__module__)
        dependencies[id(cls)] = paths
        for name in dir(cls):
            if time_table(name):
                value = getattr(cls, name)
                if isinstance(value, dict):
                    index[id(value)].update(paths)
    result = pytest.main(
        [*data["readers"], "-q", "--tb=short", "-p", "no:cacheprovider"],
        plugins=[Plugin()],
    )
    if result:
        raise SystemExit(result)

    def expand(tests):
        return {
            name
            for name in data["readers"]
            if name in tests or "@" + name.rsplit("::", 1)[0] in tests
        }

    # Readers without any attributable observation remain unconditionally.
    pending = set(data["readers"]) - expand(seen)
    unresolved = pending | expand(unknown)
    data["paths"] = {
        p: sorted(expand(ids) | unresolved)
        for p, ids in readers.items()
        if p in data["locations"]
    }
    out.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
