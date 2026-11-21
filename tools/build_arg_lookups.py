"""Observe exact expression-argument dict lookup equivalences on the unmodified baseline."""

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


def install(root, m):
    results, indexed = {}, {}

    def read(site, obj, key):
        result = obj.get(key)
        row = results[site]
        if not row["unsafe"]:
            if type(obj) is not dict or any(type(k) is not str for k in obj):
                row["unsafe"] = True
            elif result is None:
                row["seen"] = True
                row["different_on_none"].update(
                    k for k, v in obj.items() if v is not None
                )
            else:
                row["seen"] = True
                same = {k for k, v in obj.items() if v is result}
                if row["same_on_nonnull"] is None:
                    row["same_on_nonnull"] = same
                else:
                    row["same_on_nonnull"].intersection_update(same)
        return result

    class Mark(ast.NodeTransformer):
        def __init__(self, source):
            self.source = source

        def visit_Call(self, n):
            self.generic_visit(n)
            if (
                isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "args"
                and len(n.args) == 1
                and not n.keywords
                and isinstance(n.args[0], ast.Constant)
                and type(n.args[0].value) is str
            ):
                site = (
                    self.source,
                    n.lineno,
                    n.col_offset,
                    n.end_lineno,
                    n.end_col_offset,
                )
                results.setdefault(
                    site,
                    dict(
                        key=n.args[0].value,
                        unsafe=False,
                        seen=False,
                        different_on_none=set(),
                        same_on_nonnull=None,
                    ),
                )
                call = ast.Call(
                    ast.Name("_br_exact_lookup_", ast.Load()),
                    [ast.Constant(site), n.func.value, n.args[0]],
                    [],
                )
                return ast.copy_location(call, n)
            return n

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            p = Path(self.path)
            source = p.relative_to(root).as_posix()
            raw = p.read_bytes()
            if hashlib.sha256(raw).hexdigest() != m["source_hashes"].get(source):
                raise ValueError("Unrecognized source " + source)
            indexed[source] = hashlib.sha256(raw).hexdigest()
            t = Mark(source).visit(ast.parse(raw))
            return compile(ast.fix_missing_locations(t), self.path, "exec")

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith("sqlglot."):
                return
            s = importlib.machinery.PathFinder.find_spec(name, path)
            if (
                s
                and s.origin
                and s.origin.endswith(".py")
                and Path(s.origin).is_relative_to(root)
                and Path(s.origin).relative_to(root).as_posix() in m["source_hashes"]
            ):
                s.loader = Loader(name, s.origin)
            return s

    builtins._br_exact_lookup_ = read
    sys.meta_path.insert(0, Finder())
    return results, indexed


def main():
    root, mapping_file, output, test_catalog = [
        Path(s).resolve() for s in sys.argv[1:5]
    ]
    catalog = json.loads(test_catalog.read_bytes())
    expected = {row["id"] for row in catalog["tests"]}
    skipped = set(catalog["skipped"])
    m = json.loads(mapping_file.read_bytes())
    results, indexed = install(root, m)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from build_map import InlinePool

    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    args = ["tests", "-q", "--tb=short", "-p", "no:cacheprovider"]

    class NumberedResults:
        def __init__(self):
            self.passed = set()
            self.skipped = set()

        def pytest_runtest_logreport(self, report):
            if report.skipped:
                self.skipped.add(report.nodeid)
            elif report.when == "call" and report.passed:
                self.passed.add(report.nodeid)

    oracle = NumberedResults()
    if (
        pytest.main(args, plugins=[oracle])
        or oracle.passed != expected
        or oracle.skipped != skipped
        or expected | skipped != set(m["tests"])
    ):
        raise SystemExit("Every numbered test must pass the instrumented suite")
    clean = {}
    for site, row in results.items():
        if row["unsafe"] or not row["seen"]:
            continue
        path, *span = site
        clean.setdefault(path, {})[":".join(map(str, span))] = [
            row["key"],
            sorted(row["different_on_none"]),
            None if row["same_on_nonnull"] is None else sorted(row["same_on_nonnull"]),
        ]
    output.write_text(
        json.dumps(
            dict(
                commit=m["commit"], sources={p: indexed[p] for p in clean}, rows=clean
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    import subprocess

    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("confirm_worker_args.py")),
            str(root),
            str(mapping_file),
            str(output),
        ],
        check=True,
    )


if __name__ == "__main__":
    main()
