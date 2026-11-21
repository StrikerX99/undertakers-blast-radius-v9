"""Record per-entry invocation coverage from the pinned baseline. Python 3.13."""

import ast
import builtins
import concurrent.futures
from collections import defaultdict
from functools import lru_cache
import importlib.abc
import importlib.machinery
import inspect
import hashlib
import json
import os
from pathlib import Path
import sys
import textwrap
import types
import pytest

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
root = Path(sys.argv[1]).resolve()
base = json.loads(Path(sys.argv[2]).read_bytes())
out = Path(sys.argv[3]).resolve()
if base["commit"] != PIN or base["baseline_failures"]:
    raise ValueError("Baseline root or test status does not match source")
current = ""
readers = defaultdict(set)
code_sites = {}
code_objects = []
external = set()
state = sys.monitoring
slot = 4
state.use_tool_id(slot, "sqlglot-entry-coverage")


@lru_cache(None)
def pure_factory(fn):
    if not isinstance(fn, types.FunctionType):
        return False
    try:
        body = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0].body
    except (TypeError, OSError, SyntaxError, AttributeError):
        return False
    body = [
        stmt
        for stmt in body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    if (
        len(body) == 1
        and isinstance(body[0], ast.Return)
        and isinstance(body[0].value, ast.Lambda)
    ):
        args = body[0].value.args
        return not args.defaults and not any(args.kw_defaults)
    if (
        len(body) == 2
        and isinstance(body[0], ast.FunctionDef)
        and isinstance(body[1], ast.Return)
    ):
        nested = body[0]
        return (
            not nested.decorator_list
            and not nested.args.defaults
            and not any(nested.args.kw_defaults)
            and isinstance(body[1].value, ast.Name)
            and body[1].value.id == nested.name
        )
    return False


def register(result, path, region, factory=None):
    if not isinstance(result, types.FunctionType) or (
        factory is not None and not pure_factory(factory)
    ):
        return result
    # Give each closure its own code object, with identical instructions,
    # constants, line numbers, signature, and closure cells. Inherited copies
    # of a dispatch table still point to the registered function object.
    code = result.__code__.replace()
    result.__code__ = code
    code_objects.append(code)
    code_sites[id(code)] = (path, region)
    readers[path, region]
    state.set_local_events(slot, code, state.events.PY_START)
    return result


def called(code, offset):
    site = code_sites.get(id(code))
    if site is not None:
        readers[site].add(current)


class Tag(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_FunctionDef(self, node):
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        for field in node.body:
            if not isinstance(field, (ast.Assign, ast.AnnAssign)):
                continue
            value = field.value
            if not isinstance(value, ast.Dict):
                continue
            targets = field.targets if isinstance(field, ast.Assign) else [field.target]
            if not any(isinstance(t, ast.Name) and t.id.isupper() for t in targets):
                continue
            for i, (key, item) in enumerate(zip(value.keys, value.values)):
                if key is None:
                    continue
                factory = None
                if isinstance(item, ast.Call) and isinstance(item.func, ast.Name):
                    factory = ast.Name(id=item.func.id, ctx=ast.Load())
                elif not isinstance(item, ast.Lambda):
                    continue
                region = f"{key.lineno}:{key.col_offset}:{item.end_lineno}:{item.end_col_offset}"
                value.values[i] = ast.copy_location(
                    ast.Call(
                        func=ast.Name(id="_br_dispatch_register", ctx=ast.Load()),
                        args=[
                            item,
                            ast.Constant(self.path),
                            ast.Constant(region),
                            factory or ast.Constant(None),
                        ],
                        keywords=[],
                    ),
                    item,
                )
        self.generic_visit(node)
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        tree = Tag(Path(self.path).relative_to(root).as_posix()).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(
            ("sqlglot.parser", "sqlglot.generator", "sqlglot.dialects.")
        ):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            spec.loader = Loader(fullname, spec.origin)
        return spec


class Recorder:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global current
        current = item.nodeid
        try:
            yield
        finally:
            current = ""

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global current
        old = current
        current = "@" + item.nodeid.rsplit("::", 1)[0]
        try:
            yield
        finally:
            current = old


def audit(event, args):
    if event == "subprocess.Popen" and current:
        external.add(current)


if __name__ == "__main__":
    for source, digest in base["source_hashes"].items():
        if hashlib.sha256((root / source).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Pinned checkout mismatch at {source}")
    # In the delivered repository, build_map.py is one level above tools/.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    from build_map import InlinePool

    concurrent.futures.ProcessPoolExecutor = InlinePool
    builtins._br_dispatch_register = register
    sys.meta_path.insert(0, Finder())
    sys.addaudithook(audit)
    state.register_callback(slot, state.events.PY_START, called)
    status = pytest.main(
        ["tests", "-q", "-p", "no:cacheprovider", "--tb=short"], plugins=[Recorder()]
    )
    if status:
        raise SystemExit(status)
    tests = base["tests"]
    index = {name: i for i, name in enumerate(tests)}

    def expand(contexts):
        selected = set()
        for item in contexts:
            if item.startswith("@"):
                selected.update(
                    i for i, n in enumerate(tests) if n.startswith(item[1:] + "::")
                )
            elif item in index:
                selected.add(index[item])
        return sorted(selected)

    rows = {}
    for (path, region), names in sorted(readers.items()):
        if "" in names:
            continue  # Import-time dispatch before a test may seed caches for later tests.
        rows.setdefault(path, {})[region] = expand(names)
    out.write_text(
        json.dumps(
            {
                "commit": PIN,
                "tests": tests,
                "regions": rows,
                "external": expand(external),
            },
            separators=(",", ":"),
        )
    )
    print(
        f"{len(readers)} dispatch entries; {len(expand(external))} external-process tests"
    )
