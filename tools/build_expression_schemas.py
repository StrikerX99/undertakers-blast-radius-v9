"""Build a passing-suite dependency profile for literal expression-class declarations."""

import ast
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.append(str(Path(__file__).resolve().parent.parent))
import expression_schema

ORACLE = """
import os, json, pytest
from pathlib import Path
passed=set()
skipped=set()
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_protocol(item):
    import fcntl
    name=item.nodeid
    if name.endswith('::TestParser::test_max_nodes') and os.environ.get('BR_PROFILE_STRESS_LOCK'):
        with open(os.environ['BR_PROFILE_STRESS_LOCK'],'a') as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            return (yield)
    return (yield)
def pytest_runtest_logreport(report):
    if report.skipped: skipped.add(report.nodeid)
    elif report.when=='call' and report.passed: passed.add(report.nodeid)
def pytest_sessionfinish(session,exitstatus):
    st=Path(os.environ['BR_EXPR_STATE'])
    c=json.loads((st/'tests.json').read_bytes())
    if exitstatus==0 and passed=={r['id'] for r in c['tests']} and skipped==set(c['skipped']):
        (st/'suite-ok').touch()
"""


def build(root, mapping, tests, dest):
    root, mapping, tests, dest = (
        Path(x).resolve() for x in (root, mapping, tests, dest)
    )
    m = json.loads(mapping.read_bytes())
    pinned = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    actual = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    dirty = subprocess.check_output(
        ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"], text=True
    )
    if m["commit"] != pinned or actual != pinned or dirty:
        raise ValueError("Expected a clean tracked tree at the pinned revision")
    hashes = m["source_hashes"]
    names = {}
    targets = {}
    paths = {}
    # Exclude names used as parents anywhere in the pinned source. A
    # dynamically created descendant also invalidates its measured parent.
    used_bases = set()
    for f in sorted((root / "sqlglot").rglob("*.py")):
        for cls in ast.walk(ast.parse(f.read_bytes())):
            if isinstance(cls, ast.ClassDef):
                for base in cls.bases:
                    if isinstance(base, ast.Name):
                        used_bases.add(base.id)
                    elif isinstance(base, ast.Attribute):
                        used_bases.add(base.attr)
    for f in sorted((root / "sqlglot/expressions").glob("*.py")):
        path = f.relative_to(root).as_posix()
        digest = hashlib.sha256(f.read_bytes()).hexdigest()
        if hashes.get(path) != digest:
            raise ValueError(path)
        eligible = {
            n.name: n
            for n in ast.parse(f.read_bytes()).body
            if expression_schema.fields(n) is not None and n.name not in used_bases
        }
        module = path[:-3].replace("/", ".")
        targets[module] = sorted(eligible)
        paths[path] = digest
        names.update({module + "." + name: (path, name) for name in eligible})
    with tempfile.TemporaryDirectory(prefix="expr-shapes-", dir=dest.parent) as tmp:
        st = Path(tmp)
        (st / "sitecustomize.py").write_text(
            "from expression_schema_observer import install\ninstall()\n"
        )
        (st / "targets.json").write_text(json.dumps(targets))
        (st / "br_expr_oracle.py").write_text(ORACLE)
        (st / "tests.json").write_bytes(tests.read_bytes())
        tools = str(Path(__file__).resolve().parent)
        env = dict(
            os.environ,
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            PYTHONPATH=os.pathsep.join(
                [
                    tmp,
                    tools,
                    str(Path(expression_schema.__file__).resolve().parent),
                    str(root),
                ]
            ),
            BR_EXPR_STATE=tmp,
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHON_CPU_COUNT="2",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-qq",
                "--tb=short",
                "-p",
                "br_expr_oracle",
                "-p",
                "no:cacheprovider",
            ],
            cwd=root,
            env=env,
            check=True,
        )
        start = {p.stem for p in st.glob("*.start")}
        finish = {p.stem for p in st.glob("*.done")}
        if start != finish or len(start) < 5 or not (st / "suite-ok").exists():
            raise RuntimeError(["Incomplete native suite", len(start), start - finish])
        installed, invalid = set(), set()
        reads = defaultdict(lambda: defaultdict(set))
        catalog = {t["id"] for t in json.loads(tests.read_bytes())["tests"]}
        skip = set(json.loads(tests.read_bytes())["skipped"])
        for pid in start:
            f = json.loads((st / (pid + ".done")).read_bytes())
            installed.update(f["installed"])
            invalid.update(f["invalid"])
            for k, event in f["seen"].items():
                for g, ts in event.items():
                    reads[k][g].update(ts)
        out = {}
        for k in sorted(installed - invalid):
            path, n = names[k]
            obs = {
                g: sorted(reads[k].get(g, set()) & catalog)
                for g in ("schema", "doc")
                if not (reads[k].get(g, set()) - catalog - skip)
            }
            if obs:
                out.setdefault(path, {})[n] = obs
        # Intern repeated reader sets. The committed map holds ordinary JSON.
        ids = {name: i for i, name in enumerate(m["tests"])}
        unique = {}
        for classes in out.values():
            for entry in classes.values():
                for field, readers in entry.items():
                    sig = tuple(sorted(ids[t] for t in readers))
                    entry[field] = unique.setdefault(sig, len(unique))
        result = {
            "commit": m["commit"],
            "sources": {p: paths[p] for p in out},
            "classes": out,
            "tests": m["tests"],
            "sets": list(unique),
        }
        dest.write_text(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
        )
        print(
            "observed processes",
            len(finish),
            "installed",
            len(installed),
            "excluded",
            len(invalid),
            "certified",
            sum(map(len, out.values())),
            flush=True,
        )


if __name__ == "__main__":
    build(*sys.argv[1:])
