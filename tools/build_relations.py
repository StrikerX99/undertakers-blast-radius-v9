"""Profile previously opaque ordered comparisons on the passing pinned test suite."""

import ast
import builtins
import concurrent.futures
import hashlib
import importlib.abc
import importlib.machinery
import json
import operator
import os
from pathlib import Path
import sys

import pytest

root, map_file, out = [Path(s).resolve() for s in sys.argv[1:4]]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from set_relations import relation, OPS

m = json.loads(map_file.read_bytes())
sites, wanted, data = {}, set(), {}
for path, points in m["comparisons"].items():
    tree = ast.parse((root / path).read_bytes())
    for n in ast.walk(tree):
        if (
            not isinstance(n, ast.Compare)
            or len(n.ops) != 1
            or not isinstance(n.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE))
        ):
            continue
        key = f"{n.lineno}:{n.col_offset}:{n.end_lineno}:{n.end_col_offset}"
        record = points.get(key)
        if not record:
            continue
        ids = {
            m["tests"][i]
            for (left, right, _, _), index in record["variants"]
            if left[0] == right[0] == "unknown"
            for i in m["sets"][index]
        }
        if not ids:
            continue
        wanted.update(ids)
        sites.setdefault(path, {})[key] = type(n.ops[0]).__name__
outside = set()
active = None


def observe(site, op, left, right):
    if active is None:
        outside.add(site)
    else:
        d = data.setdefault(site, {})
        d[active] = d.get(active, 0) | relation(left, right)
    return OPS[op](left, right)


class Trace(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, n):
        self.generic_visit(n)
        key = f"{n.lineno}:{n.col_offset}:{n.end_lineno}:{n.end_col_offset}"
        op = sites[self.path].get(key)
        if not op:
            return n
        return ast.copy_location(
            ast.Call(
                ast.Name("_br_set_rel", ast.Load()),
                [
                    ast.Constant(self.path + "|" + key),
                    ast.Constant(op),
                    n.left,
                    n.comparators[0],
                ],
                [],
            ),
            n,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = Path(self.path).relative_to(root).as_posix()
        return compile(
            ast.fix_missing_locations(
                Trace(path).visit(ast.parse(Path(self.path).read_bytes()))
            ),
            self.path,
            "exec",
        )


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not name.startswith("sqlglot."):
            return
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if (
            spec
            and spec.origin
            and Path(spec.origin).is_relative_to(root)
            and Path(spec.origin).relative_to(root).as_posix() in sites
        ):
            spec.loader = Loader(name, spec.origin)
        return spec


class Plugin:
    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_call(self, item):
        global active
        active = item.nodeid
        try:
            return (yield)
        finally:
            active = None


if __name__ == "__main__":
    # Serialized executor submissions preserve pytest attribution across the pool.
    sys.path.insert(0, str(map_file.parent))
    from build_map import InlinePool

    concurrent.futures.ProcessPoolExecutor = InlinePool
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    builtins._br_set_rel = observe
    status = pytest.main(
        [*sorted(wanted), "-q", "-p", "no:cacheprovider", "--tb=line"],
        plugins=[Plugin()],
    )
    if status:
        raise SystemExit(status)
    regions = {
        site: row
        for site, rows in data.items()
        if site not in outside
        and (row := {test: mask for test, mask in rows.items() if not mask & 16})
    }
    payload = {
        "commit": m["commit"],
        "sources": {
            path: hashlib.sha256((root / path).read_bytes()).hexdigest()
            for path in sites
        },
        "sites": regions,
    }
    out.write_text(json.dumps(payload, separators=(",", ":")))
    print(
        "profiled",
        len(wanted),
        "tests;",
        len(regions),
        "set-operator sites",
        flush=True,
    )
