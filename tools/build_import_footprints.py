"""Record modules actually executed during cold and warm runs of costly catalog tests."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

START = """
import atexit, os, sys
from pathlib import Path
folder = Path(os.environ['BR_IMPORT_TRACE'])
pid = str(os.getpid())
(folder / (pid + '.start')).touch()
root = os.environ['BR_TRACE_ROOT'] + '/'
seen = set()
def enter(code, offset):
    if code.co_name == 'mp_execute' and code.co_filename == root + 'tests/test_executor.py':
        (folder / (pid + '.worker')).touch()
    if code.co_name == '<module>' and code.co_filename.startswith(root):
        seen.add(code.co_filename[len(root):])
    return sys.monitoring.DISABLE
m = sys.monitoring
m.use_tool_id(m.PROFILER_ID, 'baseline module loads')
m.register_callback(m.PROFILER_ID, m.events.PY_START, enter)
m.set_events(m.PROFILER_ID, m.events.PY_START)
@atexit.register
def save():
    (folder / (pid + '.done')).write_text('\\n'.join(sorted(seen)))
"""
ORACLE = """
import os
from pathlib import Path
import pytest
@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    lock = None
    if os.getenv('BR_GRADING_LOCK') and item.nodeid.endswith('::TestParser::test_max_nodes'):
        import fcntl
        lock = open(os.environ['BR_GRADING_LOCK'], 'a')
        fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        yield
    finally:
        if lock:
            lock.close()
def pytest_runtest_logreport(report):
    if report.when == 'call' and report.passed and report.nodeid == os.environ['BR_EXPECT_CASE']:
        (Path(os.environ['BR_IMPORT_TRACE'])/'case-ok').touch()
"""


def record(root, case, warm):
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        (folder / "sitecustomize.py").write_text(START)
        (folder / "br_import_oracle.py").write_text(ORACLE)
        env = dict(
            os.environ,
            BR_IMPORT_TRACE=tmp,
            BR_EXPECT_CASE=case,
            BR_TRACE_ROOT=str(root),
            PYTHONPATH=tmp + os.pathsep + str(root),
            PYTHON_CPU_COUNT="2",
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHONDONTWRITEBYTECODE="1",
        )
        expr = ["tests", "-k", case.rsplit("::", 1)[-1]] if warm else [case]
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-qq",
                "-p",
                "no:cacheprovider",
                "-p",
                "br_import_oracle",
                *expr,
            ],
            cwd=root,
            env=env,
            check=True,
        )
        start = {p.stem for p in folder.glob("*.start")}
        finish = {p.stem for p in folder.glob("*.done")}
        if not start or start != finish or not (folder / "case-ok").is_file():
            raise ValueError("Incomplete target or descendant process trace")
        if case.endswith("::test_execute_tpcds"):
            workers = {p.stem for p in folder.glob("*.worker")}
            if len(start) < 4 or len(workers) < 2 or not workers <= finish:
                raise ValueError("Incomplete real executor worker trace")
        print(case, "warm" if warm else "cold", len(start), "processes", flush=True)
        return set().union(
            *(set((folder / (i + ".done")).read_text().splitlines()) for i in finish)
        )


def collect(root, catalog, output):
    root = root.resolve()
    commit = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if commit != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Unexpected source revision")
    tests = json.loads(catalog.read_bytes())["tests"]
    eligible = [t for t in tests if t["seconds"] >= 20]
    sources = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (root / "sqlglot/dialects").glob("*.py")
        if p.name not in ("__init__.py", "dialect.py")
    }
    cold = {}
    for t in eligible:
        used = record(root, t["id"], False) | record(root, t["id"], True)
        cold[t["id"]] = sorted(sources.keys() - used)
    output.write_text(
        json.dumps(
            {"commit": commit, "sources": sources, "absent": cold},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    collect(*(Path(p) for p in sys.argv[1:]))
