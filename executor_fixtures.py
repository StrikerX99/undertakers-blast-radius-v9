"""Follow the fixture fields used by the audited baseline executor test class."""

import ast
import hashlib
import json
from pathlib import Path
import sys
import tempfile

import forkprobe

# The class owns both fixture lists. Keep the entire analysis tied to this
# exact implementation of setup, SQL execution and DataFrame comparison.
EXECUTOR_FILE = "tests/test_executor.py"
EXECUTOR_SHA = "270a844a7b0b84045efde017135e6208ca07d7d93eba395be17c20c6e918923d"


class ExecutorFixtures:
    def __init__(self, root, by_name):
        self.root = root
        self.uses = {}
        raw = (root / EXECUTOR_FILE).read_bytes()
        if hashlib.sha256(raw).hexdigest() != EXECUTOR_SHA:
            return
        cls = next(
            n
            for n in ast.parse(raw).body
            if isinstance(n, ast.ClassDef) and n.name == "TestExecutor"
        )
        funcs = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}
        fields = {}
        for assign in ast.walk(funcs["setUpClass"]):
            if not isinstance(assign, ast.Assign) or not isinstance(
                assign.targets[0], ast.Attribute
            ):
                continue
            value = assign.value
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "list"
                and len(value.args) == 1
            ):
                continue
            inner = value.args[0]
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "load_sql_fixture_pairs"
            ):
                fields[assign.targets[0].attr] = "tests/fixtures/" + ast.literal_eval(
                    inner.args[0]
                )
        # Unlike setUpClass, a test may only use the list on one branch of the
        # shared helper. Read the actual list passed by each executor entry.
        for field, path in fields.items():
            tests = self.uses[path] = {}
            for name, fn in funcs.items():
                number = by_name.get(f"{EXECUTOR_FILE}::{cls.name}::{name}")
                if number is None:
                    continue
                calls = [
                    n
                    for n in ast.walk(fn)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_mp_execute"
                ]
                if calls:
                    (call,) = calls
                    which = call.args[2]
                    if isinstance(which, ast.Attribute) and which.attr in fields:
                        tests[number] = "execute" if which.attr == field else "unused"
                    continue
                todo = [name]
                seen = set()
                uses_field = False
                while todo:
                    method = todo.pop()
                    if method in seen:
                        continue
                    seen.add(method)
                    for n in ast.walk(funcs[method]):
                        if not (
                            isinstance(n, ast.Attribute)
                            and isinstance(n.value, ast.Name)
                            and n.value.id in ("self", "cls")
                        ):
                            continue
                        if n.attr == field:
                            uses_field = True
                        if n.attr in funcs and n.attr not in seen:
                            todo.append(n.attr)
                if not uses_field:
                    tests[number] = "unused"
                elif (
                    "cached_execute" in seen
                    and any(
                        isinstance(n, ast.Name) and n.id == "transpile"
                        for n in ast.walk(fn)
                    )
                    and any(
                        isinstance(n, ast.Name) and n.id == "assert_frame_equal"
                        for n in ast.walk(fn)
                    )
                ):
                    tests[number] = "duckdb"

    def unchanged(self, path, old, new, readers):
        modes = self.uses.get(path, {})
        if not modes:
            return set()
        # The caller parsed both pair lists using the pinned setup helper.
        equal = {n for n in readers if modes.get(n) == "unused"}
        if len(old) != len(new) or any(a[1] != b[1] for a, b in zip(old, new)):
            return equal
        if all(
            bool(a[0].get("execute")) == bool(b[0].get("execute"))
            for a, b in zip(old, new)
        ):
            equal.update(n for n in readers if modes.get(n) == "execute")
        database = {n for n in readers if modes.get(n) == "duckdb"}
        if not database:
            return equal
        if all(a[2] == b[2] for a, b in zip(old, new)) or self.same_statements(
            old, new
        ):
            equal.update(database)
        return equal

    def same_statements(self, old, new):
        # Execute the original and new SQLGlot transpilation streams in fresh
        # children. Identical input SQL keeps the DataFrame cache keys fixed.
        with tempfile.TemporaryDirectory(prefix="br-fixture-db-") as directory:
            fingerprints = []
            for i, records in enumerate((old, new)):
                output = Path(directory) / str(i)
                forkprobe.run((self.root, records, output), {}, statement_stream, 1.5)
                if not output.is_file():
                    return False
                digest = output.read_bytes()
                if len(digest) != hashlib.sha256().digest_size:
                    return False
                fingerprints.append(digest)
            return fingerprints[0] == fingerprints[1]


def statement_stream(request):
    root, records, output = request
    sys.path.insert(0, str(root))
    from sqlglot import transpile

    result = []
    seen = set()
    for _, sql, optimized in records:
        if sql not in seen:
            result.append(transpile(sql, write="duckdb")[0])
            seen.add(sql)
        result.append(transpile(optimized, write="duckdb")[0])
    output.write_bytes(hashlib.sha256(json.dumps(result).encode()).digest())
