"""Build dependency data solely by executing the unmodified pinned baseline.

Instrumentation observes Python lines, class attributes, dictionary entries and
fixture reads. No patches or expected failure labels are inputs to this script.
"""

import ast
import builtins
import concurrent.futures
import importlib.abc
import importlib.machinery
import json
import hashlib
import os
import pickle
from pathlib import Path
import sys
import time
from collections import defaultdict
from value_profile import snapshot, OPS

ROOT = Path(os.environ.get("SQLGLOT_ROOT", "/opt/sqlglot")).resolve()
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "/app/coverage-map.json")
ACTIVE = ""
DATA = defaultdict(lambda: defaultdict(set))
FILES = defaultdict(set)
DEFINITIONS = {}
ATTR_CACHE = {}
LAST_MARK = {}
IMPORTS = defaultdict(set)
REGIONS = defaultdict(lambda: defaultdict(set))
COMPARISONS = {}


def mark(origins):
    if ACTIVE:
        for origin in origins:
            if LAST_MARK.get(origin) == ACTIVE:
                continue
            LAST_MARK[origin] = ACTIVE
            path, start, end = origin[:3]
            if len(origin) == 5:
                key = f"{start}:{origin[3]}:{end}:{origin[4]}"
                REGIONS[path][key].add(ACTIVE)
            for line in range(start, end + 1):
                DATA[path][line].add(ACTIVE)


class TrackedDict(dict):
    def __init__(self, value, origins):
        super().__init__(value)
        self.origins = origins

    def __getitem__(self, key):
        mark(self.origins.get(key, ()))
        return super().__getitem__(key)

    def get(self, key, default=None):
        mark(self.origins.get(key, ()))
        return super().get(key, default)

    def __contains__(self, key):
        mark(self.origins.get(key, ()))
        return super().__contains__(key)

    def items(self):
        for origin in self.origins.values():
            mark(origin)
        return super().items()

    def values(self):
        for origin in self.origins.values():
            mark(origin)
        return super().values()

    def __iter__(self):
        for origin in self.origins.values():
            mark(origin)
        return super().__iter__()

    def copy(self):
        return TrackedDict(self, self.origins.copy())


def tracked_dict(parts, path):
    value, origins = {}, {}
    for key, item, start, end, col, endcol in parts:
        if start == -1:
            value.update(item)
            if isinstance(item, TrackedDict):
                origins.update(item.origins)
        else:
            value[key] = item
            origins[key] = [(path, start, end, col, endcol)]
    return TrackedDict(value, origins)


def read_attr(obj, name):
    value = getattr(obj, name)
    if ACTIVE and not isinstance(value, TrackedDict):
        cls = obj if isinstance(obj, type) else type(obj)
        key = (cls, name)
        if key not in ATTR_CACHE:
            origins = []
            for base in cls.__mro__:
                if name in vars(base):
                    origin = DEFINITIONS.get((base.__module__, base.__qualname__, name))
                    if origin:
                        origins.append(origin)
                    break
            ATTR_CACHE[key] = origins
        mark(ATTR_CACHE[key])
    return value


def observe_compare(path, key, op, left, right):
    result = OPS[op](left, right)
    if ACTIVE:
        record = COMPARISONS.setdefault((path, key), {"variants": {}, "unknown": set()})
        if ACTIVE not in record["unknown"]:
            if type(result) is not bool:
                record["unknown"].add(ACTIVE)
            else:
                values = (snapshot(left), snapshot(right), left is right, result)
                variants = record["variants"]
                if values in variants or len(variants) < 64:
                    variants.setdefault(values, set()).add(ACTIVE)
                else:
                    record["unknown"].add(ACTIVE)
    return result


builtins._br_compare = observe_compare
builtins._br_read_attr = read_attr
builtins._br_dict = tracked_dict


class Instrument(ast.NodeTransformer):
    def __init__(self, module, path):
        self.module, self.path, self.scope, self.function_depth = module, path, [], 0

    def visit_ClassDef(self, node):
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()
        return node

    def visit_FunctionDef(self, node):
        self.function_depth += 1
        self.generic_visit(node)
        self.function_depth -= 1
        return node

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Assign(self, node):
        if not self.function_depth:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    DEFINITIONS[(self.module, ".".join(self.scope), target.id)] = (
                        self.path,
                        node.lineno,
                        node.end_lineno,
                    )
        return self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if not self.function_depth and isinstance(node.target, ast.Name):
            DEFINITIONS[(self.module, ".".join(self.scope), node.target.id)] = (
                self.path,
                node.lineno,
                node.end_lineno,
            )
        return self.generic_visit(node)

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1:
            key = f"{node.lineno}:{node.col_offset}:{node.end_lineno}:{node.end_col_offset}"
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="_br_compare", ctx=ast.Load()),
                    args=[
                        ast.Constant(self.path),
                        ast.Constant(key),
                        ast.Constant(type(node.ops[0]).__name__),
                        node.left,
                        node.comparators[0],
                    ],
                    keywords=[],
                ),
                node,
            )
        return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load) and (
            node.attr.isupper() or node.attr == "arg_types"
        ):
            return ast.copy_location(
                ast.Call(
                    func=ast.Name(id="_br_read_attr", ctx=ast.Load()),
                    args=[node.value, ast.Constant(node.attr)],
                    keywords=[],
                ),
                node,
            )
        return node

    def visit_Dict(self, node):
        self.generic_visit(node)
        if self.function_depth:
            return node
        parts = []
        for key, value in zip(node.keys, node.values):
            parts.append(
                ast.Tuple(
                    elts=[
                        key or ast.Constant(None),
                        value,
                        ast.Constant(key.lineno if key else -1),
                        ast.Constant(value.end_lineno),
                        ast.Constant(key.col_offset if key else 0),
                        ast.Constant(value.end_col_offset),
                    ],
                    ctx=ast.Load(),
                )
            )
        return ast.copy_location(
            ast.Call(
                func=ast.Name(id="_br_dict", ctx=ast.Load()),
                args=[ast.List(elts=parts, ctx=ast.Load()), ast.Constant(self.path)],
                keywords=[],
            ),
            node,
        )


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source, filename=self.path)
        tree = Instrument(fullname, str(Path(self.path).relative_to(ROOT))).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class InlinePool:
    """Keep child computations inside the measured test's coverage context."""

    def __init__(self, *args, initializer=None, initargs=(), **kwargs):
        if initializer:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        future = concurrent.futures.Future()
        try:
            # Match the process boundary: SQLGlot serializes ASTs through its
            # own serde module when passing arguments/results to pool workers.
            child_args, child_kwargs = pickle.loads(pickle.dumps((args, kwargs)))
            value = fn(*child_args, **child_kwargs)
            future.set_result(pickle.loads(pickle.dumps(value)))
        except BaseException as exc:
            future.set_exception(exc)
        return future

    def shutdown(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def audit(event, args):
    if event == "open" and ACTIVE and isinstance(args[0], str):
        # Import machinery reading a module is already represented by code/data
        # dependencies. It is not a test inspecting the source text itself.
        if args[0].endswith(".py"):
            frame = sys._getframe(1)
            while frame:
                filename = frame.f_code.co_filename
                if filename.startswith("<frozen importlib"):
                    return
                if filename.startswith(str(ROOT / "tests")):
                    break
                frame = frame.f_back
        try:
            path = str(Path(args[0]).resolve().relative_to(ROOT))
        except ValueError:
            return
        if not path.endswith(".pyc"):
            FILES[path].add(ACTIVE)


def main():
    global ACTIVE
    import coverage
    import pytest

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    os.environ["SKIP_INTEGRATION"] = "0"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    concurrent.futures.ProcessPoolExecutor = InlinePool
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(audit)
    cov = coverage.Coverage(
        data_file=None,
        source=[str(ROOT / "sqlglot"), str(ROOT / "tests")],
        config_file=False,
    )
    contexts = {}
    durations = {}
    failures = set()

    class Plugin:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            global ACTIVE
            ACTIVE = item.nodeid
            cov.switch_context(ACTIVE)
            start = time.process_time()
            yield
            durations[ACTIVE] = time.process_time() - start
            ACTIVE = ""
            cov.switch_context("")

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            global ACTIVE
            ACTIVE = "@setup:" + item.nodeid.rsplit("::", 1)[0]
            cov.switch_context(ACTIVE)
            yield
            ACTIVE = item.nodeid
            cov.switch_context(ACTIVE)

        def pytest_runtest_logreport(self, report):
            if report.failed:
                failures.add(report.nodeid)

        def pytest_collection_finish(self, session):
            for item in session.items:
                contexts[item.nodeid] = item.path.relative_to(ROOT).as_posix()

    cov.start()
    result = pytest.main(
        ["tests", "-q", "-p", "no:cacheprovider", "--tb=short"], plugins=[Plugin()]
    )
    cov.stop()
    data = cov.get_data()
    import_lines = {}
    for absolute in data.measured_files():
        path = str(Path(absolute).relative_to(ROOT))
        for line, names in data.contexts_by_lineno(absolute).items():
            DATA[path][line].update(n for n in names if n)
            if "" in names:
                import_lines.setdefault(path, []).append(line)

    # Expand setup contexts to all methods of the same class.
    def expand(names):
        result = set()
        for name in names:
            if name.startswith("@setup:"):
                prefix = name[len("@setup:") :] + "::"
                result.update(n for n in contexts if n.startswith(prefix))
            else:
                result.add(name)
        return result

    for rows in DATA.values():
        for line in rows:
            rows[line] = expand(rows[line])
    for path in FILES:
        FILES[path] = expand(FILES[path])
    # Class setup/teardown can read fixtures outside an individual test. Preserve
    # dependencies across every member of a class when setup is shared.
    setup_files = {}
    for path, names in FILES.items():
        setup_files[path] = sorted(names)
    payload = {
        "version": 1,
        "commit": "eb11aec4be8cb2122afe713a4816662c3f0fb2fa",
        "tests": sorted(contexts),
        "import_lines": import_lines,
        "lines": {
            p: {str(line): sorted(s) for line, s in rows.items() if s}
            for p, rows in DATA.items()
        },
        "files": setup_files,
        "durations": durations,
        "baseline_failures": sorted(failures),
    }
    # Intern test names and identical dependency sets for a small portable map.
    ids = {name: i for i, name in enumerate(payload["tests"])}
    sets, seen = [], {}

    def intern(names):
        value = tuple(sorted(ids[n] for n in names if n in ids))
        if value not in seen:
            seen[value] = len(sets)
            sets.append(value)
        return seen[value]

    payload["lines"] = {
        p: {line: intern(s) for line, s in rows.items()}
        for p, rows in payload["lines"].items()
    }
    payload["files"] = {p: intern(s) for p, s in payload["files"].items()}
    payload["source_hashes"] = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in set(payload["lines"]) | set(payload["files"])
        if (ROOT / p).is_file()
    }
    payload["regions"] = {
        p: {k: intern(expand(s)) for k, s in rows.items()}
        for p, rows in REGIONS.items()
    }
    payload["comparisons"] = {}
    for (path, key), record in COMPARISONS.items():
        payload["comparisons"].setdefault(path, {})[key] = {
            "variants": [
                [values, intern(expand(names))]
                for values, names in record["variants"].items()
            ],
            "unknown": intern(expand(record["unknown"])),
        }
    payload["sets"] = sets
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    print(
        f"Map written: {OUT} ({OUT.stat().st_size} bytes), pytest status={result}",
        file=sys.stderr,
    )
    return result


if __name__ == "__main__":
    raise SystemExit(main())
