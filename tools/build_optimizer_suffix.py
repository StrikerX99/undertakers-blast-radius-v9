"""Measure optimizer phase outputs and module calls on the pinned executor workload."""

import hashlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def build(root, selector_dir, dest):
    root, selector_dir, dest = [Path(p).resolve() for p in (root, selector_dir, dest)]
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
    ):
        raise ValueError("Unexpected SQLGlot source")
    if subprocess.check_output(["git", "-C", root, "status", "--porcelain", "-uno"]):
        raise ValueError("Modified SQLGlot source")
    sys.path[:0] = [str(root), str(selector_dir)]
    os.chdir(root)
    import sqlglot
    from sqlglot import parse_one, transpile
    from sqlglot.optimizer.optimizer import RULES
    import sqlglot.executor as ex
    from tests.helpers import load_sql_fixture_pairs
    from checkpoint_graph import expression_digest
    from optimizer_replay import GRAPH_RULES
    from query_case import source_environment, table_digest

    if Path(sqlglot.__file__).resolve() != root / "sqlglot/__init__.py":
        raise ValueError("Wrong checkout")
    reference = json.loads((selector_dir / "executor-queries.json").read_bytes())
    graphs = json.loads((selector_dir / "optimizer-checkpoints.json").read_bytes())
    raw_hash = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    if reference["commit"] != graphs["commit"] or graphs["commit"] != PIN:
        raise ValueError("Mismatched input profiles")
    original = ex.optimize
    scope = source_environment(root, reference["driver_sha"])
    labels = [fn.__module__ + ":" + fn.__name__ for fn in RULES]
    modules = {
        str(root / "sqlglot/optimizer" / (name + ".py")): name for name in GRAPH_RULES
    }
    state = {"phase": -1, "entries": 0}
    last = {}
    marks = {}

    def observe(code, offset):
        module = modules.get(code.co_filename)
        if module:
            qualname = code.co_qualname.split(".<locals>.", 1)[0]
            last[module + ":" + qualname] = state["phase"]
        else:
            return sys.monitoring.DISABLE

    checkpoints = []
    for i, rule in enumerate(RULES):

        def activate(expression, index=i):
            state["phase"] = index
            return expression

        checkpoints.extend((activate, rule))
        if rule.__name__ in GRAPH_RULES:

            def checkpoint(expression, index=i):
                try:
                    marks[str(index)] = expression_digest(expression)
                except ValueError:
                    pass
                return expression

            checkpoints.append(checkpoint)

    def measured(*args, **kwargs):
        state["entries"] += 1
        if "rules" in kwargs:
            raise ValueError("Rule override")
        graph = original(*args, rules=checkpoints, **kwargs)
        state["phase"] = len(RULES)
        if expression_digest(graph) != graphs["graphs"][str(case)]:
            raise ValueError("Changed final graph")
        return graph

    ex.optimize = measured
    monitor = sys.monitoring
    tool = monitor.PROFILER_ID
    monitor.use_tool_id(tool, "clean optimizer phases")
    monitor.register_callback(tool, monitor.events.PY_START, observe)
    monitor.set_events(tool, monitor.events.PY_START)
    corpus = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    output = {}
    for si, record in reference["queries"].items():
        case = int(si)
        meta, sql, _ = corpus[case]
        state.update(phase=-1, entries=0)
        last.clear()
        marks.clear()
        if (
            not meta.get("execute")
            or hashlib.sha256(sql.encode()).hexdigest() != record["sql"]
        ):
            raise ValueError("Changed fixture case")
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        table = scope["mp_execute"](expr, meta)
        generated = transpile(sql, write="duckdb")
        if (
            state["entries"] != 1
            or table_digest(table) != record["result"]
            or len(generated) != 1
            or hashlib.sha256(generated[0].encode()).hexdigest() != record["duck_sql"]
        ):
            raise ValueError("Unexpected worker result")
        output[si] = {"last": last.copy(), "graphs": marks.copy()}
    monitor.set_events(tool, 0)
    monitor.free_tool_id(tool)
    dest.write_text(
        json.dumps(
            {
                "pin": PIN,
                "optimizer": raw_hash(root / "sqlglot/optimizer/optimizer.py"),
                "rules": labels,
                "queries": output,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    print(
        len(output),
        "full native query inputs with matching executor output, optimized graph and transpiled SQL; stage markers:",
        sum(len(x["graphs"]) for x in output.values()),
        flush=True,
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
