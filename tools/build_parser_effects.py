"""Collect per-test guard and optional-argument use on the passing pinned SQLGlot suite."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from collections import defaultdict

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PYTEST_PLUGIN = """
import pytest, os, fcntl
from pathlib import Path
passed=set()
def pytest_runtest_logreport(report):
    if report.when=="call" and report.passed: passed.add(report.nodeid)
def pytest_sessionfinish(session,exitstatus):
    if exitstatus == 0 and os.environ['BR_CASE'] in passed:
        (Path(os.environ['BR_PARSE_STATE'])/'passed').touch()
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_protocol(item):
    if os.environ.get("BR_GRADING_LOCK") and item.nodeid.endswith("::TestParser::test_max_nodes"):
        with open(os.environ["BR_GRADING_LOCK"],"a") as handle:
            fcntl.flock(handle,fcntl.LOCK_EX)
            return (yield)
    return (yield)
"""


def main():
    root, app, cat, dest = map(lambda p: Path(p).resolve(), sys.argv[1:5])
    assert (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        == PIN
    )
    with tempfile.TemporaryDirectory() as tmp:
        st = Path(tmp)
        (st / "sitecustomize.py").write_text(
            "import parser_effect_observer, parser_ownership; parser_effect_observer.install(); parser_ownership.install()"
        )
        (st / "pb_oracle.py").write_text(
            PYTEST_PLUGIN.replace(
                "if exitstatus == 0 and os.environ['BR_CASE'] in passed:",
                """import json
    if exitstatus == 0 and passed == {r['id'] for r in json.loads(Path(os.environ['BR_CATALOG']).read_text())['tests']}:""",
            )
        )
        with (st / "pb_oracle.py").open("a") as plugin:
            plugin.write("""
import sys
def reset():
    sys.monitoring.restart_events()
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_setup(item):
    reset()
    try: return (yield)
    finally: reset()
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_call(item):
    reset()
    try: return (yield)
    finally: reset()
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_teardown(item):
    reset()
    try: return (yield)
    finally: reset()
""")
        env = dict(
            os.environ,
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
            BR_PARSE_ROOT=str(root),
            BR_PARSE_STATE=str(st),
            BR_CATALOG=str(cat),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON_CPU_COUNT="2",
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHONPATH=os.pathsep.join(
                (
                    str(st),
                    str(Path(__file__).parent.resolve()),
                    str(Path(__file__).resolve().parents[1]),
                    str(app / "tools"),
                    str(app),
                    str(root),
                )
            ),
        )
        env.pop("BR_POOL_OWNER", None)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-qq",
                "-p",
                "pb_oracle",
                "-p",
                "no:cacheprovider",
                "tests",
            ],
            check=True,
            env=env,
            cwd=root,
        )
        starts = {p.stem for p in st.glob("*.start")}
        done = {p.stem for p in st.glob("*.json")}
        if (
            starts != done
            or len(done) < 5
            or not (st / "passed").exists()
            or len(list(st.glob("*.pool"))) < 2
        ):
            raise ValueError("Incomplete owner or native workers")
        sites = set()
        dirty = defaultdict(set)
        owners = defaultdict(set)
        throws = defaultdict(set)
        for pid in sorted(done):
            fn, exc = json.loads((st / (pid + ".owners")).read_bytes())
            for k, v in fn.items():
                owners[k].update(v)
            for k, v in exc.items():
                throws[k].update(v)
            allsites, rows = json.loads((st / (pid + ".json")).read_bytes())
            sites.update(allsites)
            for k, v in rows.items():
                dirty[k].update(v)
        rv = {
            "pin": PIN,
            "source": hashlib.sha256(
                (root / "sqlglot/parser.py").read_bytes()
            ).hexdigest(),
            "sites": {k: sorted(dirty[k]) for k in sorted(sites)},
            "owners": {k: sorted(v) for k, v in owners.items()},
            "throws": {k: sorted(v) for k, v in throws.items()},
        }
        dest.write_text(json.dumps(rv, separators=(",", ":")) + "\n")
        print("baseline", len(sites), "sites;", len(done), "native processes")


if __name__ == "__main__":
    main()
