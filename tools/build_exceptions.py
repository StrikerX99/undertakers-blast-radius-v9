"""Collect actual exception types thrown out of eligible protected suites.

Run this with the locked development Python interpreter, from the submission
checkout, passing SQLGlot source, coverage-map.json and destination JSON.
"""

import ast
import builtins
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

if not Path(__file__).with_name("exception_impact.py").exists():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from exception_impact import names, BUILTINS

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
current = ""
memo = set()
import_depth = 0
writer = None


def emit(row):
    global writer
    if writer is None:
        dst = Path(os.environ["BR_EXCEPTION_LOG"]) / (str(os.getpid()) + ".jsonl")
        writer = dst.open("a", buffering=1)
    writer.write(json.dumps(row, separators=(",", ":")) + "\n")


def note(site, exc):
    frame = sys._getframe(1)
    scope = (
        ""
        if import_depth or threading.current_thread() is not threading.main_thread()
        else current
    )
    mro = type.__getattribute__(type(exc), "__mro__")
    lineage = "|".join(
        name for name, cls in BUILTINS.items() if any(cls is c for c in mro)
    )
    if any(
        frame.f_globals.get(k, frame.f_builtins.get(k)) is not cls
        for k, cls in BUILTINS.items()
    ):
        lineage = "*"
    row = (site, lineage, scope)
    if row not in memo:
        memo.add(row)
        emit(["hit", *row])


class Instrument(ast.NodeTransformer):
    def __init__(self, path):
        self.path = path

    def visit_Try(self, node):
        self.generic_visit(node)
        if len(node.handlers) != 1 or names(node.handlers[0].type) is None:
            return node
        handler = node.handlers[0]
        key = f"{self.path}@{handler.lineno}:{handler.col_offset}"
        # Preserve the original exception matching and traceback by re-raising.
        watch = ast.ExceptHandler(
            ast.Name("_br_exception_base", ast.Load()),
            "_br_caught_probe",
            [
                ast.Expr(
                    ast.Call(
                        ast.Name("_br_exception_note", ast.Load()),
                        [ast.Constant(key), ast.Name("_br_caught_probe", ast.Load())],
                        [],
                    )
                ),
                ast.Raise(None, None),
            ],
        )
        node.body = [ast.copy_location(ast.Try(node.body, [watch], [], []), node)]
        return node


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, name):
        file = Path(self.path)
        rel = file.relative_to(Path(os.environ["BR_EXCEPTION_ROOT"])).as_posix()
        tree = Instrument(rel).visit(ast.parse(file.read_bytes()))
        return compile(ast.fix_missing_locations(tree), self.path, "exec")

    def exec_module(self, module):
        global import_depth
        import_depth += 1
        try:
            return super().exec_module(module)
        finally:
            import_depth -= 1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name != "sqlglot" and not name.startswith("sqlglot."):
            return
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.endswith(".py"):
            root = Path(os.environ["BR_EXCEPTION_ROOT"])
            if not Path(spec.origin).is_relative_to(root):
                raise RuntimeError("unexpected SQLGlot import " + spec.origin)
            spec.loader = Loader(name, spec.origin)
        return spec


def run_owned(tag, job, args, kwargs):
    global current
    previous = current
    current = tag
    try:
        return job(*args, **kwargs)
    finally:
        current = previous


def install(child=False):
    global current
    if any(k == "sqlglot" or k.startswith("sqlglot.") for k in sys.modules):
        raise RuntimeError("Install the collector before importing SQLGlot")
    builtins._br_exception_base = BaseException
    builtins._br_exception_note = note
    builtins._br_exception_run_owned = run_owned
    sys.meta_path.insert(0, Finder())
    if child:
        emit(["child", os.environ.get("BR_EXCEPTION_CURRENT", "")])
        # A child can serve many tests. Only per-submit ownership is precise.
        current = ""

        def audit(event, args):
            if event in {
                "subprocess.Popen",
                "os.posix_spawn",
                "os.fork",
                "os.forkpty",
                "os.exec",
                "os.system",
            }:
                emit(["opaque", current])

        sys.addaudithook(audit)


def bound_names(tree):
    bound = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            bound.add(n.id)
        if isinstance(n, ast.arg):
            bound.add(n.arg)
        if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            bound.add(n.name)
        if isinstance(n, ast.alias):
            if n.name == "*":
                return set(BUILTINS)
            bound.add(n.asname or n.name.split(".")[0])
        if isinstance(n, ast.ExceptHandler):
            bound.add(n.name)
        if isinstance(n, (ast.MatchAs, ast.MatchStar)):
            bound.add(n.name)
        if isinstance(n, ast.MatchMapping):
            bound.add(n.rest)
    return bound & BUILTINS.keys()


def main():
    import concurrent.futures
    import multiprocessing.util
    import fcntl
    import subprocess
    import pytest

    root, basefile, out = (Path(s).resolve() for s in sys.argv[1:])
    baseline = json.loads(basefile.read_bytes())
    if baseline["commit"] != PIN or baseline["baseline_failures"]:
        raise ValueError("Unsupported baseline")
    sites, hashes, bindings = {}, {}, {}
    for rel, digest in baseline["source_hashes"].items():
        src = (root / rel).read_bytes()
        if hashlib.sha256(src).hexdigest() != digest:
            raise ValueError("source mismatch " + rel)
        if not rel.startswith("sqlglot/") or not rel.endswith(".py"):
            continue
        tree = ast.parse(src)
        available = {
            f"{n.handlers[0].lineno}:{n.handlers[0].col_offset}": {}
            for n in ast.walk(tree)
            if isinstance(n, ast.Try)
            and len(n.handlers) == 1
            and names(n.handlers[0].type) is not None
        }
        if available:
            if b"_br_caught_probe" in src or b"_br_exception_" in src:
                raise ValueError("Instrumented names already in use")
            sites[rel] = available
            hashes[rel] = digest
            bindings[rel] = sorted(bound_names(tree))
    processed = set()
    child_pids = []
    opaque = set()
    waiting = []
    old_popen = subprocess.Popen
    old_spawnv = multiprocessing.util.spawnv_passfds

    def tracked_spawnv(executable, args, passfds):
        waiting.append(True)
        try:
            pid = old_spawnv(executable, args, passfds)
        finally:
            waiting.pop()
        child_pids.append((pid, current))
        return pid

    def tracked_popen(*args, **kwargs):
        waiting.append(True)
        try:
            proc = old_popen(*args, **kwargs)
        finally:
            waiting.pop()
        child_pids.append((proc.pid, current))
        return proc

    def track(event, args):
        if (
            event
            in {
                "os.posix_spawn",
                "os.fork",
                "os.forkpty",
                "os.exec",
                "os.system",
                "subprocess.Popen",
            }
            and not waiting
        ):
            opaque.add(current)

    class Plugin:
        def pytest_runtest_logreport(self, report):
            if report.skipped:
                opaque.add(report.nodeid)

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item, nextitem):
            global current
            node = item.nodeid
            processed.add(node)
            current = node
            os.environ["BR_EXCEPTION_CURRENT"] = node
            guard = None
            if node.endswith("TestParser::test_max_nodes") and os.environ.get(
                "SQLGLOT_STRESS_LOCK"
            ):
                guard = open(os.environ["SQLGLOT_STRESS_LOCK"], "a")
                fcntl.flock(guard, fcntl.LOCK_EX)
            try:
                return (yield)
            finally:
                if guard:
                    guard.close()
                current = ""
                os.environ["BR_EXCEPTION_CURRENT"] = ""

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_setup(self, item):
            return (yield from self.fixture_scope(item))

        @pytest.hookimpl(wrapper=True, tryfirst=True)
        def pytest_runtest_teardown(self, item):
            return (yield from self.fixture_scope(item))

        def fixture_scope(self, item):
            global current
            prev = current
            current = "@" + item.nodeid.split("::")[0]
            os.environ["BR_EXCEPTION_CURRENT"] = current
            try:
                return (yield)
            finally:
                current = prev
                os.environ["BR_EXCEPTION_CURRENT"] = prev

    with tempfile.TemporaryDirectory(prefix="br-exceptions-") as temp:
        logs = Path(temp)
        os.environ.update(
            BR_EXCEPTION_ROOT=str(root),
            BR_EXCEPTION_LOG=str(logs),
            SKIP_INTEGRATION="0",
        )
        path = Path(__file__).resolve()
        hook = (
            "import os,runpy,sys\n"
            f"sys.path[:0] = [{str(root)!r},{str(path.parent)!r},{str(path.parent.parent)!r}]\n"
            f"runpy.run_path({str(path)!r})['install'](True)\n"
        )
        (logs / "sitecustomize.py").write_text(hook)
        # A top-level importable adapter remains picklable by a real spawn pool.
        (logs / "br_exception_job.py").write_text(
            "import builtins\n"
            "def run(tag, job, args, kwargs):\n"
            "    return builtins._br_exception_run_owned(tag, job, args, kwargs)\n"
        )
        sys.path.insert(0, str(logs))
        import br_exception_job

        original_submit = concurrent.futures.ProcessPoolExecutor.submit

        def submit(pool, function, /, *args, **kwargs):
            return original_submit(
                pool, br_exception_job.run, current, function, args, kwargs
            )

        concurrent.futures.ProcessPoolExecutor.submit = submit
        os.environ["PYTHONPATH"] = str(logs) + os.pathsep + str(root)
        sys.path.insert(0, str(root))
        os.chdir(root)
        install()
        subprocess.Popen = tracked_popen
        multiprocessing.util.spawnv_passfds = tracked_spawnv
        sys.addaudithook(track)
        status = pytest.main(
            ["tests", "-q", "--tb=short", "-p", "no:cacheprovider"], plugins=[Plugin()]
        )
        if status or processed != set(baseline["tests"]):
            raise RuntimeError("passing complete suite is required")
        tests = baseline["tests"]
        ids = {name: i for i, name in enumerate(tests)}

        def expand(tag):
            if tag.startswith("@"):
                return [i for n, i in ids.items() if n.startswith(tag[1:] + "::")]
            return [ids[tag]] if tag in ids else [-1]

        handshakes = set()
        for f in logs.glob("*.jsonl"):
            for line in f.read_text().splitlines():
                record = json.loads(line)
                if record[0] == "child":
                    handshakes.add((int(f.stem), record[1]))
                elif record[0] == "opaque":
                    opaque.add(record[1])
                elif record[0] == "hit":
                    _, site, lineage, scope = record
                    p, k = site.rsplit("@", 1)
                    sites[p][k].setdefault(lineage, set()).update(expand(scope))
        for key in child_pids:
            if key not in handshakes:
                opaque.add(key[1])
        if "" in opaque:
            raise ValueError("untracked process outside test attribution")
        unknown = sorted({i for n in opaque for i in expand(n)})
        for ps in sites.values():
            for ks in ps.values():
                for key, values in ks.items():
                    ks[key] = sorted(values)
        data = dict(
            commit=PIN,
            tests=tests,
            sources=hashes,
            sites=sites,
            bindings=bindings,
            opaque=unknown,
        )
        out.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")
        print(
            "forked processes",
            len(child_pids),
            "opaque IDs",
            unknown,
            "captured sites",
            sum(map(len, sites.values())),
        )


if __name__ == "__main__":
    main()
