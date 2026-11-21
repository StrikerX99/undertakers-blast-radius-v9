"""Compare the values returned by the pinned project's own fixture loaders."""

import ast
import hashlib
import io
import json
import os

from executor_fixtures import ExecutorFixtures


class FixtureViews:
    def __init__(self, file, root, baseline, by_name):
        data = json.loads(file.read_bytes())
        path = root / "tests/helpers.py"
        raw = path.read_bytes()
        if (
            data["commit"] != baseline["commit"]
            or hashlib.sha256(raw).hexdigest() != data["sha256"]
        ):
            raise ValueError("Unrecognized SQL fixture helper")
        self.files = data["files"]
        self.root = root
        self.by_name = by_name
        self.executor_numbers = {
            n
            for name, n in by_name.items()
            if name.startswith("tests/test_executor.py::TestExecutor::")
        }
        self.executor = None
        self.numbers = [by_name.get(name) for name in baseline["tests"]]
        names = {
            "_extract_meta",
            "_filter_comments",
            "load_sql_fixtures",
            "load_sql_fixture_pairs",
        }
        funcs = [
            n
            for n in ast.parse(raw).body
            if isinstance(n, ast.FunctionDef) and n.name in names
        ]
        tree = ast.Module(body=funcs, type_ignores=[])
        self.code = compile(tree, str(path), "exec")

    def unchanged_readers(self, path, before, after):
        readers = self.files.get(path, {})
        if not readers:
            return set()
        methods = {"single": "load_sql_fixtures", "pairs": "load_sql_fixture_pairs"}
        equal = set()
        unused = set()
        for mode in {mode for uses in readers.values() for mode in uses}:

            def load(text):
                # Only baseline helper definitions execute here. The patch is
                # input data, and each read starts with a new text stream.
                scope = {
                    "os": os,
                    "FIXTURES_DIR": "",
                    "open": lambda *args, **kw: io.StringIO(text, newline=None),
                }
                exec(self.code, scope)
                return list(scope[methods[mode]](path))

            try:
                old, new = load(before), load(after)
                if old == new:
                    equal.add(mode)
                elif mode == "pairs" and any(
                    self.numbers[int(i)] in self.executor_numbers for i in readers
                ):
                    if self.executor is None:
                        self.executor = ExecutorFixtures(self.root, self.by_name)
                    pair_readers = {
                        self.numbers[int(i)]
                        for i, modes in readers.items()
                        if set(modes) == {"pairs"} and self.numbers[int(i)] is not None
                    }
                    unused.update(self.executor.unchanged(path, old, new, pair_readers))
            except (IndexError, ValueError, KeyError):
                continue
        return unused | {
            self.numbers[int(i)]
            for i, modes in readers.items()
            if self.numbers[int(i)] is not None and equal.issuperset(modes)
        }
