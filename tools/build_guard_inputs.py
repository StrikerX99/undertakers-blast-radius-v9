"""Collect plain string field truth values at simple type guards in the pinned optimizer."""

import ast
import atexit
import builtins
from collections import defaultdict
import hashlib
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def match(expr):
    if not (
        isinstance(expr, ast.Call)
        and isinstance(expr.func, ast.Name)
        and expr.func.id == "isinstance"
        and len(expr.args) == 2
        and not expr.keywords
    ):
        return False
    obj, typ = expr.args
    return (
        isinstance(obj, ast.Name)
        and isinstance(typ, ast.Attribute)
        and isinstance(typ.value, ast.Name)
        and typ.value.id == "exp"
    )


def install():
    import profile_pool

    profile_pool.install()
    root = Path(os.environ["BR_GUARD_ROOT"])
    state = Path(os.environ["BR_GUARD_STATE"])
    paths = json.loads((state / "sources.json").read_bytes())
    (state / (str(os.getpid()) + ".start")).touch()
    rows = defaultdict(lambda: defaultdict(set))
    active = 0

    def observe(p, site, result, node):
        context = profile_pool.context()
        test, _, phase = context.rpartition(" (")
        if active or not test:
            test = "*"
        elif phase != "call)":
            test = test.partition("::")[0] + "::*"
        key = "--"
        if result:
            typ = type(node)
            from sqlglot.expressions.core import Expression

            definition = sys.modules["sqlglot.expressions.core"].__file__

            def pinned(fn, qualname):
                code = getattr(fn, "__code__", None)
                return (
                    code is not None
                    and code.co_filename == definition
                    and code.co_qualname == qualname
                )

            regular = typ.__getattribute__ is object.__getattribute__
            can_read_args = regular and getattr(typ, "args", None) is Expression.args
            this_property = getattr(typ, "this", None)
            name_property = getattr(typ, "name", None)
            this = (
                can_read_args
                and isinstance(this_property, property)
                and pinned(this_property.fget, "Expression.this")
            )
            name = (
                can_read_args
                and isinstance(name_property, property)
                and pinned(name_property.fget, "Expression.name")
                and pinned(getattr(typ, "text", None), "Expression.text")
                and "text" not in getattr(node, "__dict__", {})
            )
            # Read stored args directly. Do not execute arbitrary properties.
            arg = object.__getattribute__(node, "args") if (this or name) else None
            if type(arg) is dict:
                value = arg.get("this")
                if type(value) is str:
                    truth = "1" if value else "0"
                    key = (truth if this else "?") + (truth if name else "?")
                else:
                    key = "??"
            else:
                key = "??"
        rows[(p, site)][key].add(test)
        return result

    class Patch(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path

        def visit_If(self, node):
            self.generic_visit(node)
            expr = node.test
            if not match(expr):
                return node
            site = f"{expr.lineno}:{expr.col_offset}"
            node.test = ast.copy_location(
                ast.Call(
                    ast.Name("_br_guard_input", ast.Load()),
                    [
                        ast.Constant(self.path),
                        ast.Constant(site),
                        expr,
                        ast.Name(expr.args[0].id, ast.Load()),
                    ],
                    [],
                ),
                expr,
            )
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, name):
            path = Path(self.path).relative_to(root).as_posix()
            if path not in paths:
                return super().get_code(name)
            tree = Patch(path).visit(ast.parse(Path(self.path).read_bytes(), self.path))
            return compile(ast.fix_missing_locations(tree), self.path, "exec")

        def exec_module(self, module):
            nonlocal active
            active += 1
            try:
                return super().exec_module(module)
            finally:
                active -= 1

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name != "sqlglot" and not name.startswith(("sqlglot.", "tests.")):
                return
            spec = importlib.machinery.PathFinder.find_spec(name, path)
            if (
                spec
                and spec.origin
                and spec.origin.endswith(".py")
                and Path(spec.origin).is_relative_to(root)
            ):
                spec.loader = Loader(name, spec.origin)
            return spec

    builtins._br_guard_input = observe
    sys.meta_path.insert(0, Finder())

    @atexit.register
    def save():
        receipt = [
            [p, k, {v: sorted(ids) for v, ids in e.items()}]
            for (p, k), e in rows.items()
        ]
        (state / (str(os.getpid()) + ".json")).write_text(json.dumps(receipt))


def build(root, data, tests, dest):
    root = Path(root).resolve()
    data = json.loads(Path(data).read_bytes())
    tests = Path(tests).resolve()
    dest = Path(dest).resolve()
    sources = {}
    for path, sha in data["source_hashes"].items():
        if not path.startswith("sqlglot/optimizer/"):
            continue
        raw = (root / path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise ValueError(path)
        if any(
            isinstance(n, ast.If) and match(n.test) for n in ast.walk(ast.parse(raw))
        ):
            sources[path] = sha
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix="guard-") as temp:
        st = Path(temp)
        (st / "sources.json").write_text(json.dumps(sources))
        (st / "sitecustomize.py").write_text(
            "from build_guard_inputs import install\ninstall()\n"
        )
        (st / "br_oracle.py").write_text("""
from pathlib import Path
import json, os
passed=set()
skipped=set()
def pytest_runtest_logreport(report):
    if report.skipped: skipped.add(report.nodeid)
    elif report.when=='call' and report.passed: passed.add(report.nodeid)
import pytest
@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    lockfile=os.environ.get("BR_PROFILE_STRESS_LOCK")
    if not lockfile or item.nodeid != "tests/test_parser.py::TestParser::test_max_nodes":
        return (yield)
    import fcntl
    with open(lockfile,"a+") as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        return (yield)
def pytest_sessionfinish(session,exitstatus):
    c=json.loads(Path(os.environ['BR_GUARD_TESTS']).read_bytes())
    if exitstatus==0 and passed=={r['id'] for r in c['tests']} and skipped==set(c['skipped']):
        (Path(os.environ['BR_GUARD_STATE'])/'ok').touch()
""")
        env = dict(
            os.environ,
            BR_GUARD_ROOT=str(root),
            BR_GUARD_TESTS=str(tests),
            BR_GUARD_STATE=str(st),
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHON_CPU_COUNT="2",
            SKIP_INTEGRATION="0",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONPATH=os.pathsep.join(
                [str(st), str(Path(__file__).resolve().parent), str(root)]
            ),
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
        env.pop("BR_POOL_OWNER", None)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-q",
                "--tb=short",
                "-p",
                "br_oracle",
                "-p",
                "no:cacheprovider",
            ],
            cwd=root,
            env=env,
            check=True,
        )
        starts = {p.stem for p in st.glob("*.start")}
        done = {p.stem for p in st.glob("*.json") if p.stem.isdigit()}
        if starts != done or len(done) < 5 or not (st / "ok").is_file():
            raise ValueError("Incomplete baseline")
        rows = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
        for pid in done:
            for path, site, obs in json.loads((st / (pid + ".json")).read_bytes()):
                for v, ids in obs.items():
                    rows[path][site][v].update(ids)
        ids = {name: i for i, name in enumerate(data["tests"])}
        out = {
            p: {
                s: {v: [ids.get(n, n) for n in sorted(ns)] for v, ns in obs.items()}
                for s, obs in sites.items()
            }
            for p, sites in rows.items()
        }
        dest.write_text(
            json.dumps(
                {"commit": data["commit"], "sources": sources, "rows": out},
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        print(
            "Profile complete",
            len(done),
            "processes",
            sum(map(len, rows.values())),
            "guard sites",
        )


if __name__ == "__main__":
    build(*sys.argv[1:])
