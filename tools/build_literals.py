"""Build comparison membership data from the pinned, unmodified reference suite."""

import ast
import concurrent.futures
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import pickle
import sys
import tempfile

import literal_collect as collector

# multiprocessing 'spawn' imports the main program before it unpickles the
# original pool initializer and its arguments. Start tracing at that boundary.
if __name__ == "__mp_main__" and "BR_LITERAL_COLLECTION" in os.environ:
    collector.start(os.environ["BR_LITERAL_COLLECTION"])


def discover(root, mapping):
    types, paths = [], {}
    for path, rows in mapping["comparisons"].items():
        if not path.startswith("sqlglot/"):
            continue
        source = (root / path).read_bytes()
        if hashlib.sha256(source).hexdigest() != mapping["source_hashes"][path]:
            raise RuntimeError("Profile differs from pinned checkout")
        for node in ast.walk(ast.parse(source)):
            if (
                not isinstance(node, ast.Compare)
                or len(node.ops) != 1
                or not isinstance(
                    node.ops[0], (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
                )
            ):
                continue
            left, right = node.left, node.comparators[0]
            if isinstance(left, ast.Constant) == isinstance(right, ast.Constant):
                continue
            literal, side = (
                (left, "right") if isinstance(left, ast.Constant) else (right, "left")
            )
            key = collector.location(node)
            if type(literal.value) not in (str, int) or key not in rows:
                continue
            if not mapping["sets"][rows[key]["unknown"]]:
                continue
            mode = type(literal.value).__name__
            if not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
                if mode != "int":
                    continue
                mode = "range"
            paths.setdefault(path, {})[key] = (len(types), side)
            types.append(mode)
    return {"root": str(root), "types": types, "sources": paths}


class Pool(concurrent.futures.ProcessPoolExecutor):
    def submit(self, fn, /, *args, **kwargs):
        return super().submit(
            collector.task, collector.active, pickle.dumps((fn, args, kwargs))
        )


def main():
    import pytest

    root, map_file, output = [Path(x).resolve() for x in sys.argv[1:4]]
    mapping = json.loads(map_file.read_bytes())
    ids = {name: i for i, name in enumerate(mapping["tests"])}
    config = discover(root, mapping)
    passed, skipped = set(), set()

    class Hooks:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            collector.active = item.nodeid
            try:
                yield
            finally:
                collector.active = "*"

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            before = collector.active
            collector.active = "@" + item.nodeid.rsplit("::", 1)[0]
            try:
                yield
            finally:
                collector.active = before

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_teardown(self, item, nextitem):
            before = collector.active
            collector.active = "@" + item.nodeid.rsplit("::", 1)[0]
            try:
                yield
            finally:
                collector.active = before

        def pytest_runtest_logreport(self, report):
            if report.skipped:
                skipped.add(report.nodeid)
            if report.when == "call" and report.passed:
                passed.add(report.nodeid)

    with tempfile.TemporaryDirectory(prefix="br-literals-") as td:
        folder = Path(td)
        (folder / "config").write_text(json.dumps(config))
        os.environ["BR_LITERAL_COLLECTION"] = td
        os.environ["SKIP_INTEGRATION"] = "0"
        os.chdir(root)
        sys.path.insert(0, str(root))
        # The parent flushes explicitly after pytest; only spawned workers need exit hooks.
        collector.start(td).cancel()
        concurrent.futures.ProcessPoolExecutor = Pool
        args = ["tests", "-q", "--tb=short", "-p", "no:cacheprovider"]
        status = pytest.main(args, plugins=[Hooks()])
        if status or not set(ids) <= passed | skipped:
            raise RuntimeError(
                f"Incomplete pinned baseline: {len(set(ids) - passed)} missing, status {status}"
            )
        collector.write_report()
        reports = list(folder.glob("*.json"))
        if {p.stem for p in reports} != {p.stem for p in folder.glob("*.started")}:
            raise RuntimeError("A worker failed to flush its observations")
        exact = defaultdict(lambda: defaultdict(set))
        opaque = defaultdict(set)
        ranges = defaultdict(lambda: defaultdict(set))

        def owners(names):
            out = set()
            for name in names:
                if name in ids:
                    out.add(ids[name])
                elif name.startswith("@"):
                    out.update(
                        i for n, i in ids.items() if n.startswith(name[1:] + "::")
                    )
                else:
                    out.update(ids.values())
            return out

        for report in reports:
            values = json.loads(report.read_bytes())
            for i, rows in values["exact"].items():
                for val, names in rows:
                    exact[int(i)][val].update(owners(names))
            for i, by_owner in values["bounds"].items():
                for owner, pair in by_owner.items():
                    ranges[int(i)][tuple(pair)].update(owners([owner]))
            for i, names in values["opaque"].items():
                opaque[int(i)].update(owners(names))
        sets, indices = [], {}

        def slot(values):
            values = tuple(sorted(values))
            if values not in indices:
                indices[values] = len(sets)
                sets.append(values)
            return indices[values]

        result = {}
        for path, sites in config["sources"].items():
            for key, (i, side) in sites.items():
                r = {str(val): slot(nums) for val, nums in sorted(exact[i].items())}
                if config["types"][i] == "range":
                    r = [
                        [lo, hi, slot(owners)]
                        for (lo, hi), owners in sorted(ranges[i].items())
                    ]
                result.setdefault(path, {})[key] = [
                    side,
                    config["types"][i],
                    slot(opaque[i] | {ids[n] for n in skipped if n in ids}),
                    r,
                ]
        out = {
            "commit": mapping["commit"],
            "sources": {p: mapping["source_hashes"][p] for p in result},
            "rows": result,
            "sets": sets,
        }
        output.write_text(json.dumps(out, separators=(",", ":")) + "\n")
        print(
            "Compared",
            len(config["types"]),
            "sites across",
            len(reports),
            "processes. Bytes",
            output.stat().st_size,
        )


if __name__ == "__main__":
    main()
