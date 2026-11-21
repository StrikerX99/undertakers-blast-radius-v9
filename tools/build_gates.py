"""Trace evaluation of short-circuit operands in the unmodified pinned SQLGlot suite."""

import ast
import builtins
import concurrent.futures
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

import pytest

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
root = Path(sys.argv[1]).resolve()
base = json.loads(Path(sys.argv[2]).read_bytes())
output = Path(sys.argv[3]).resolve()
assert base["commit"] == PIN and not base["baseline_failures"]
for path, expected in base["source_hashes"].items():
    assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, path

active = ""
locations = []
seen = []
last = []


def mark(site):
    if last[site] != active:
        last[site] = active
        seen[site].add(active)
    return True


class TraceGates(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def gate(self, node):
        key = f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
        index = len(seen)
        locations.append((self.path, key))
        last.append(None)
        seen.append(set())
        test = ast.Call(
            ast.Name("_br_baseline_gate_", ast.Load()), [ast.Constant(index)], []
        )
        return ast.copy_location(ast.IfExp(test, node, ast.Constant(None)), node)

    def visit_IfExp(self, node):
        self.generic_visit(node)
        node.body = self.gate(node.body)
        node.orelse = self.gate(node.orelse)
        return node

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        node.values[1:] = [self.gate(v) for v in node.values[1:]]
        return node

    def visit_ListComp(self, node):
        self.generic_visit(node)
        node.elt = self.gate(node.elt)
        return node

    visit_SetComp = visit_GeneratorExp = visit_ListComp

    def visit_DictComp(self, node):
        self.generic_visit(node)
        node.key = self.gate(node.key)
        node.value = self.gate(node.value)
        return node

    def visit_comprehension(self, node):
        self.generic_visit(node)
        node.ifs = [self.gate(v) for v in node.ifs]
        return node

    def visit_Lambda(self, node):
        self.generic_visit(node)
        node.body = self.gate(node.body)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tagged = TraceGates(Path(self.path).relative_to(root).as_posix()).visit(tree)
        return compile(ast.fix_missing_locations(tagged), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (
            spec
            and spec.origin
            and spec.origin.endswith(".py")
            and Path(spec.origin).is_relative_to(root)
        ):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Tests:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global active
        active = item.nodeid
        try:
            yield
        finally:
            active = ""

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global active
        prior = active
        active = "@" + item.nodeid.rsplit("::", 1)[0]
        try:
            yield
        finally:
            active = prior


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from build_map import InlinePool

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._br_baseline_gate_ = mark
    sys.meta_path.insert(0, Finder())
    if pytest.main(
        ["tests", "-q", "--tb=short", "-p", "no:cacheprovider"], plugins=[Tests()]
    ):
        raise SystemExit("The instrumented baseline failed")
    tests = {n: i for i, n in enumerate(base["tests"])}
    origins = {}
    for (path, key), names in zip(locations, seen):
        # Modules can reload. Union observations for all instances of this source.
        origins.setdefault((path, key), set()).update(names)
    rows, sets, indices = {}, [], {}
    for (path, key), names in sorted(origins.items()):
        if "" in names:
            continue
        ids = {tests[n] for n in names if n in tests}
        for owner in (n[1:] + "::" for n in names if n.startswith("@")):
            ids.update(i for n, i in tests.items() if n.startswith(owner))
        ids = tuple(sorted(ids))
        if ids not in indices:
            indices[ids] = len(sets)
            sets.append(ids)
        rows.setdefault(path, {})[key] = indices[ids]
    output.write_text(
        json.dumps({"commit": PIN, "rows": rows, "sets": sets}, separators=(",", ":"))
    )
    print(
        f"{len(origins)} evaluated or registered expressions; {len(sets)} unique test groups"
    )
