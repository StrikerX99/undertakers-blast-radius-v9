"""Measure per-query dependencies and results during a passing pinned executor test."""

import ast, builtins, concurrent.futures, hashlib, importlib.abc, importlib.machinery, json, os, pickle, sys, time
from pathlib import Path
import unittest

if not Path(__file__).with_name("query_case.py").is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from query_case import table_digest
from value_profile import OPS, snapshot

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2]).resolve()
active = -1
baseline = json.loads(Path(sys.argv[3]).read_bytes())
if baseline["commit"] != PIN or baseline["baseline_failures"]:
    raise ValueError("Unrecognized baseline coverage")
for name, digest in baseline["source_hashes"].items():
    if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
        raise ValueError("Pinned source mismatch at " + name)
rows = {}
queries = {}
compiled = {}


def observe(site, op, l, r):
    point = rows.setdefault(site, {})
    samples = point.setdefault(active, set())
    try:
        result = OPS[op](l, r)
    except BaseException:
        samples.add(None)
        raise
    # Record only ordinary primitives; any opaque input conservatively runs this query.
    if (
        type(l) not in (bool, int, str, float, type(None))
        or type(r) not in (bool, int, str, float, type(None))
        or type(result) is not bool
    ):
        samples.add(None)
    elif len(samples) < 32:
        samples.add((snapshot(l), snapshot(r), l is r, result))
    else:
        samples.add(None)
    return result


class Trace(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Compare(self, n):
        self.generic_visit(n)
        if len(n.ops) != 1 or not (
            isinstance(n.left, ast.Constant)
            or isinstance(n.comparators[0], ast.Constant)
        ):
            return n
        key = f"{self.path}|{n.lineno}:{n.col_offset}:{n.end_lineno}:{n.end_col_offset}"
        rows.setdefault(key, {})
        return ast.copy_location(
            ast.Call(
                ast.Name("_br_query_cmp", ast.Load()),
                [
                    ast.Constant(key),
                    ast.Constant(type(n.ops[0]).__name__),
                    n.left,
                    n.comparators[0],
                ],
                [],
            ),
            n,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = Path(self.path)
        path = source.relative_to(root).as_posix()
        data = source.read_bytes()
        compiled[path] = hashlib.sha256(data).hexdigest()
        tree = Trace(path).visit(ast.parse(data))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if not name.startswith("sqlglot.optimizer."):
            return
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            spec.loader = Loader(name, spec.origin)
        return spec


if __name__ == "__main__":
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.meta_path.insert(0, Finder())
    builtins._br_query_cmp = observe
    from tests import test_executor as mod
    from sqlglot import transpile

    class Pool:
        def __init__(self, initializer=None, initargs=(), **kwargs):
            self.index = 0
            if initializer:
                initializer(*initargs)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def submit(self, f, expression, meta):
            global active
            i = self.index
            self.index += 1
            # Attribute only worker computation. Parsing, transport and the DB
            # reference run outside any query for the conservative outside guard.
            expr = pickle.loads(pickle.dumps(expression))
            start = time.process_time()
            active = i
            try:
                result = f(expr, meta)
            finally:
                active = -1
            cost = time.process_time() - start
            future = concurrent.futures.Future()
            future.set_result(result)
            if result is not None:
                sql = mod.TestExecutor.tpcds_sqls[i][1]
                queries[i] = {
                    "sql": hashlib.sha256(sql.encode()).hexdigest(),
                    "result": table_digest(result),
                    "duck_sql": hashlib.sha256(
                        transpile(sql, write="duckdb")[0].encode()
                    ).hexdigest(),
                    "cpu": round(cost, 3),
                }
            return future

    mod.ProcessPoolExecutor = Pool
    result = unittest.TextTestRunner().run(
        unittest.TestSuite([mod.TestExecutor("test_execute_tpcds")])
    )
    if not result.wasSuccessful() or result.skipped:
        raise SystemExit("Baseline executor did not pass")

    def ordered(values):
        return sorted(values, key=repr)

    ready = {
        k: {i: ordered(s) for i, s in by.items()}
        for k, by in rows.items()
        if -1 not in by and by and set(by) <= queries.keys()
    }
    output.write_text(
        json.dumps(
            {
                "commit": PIN,
                "driver_sha": hashlib.sha256(
                    (root / "tests/test_executor.py").read_bytes()
                ).hexdigest(),
                "sources": compiled,
                "rows": ready,
                "queries": queries,
            },
            separators=(",", ":"),
        )
    )
    print(len(queries), "query results and", len(ready), "eligible comparison sites")
