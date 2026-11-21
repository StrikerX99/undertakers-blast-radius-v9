"""Build a baseline-only inventory of resolved scalar attributes in dialect classes."""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from dialect_observer import classes, declaration


def catalog(root, hashes):
    sites, sources, keys = {}, {}, {}
    for f in sorted((root / "sqlglot/dialects").glob("*.py")):
        path = f.relative_to(root).as_posix()
        if path not in hashes:
            continue
        raw = f.read_bytes()
        if hashlib.sha256(raw).hexdigest() != hashes[path]:
            raise ValueError("Unrecognized source")
        tree = ast.parse(raw.decode())
        base = path == "sqlglot/dialects/dialect.py"
        if base:
            meta = next(
                n
                for n in tree.body
                if isinstance(n, ast.ClassDef) and n.name == "_Dialect"
            )
            method = next(
                n
                for n in meta.body
                if isinstance(n, ast.FunctionDef) and n.name == "__new__"
            )
            parents = {c: p for p in ast.walk(method) for c in ast.iter_child_nodes(p)}
            for n in ast.walk(method):
                if isinstance(n, ast.Attribute) and n.attr == "__dict__":
                    outer = parents[n]
                    call = parents[outer]
                    if (
                        isinstance(outer, ast.Attribute)
                        and outer.attr == "get"
                        and isinstance(call, ast.Call)
                        and call.args
                        and isinstance(call.args[0], ast.Constant)
                        and type(call.args[0].value) is str
                    ):
                        keys.setdefault(str(n.lineno), []).append(call.args[0].value)
                    else:
                        raise ValueError(
                            "Unexpected access to dialect metaclass namespace"
                        )
        local = {}
        for cls in classes(tree, base):
            bare = {
                n.id
                for n in ast.walk(cls)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            }
            for stmt in cls.body:
                name = declaration(stmt)
                if name and name not in bare:
                    local[str(stmt.lineno)] = [cls.name, name]
        if local or base:
            sites[path] = local
            sources[path] = hashes[path]
    return dict(sites=sites, sources=sources, read_keys=keys)


def run(root, mapping, tests, output):
    root, mapping, tests, output = [
        Path(p).resolve() for p in (root, mapping, tests, output)
    ]
    m = json.loads(mapping.read_bytes())
    ref = catalog(root, m["source_hashes"])
    with tempfile.TemporaryDirectory(prefix="dialect-reads-") as t:
        state = Path(t)
        (state / "catalog.json").write_text(json.dumps(ref))
        (state / "sitecustomize.py").write_text(
            "from dialect_observer import install\ninstall()\n"
        )
        (state / "expected.json").write_bytes(tests.read_bytes())
        (state / "br_oracle.py").write_text("""
import json,os
from pathlib import Path
passed=set()
skipped=set()
def pytest_runtest_logreport(report):
    if report.skipped: skipped.add(report.nodeid)
    elif report.when=='call' and report.passed: passed.add(report.nodeid)
def pytest_sessionfinish(session,exitstatus):
    t=Path(os.environ['BR_DIALECT_STATE'])
    c=json.loads((t/'expected.json').read_bytes())
    if exitstatus==0 and passed=={r['id'] for r in c['tests']} and skipped==set(c['skipped']):
        (t/'suite-ok').touch()
""")
        env = dict(
            os.environ,
            BR_DIALECT_ROOT=str(root),
            BR_DIALECT_STATE=t,
            SKIP_INTEGRATION="0",
            PYTHONPATH=os.pathsep.join(
                [t, str(Path(__file__).parent.resolve()), str(root)]
            ),
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
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
        starts = {f.stem for f in state.glob("*.start")}
        done = {f.stem for f in state.glob("*.json") if f.stem.isdigit()}
        if starts != done or len(starts) < 5 or not (state / "suite-ok").exists():
            raise ValueError(
                ("Incomplete process and test observations", starts - done)
            )
        initialized, reads = set(), set()
        for pid in done:
            a, b = json.loads((state / (pid + ".json")).read_bytes())
            initialized.update(a)
            reads.update(b)
        unused = {}
        for path, rows in ref["sites"].items():
            for line, cls_name in rows.items():
                if f"{path}:{line}" in initialized - reads:
                    unused.setdefault(path, {})[line] = cls_name
        result = dict(
            commit=m["commit"],
            sources={p: ref["sources"][p] for p in unused},
            unused=unused,
        )
        output.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        )
        print(
            "Observed",
            len(done),
            "processes,",
            len(initialized),
            "settings, remaining",
            sum(map(len, unused.values())),
        )


if __name__ == "__main__":
    run(*sys.argv[1:])
