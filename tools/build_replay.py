"""Collect primitive inputs and result fingerprints for baseline nested builders."""

import ast, builtins, contextvars, importlib.abc, importlib.machinery, json, os, sys
from pathlib import Path

if not Path(__file__).with_name("forms.py").is_file():
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from forms import normalize, span, LITERALS, FACTORIES

from concurrent import futures
import pickle
import tempfile
import pytest
from fingerprint import fingerprint

root = None
source_files = set()
worker_logs = None
trusted_pool = False

active = ""
blocked = set()
excluded = set()
record = {}
sites = {}
nodes = set()
stack = contextvars.ContextVar("stack", default=None)


def scan(mapping):
    for f in (root / "sqlglot").rglob("*.py"):
        path = f.relative_to(root).as_posix()
        tree = ast.parse(f.read_text())
        rows = mapping["lines"].get(path, {})
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            try:
                form, leaves = normalize(call)
            except ValueError:
                continue
            key = (path, span(call))
            sites[key] = (form, len(leaves))
            record[key] = {}
            for line in range(call.lineno, call.end_lineno + 1):
                if str(line) in rows:
                    nodes.update(
                        mapping["tests"][n] for n in mapping["sets"][rows[str(line)]]
                    )


def leaf(i, value):
    state = stack.get()
    state[i] = value
    return value


def collect(path, location, size, invoke):
    if not active:
        blocked.add((path, location))
    state = [None] * size
    token = stack.set(state)
    try:
        result = invoke()
    except BaseException:
        if active:
            record[(path, location)][active] = None
        raise
    finally:
        stack.reset(token)
    if active:
        data = record[(path, location)]
        if data.get(active, 1) is not None:
            try:
                if not pristine():
                    raise ValueError
                if any(
                    type(v) not in LITERALS or isinstance(v, str) and len(v) > 128
                    for v in state
                ):
                    raise ValueError
                digest = fingerprint(result)
                pattern = json.dumps([state, digest], separators=(",", ":"))
                variants = data.setdefault(active, set())
                variants.add(pattern)
                if len(variants) > 40:
                    raise ValueError
            except (TypeError, ValueError, RecursionError):
                data[active] = None
    return result


builtins._br4_leaf = leaf
builtins._br4_collect = collect


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Call(self, n):
        id = span(n)
        if (self.path, id) not in sites:
            return self.generic_visit(n)
        form, leaves = normalize(n)

        class Param(ast.NodeTransformer):
            def visit_Name(self, x):
                if x.id.startswith("_v") and x.id[2:].isdigit():
                    i = int(x.id[2:])
                    return ast.Call(
                        func=ast.Name(id="_br4_leaf", ctx=ast.Load()),
                        args=[ast.Constant(i), leaves[i]],
                        keywords=[],
                    )
                return x

        b = Param().visit(ast.parse(form, mode="eval").body)
        new = ast.Call(
            func=ast.Name(id="_br4_collect", ctx=ast.Load()),
            args=[
                ast.Constant(self.path),
                ast.Constant(id),
                ast.Constant(len(leaves)),
                ast.Lambda(
                    args=ast.arguments(
                        posonlyargs=[],
                        args=[],
                        kwonlyargs=[],
                        kw_defaults=[],
                        defaults=[],
                    ),
                    body=b,
                ),
            ],
            keywords=[],
        )
        return ast.copy_location(ast.fix_missing_locations(new), n)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        path = Path(self.path).relative_to(root).as_posix()
        tree = ast.parse(self.get_data(self.path), self.path)
        tree = Instrument(path).visit(tree)
        return compile(ast.fix_missing_locations(tree), self.path, "exec")


class Find(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "sqlglot" or name.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if (
                spec
                and spec.origin
                and spec.origin.endswith(".py")
                and Path(spec.origin).relative_to(root).as_posix() in source_files
            ):
                spec.loader = Loader(name, spec.origin)
            return spec


# Workers use their real spawn context and the normal pickle boundary. Their
# instrumented calls are associated with the test that submitted each job.
def setup(project, locations, logs):
    global root, sites, source_files, worker_logs
    root, sites, worker_logs = project, locations, logs
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    record.update({key: {} for key in sites})
    source_files = {path for path, key in sites}
    sys.meta_path.insert(0, Find())
    snapshot_dependencies()
    sys.addaudithook(spawned)


def worker_initialize(project, locations, logs, initializer_bytes):
    setup(project, locations, logs)
    initializer, initargs = pickle.loads(initializer_bytes)
    if initializer:
        initializer(*initargs)


def worker_call(test, job):
    global active
    active = test
    try:
        func, args, kwargs = pickle.loads(job)
        return func(*args, **kwargs)
    finally:
        active = ""
        report = worker_logs / f"{os.getpid()}.pickle"
        temp = report.with_suffix(".tmp")
        temp.write_bytes(pickle.dumps((record, blocked, excluded)))
        temp.replace(report)


class ObservedPool(futures.ProcessPoolExecutor):
    def __init__(
        self,
        max_workers=None,
        *,
        mp_context=None,
        initializer=None,
        initargs=(),
        **kwargs,
    ):
        super().__init__(
            max_workers=min(2, max_workers or 2),
            mp_context=mp_context,
            initializer=worker_initialize,
            initargs=(root, sites, worker_logs, pickle.dumps((initializer, initargs))),
            **kwargs,
        )

    def submit(self, func, *args, **kwargs):
        global trusted_pool
        trusted_pool = True
        try:
            return super().submit(
                worker_call, active, pickle.dumps((func, args, kwargs))
            )
        finally:
            trusted_pool = False


def spawned(event, args):
    if event in (
        "subprocess.Popen",
        "os.posix_spawn",
        "os.fork",
        "os.forkpty",
        "os.system",
    ):
        if active and not trusted_pool:
            excluded.add(active)


# A unit test may replace an expression helper or inspection hook. Preserve
# those tests if any builder dependency has changed while a call is observed.
dependencies = []


def snapshot_dependencies():
    from sqlglot import exp
    from sqlglot.expressions import core, builders

    for module in (exp, core, builders):
        for name in FACTORIES | {"maybe_parse", "maybe_copy", "SAFE_IDENTIFIER_RE"}:
            if hasattr(module, name):
                dependencies.append((module, name, getattr(module, name)))
    for cls in [
        getattr(exp, k) for k in FACTORIES if isinstance(getattr(exp, k), type)
    ]:
        for name in (
            "__init__",
            "__new__",
            "__getattribute__",
            "__setattr__",
            "__deepcopy__",
            "copy",
            "set",
            "append",
            "_set_parent",
            "arg_types",
        ):
            if hasattr(cls, name):
                dependencies.append((cls, name, getattr(cls, name)))


def pristine():
    return all(
        getattr(owner, name, None) is value for owner, name, value in dependencies
    )


class Hook:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_call(self, item):
        global active
        active = item.nodeid
        yield
        active = ""


def merge(other, blocked_sites, excluded_tests):
    blocked.update(blocked_sites)
    excluded.update(excluded_tests)
    for key, by_test in other.items():
        for name, variants in by_test.items():
            observed = record[key].get(name, set())
            if observed is None or variants is None or len(observed | variants) > 40:
                record[key][name] = None
            else:
                record[key][name] = observed | variants


def main():
    global root
    root = Path(sys.argv[1]).resolve()
    mapping = json.loads(Path(sys.argv[2]).read_bytes())
    output = Path(sys.argv[3]).resolve()
    scan(mapping)
    print(
        "Builder sites", len(sites), "candidate baseline tests", len(nodes), flush=True
    )
    with tempfile.TemporaryDirectory(prefix="br-builder-record-") as work:
        logs = Path(work)
        setup(root, sites, logs)
        futures.ProcessPoolExecutor = ObservedPool
        result = pytest.main(
            [*sorted(nodes), "-q", "-p", "no:cacheprovider", "--tb=short"],
            plugins=[Hook()],
        )
        if result:
            raise SystemExit(result)
        for path in logs.glob("*.pickle"):
            merge(*pickle.loads(path.read_bytes()))
    payload = {}
    for (path, key), by_test in record.items():
        if (path, key) in blocked:
            continue
        variants = {
            name: sorted(values)
            for name, values in by_test.items()
            if values and name not in excluded
        }
        if variants:
            payload.setdefault(path, {})[key] = [sites[(path, key)][0], variants]
    output.write_text(json.dumps(payload, separators=(",", ":")))


if __name__ == "__main__":
    main()
