"""Observe optimizer call boundaries during the pinned passing TPC-DS test."""

from concurrent.futures import Future
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import unittest

# Make the runtime helper importable both in the repository and in tools/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from checkpoint_graph import expression_digest


def main():
    root, baseline, output = [Path(p).resolve() for p in sys.argv[1:4]]
    catalog = json.loads(baseline.read_bytes())
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    prefix = str(root / "sqlglot/optimizer") + "/"
    sites, graphs, compiled = {}, {}, {}
    # Real parsing, driver startup/teardown, setup, pickle transfer, real row
    # execution and reference operations are outside the replay boundary.
    current, query = -1, -1
    observer = sys.monitoring
    slot = observer.PROFILER_ID
    observer.use_tool_id(slot, "optimizer checkpoint")

    def entered(code, offset):
        if not code.co_filename.startswith(prefix):
            return observer.DISABLE
        path = Path(code.co_filename).relative_to(root).as_posix()
        row = sites.setdefault(path, {}).setdefault(
            f"{code.co_firstlineno}:{code.co_name}", set()
        )
        row.add(current)
        if current == -1:
            return observer.DISABLE

    observer.register_callback(slot, observer.events.PY_START, entered)
    observer.set_events(slot, observer.events.PY_START)
    try:
        from tests import test_executor as mod
        import sqlglot.executor as driver

        original = driver.optimize

        def optimize(*a, **kwargs):
            nonlocal current
            if current != -1 or query == -1:
                raise RuntimeError("Unrecognized optimizer entry")
            current = query
            try:
                value = original(*a, **kwargs)
            finally:
                current = -1
            if query in graphs:
                raise RuntimeError("Multiple optimizer calls in query")
            graphs[query] = expression_digest(value)
            return value

        class InlinePool:
            def __init__(self, initializer=None, initargs=(), **kwargs):
                self.i = 0
                if initializer:
                    initializer(*initargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def submit(self, saved_call, expression, meta):
                nonlocal query
                expression = pickle.loads(pickle.dumps(expression))
                query = self.i
                self.i += 1
                try:
                    result = saved_call(expression, meta)
                finally:
                    query = -1
                f = Future()
                f.set_result(result)
                return f

        driver.optimize = optimize
        mod.ProcessPoolExecutor = InlinePool
        test = unittest.TestSuite([mod.TestExecutor("test_execute_tpcds")])
        run = unittest.TextTestRunner().run(test)
        if not run.wasSuccessful() or run.skipped:
            raise ValueError("Passing reference required")
    finally:
        observer.set_events(slot, 0)
        observer.free_tool_id(slot)
    for path, rows in sites.items():
        compiled[path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
        if compiled[path] != catalog["source_hashes"].get(path):
            raise ValueError(f"Unrecognized source {path}")
    ready = {
        p: {
            site: sorted(ids)
            for site, ids in data.items()
            if ids and ids <= graphs.keys()
        }
        for p, data in sites.items()
    }
    output.write_text(
        json.dumps(
            dict(
                commit=catalog["commit"], sources=compiled, sites=ready, graphs=graphs
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
