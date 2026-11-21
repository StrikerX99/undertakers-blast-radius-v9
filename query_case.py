"""Replay individual cases of SQLGlot's pinned, deterministic TPC-DS executor test."""

import ast
import csv
import datetime
import decimal
import hashlib
import json
import os
from pathlib import Path
import pickle

CASE = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def table_digest(table):
    def scalar(x):
        kind = type(x)
        if x is None or kind in (str, int, bool):
            return x
        if kind is float:
            return ["float", x.hex()]
        if kind is decimal.Decimal:
            return ["decimal", str(x)]
        if kind in (datetime.date, datetime.time, datetime.datetime):
            return [kind.__name__, x.isoformat()]
        raise ValueError("Unrecognized table value")

    if type(table.columns) is not tuple or not all(
        type(c) is str for c in table.columns
    ):
        raise ValueError("Unrecognized columns")
    data = [table.columns, [[scalar(cell) for cell in row] for row in table.rows]]
    return hashlib.sha256(
        json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


def source_environment(root, expected_sha):
    from sqlglot import find_tables
    from sqlglot.executor import execute
    from sqlglot.executor.table import Table
    from tests.helpers import TPCDS_SCHEMA

    source = root / "tests/test_executor.py"
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha:
        raise ValueError("Unrecognized test driver")
    tree = ast.parse(raw)
    top = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in ("mp_execute", "open_file")
    ]
    cls = next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "TestExecutor"
    )
    init = next(
        n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "setUpClass"
    )
    setup = next(
        n for n in init.body if isinstance(n, ast.FunctionDef) and n.name == "setup"
    )
    scope = dict(
        csv=csv,
        ast=ast,
        Table=Table,
        execute=execute,
        find_tables=find_tables,
        _schema=TPCDS_SCHEMA,
    )
    exec(compile(ast.Module(top + [setup], []), str(source), "exec"), scope)
    folder = root / "tests/fixtures/optimizer/tpc-ds"

    class Views:
        # The view is used by the test's DuckDB reference. Its inputs are
        # unchanged; the Python table is loaded below using the pinned setup.
        def execute(self, statement):
            return None

    class InputTables(dict):
        def __missing__(self, name):
            scope["setup"](
                Views(), str(folder) + os.sep, name, TPCDS_SCHEMA[name], self
            )
            return self[name]

    scope["_tables"] = InputTables()
    return scope


def check(payload, env):
    import sys, signal

    root, reference, indices, marker, cpu_limit = payload
    os.environ.update(env)
    os.chdir(root)
    sys.path.insert(0, str(root))
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, cpu_limit)
    from tests.helpers import load_sql_fixture_pairs
    from sqlglot import parse_one, transpile

    scope = source_environment(root, reference["driver_sha"])
    queries = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    for i in indices:
        meta, sql, _ = queries[i]
        if (
            hashlib.sha256(sql.encode()).hexdigest()
            != reference["queries"][str(i)]["sql"]
        ):
            return
        parsed = pickle.loads(pickle.dumps(parse_one(sql)))
        result = scope["mp_execute"](parsed, meta)
        if (
            result is None
            or table_digest(result) != reference["queries"][str(i)]["result"]
        ):
            return
        generated = transpile(sql, write="duckdb")[0]
        if (
            hashlib.sha256(generated.encode()).hexdigest()
            != reference["queries"][str(i)]["duck_sql"]
        ):
            return
    marker.touch()
