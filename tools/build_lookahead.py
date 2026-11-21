"""Run all passing pinned SQLGlot tests and pack their actual parser token comparisons."""

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
HEAVY = "tests/test_parser.py::TestParser::test_max_nodes"
PLUGIN = """
import fcntl, json, os, pytest
from pathlib import Path
passed = set()
def pytest_runtest_logreport(report):
    if report.when == 'call' and report.passed:
        passed.add(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    expected = {t['id'] for t in json.loads(Path(os.environ['BR_LEX_CATALOG']).read_bytes())['tests']}
    expected.discard(os.environ['BR_LEX_RETAIN'])
    if exitstatus == 0 and passed == expected:
        (Path(os.environ['BR_LEX_STATE'])/'passed').touch()
@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_protocol(item):
    if item.nodeid.endswith('::TestParser::test_max_nodes'):
        with open(os.environ['BR_GRADING_LOCK'], 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return (yield)
    return (yield)
"""


def pack(root, catalog, state, dest, retained):
    tests = [row["id"] for row in json.loads(catalog.read_bytes())["tests"]]
    names = {t: i for i, t in enumerate(tests)}
    compiled = set()
    unsafe = defaultdict(set)
    traces = defaultdict(lambda: defaultdict(lambda: defaultdict(set)))
    files = list(state.glob("*.end"))
    if (
        not (state / "passed").is_file()
        or len(files) < 4
        or not list(state.glob("*.pool"))
        or {p.stem for p in files} != {p.stem for p in state.glob("*.start")}
    ):
        raise ValueError("Incomplete native suite or incomplete process attribution.")
    for f in files:
        sites, unknown, rows = json.loads(f.read_bytes())
        compiled.update(sites)
        for key, owners in unknown.items():
            unsafe[key].update(owners)
        for key, parts in rows.items():
            for pos, words in parts.items():
                for word, owners in words.items():
                    traces[key][pos][word].update(owners)
    groups = []
    index = {}

    def group(owners):
        members = set()
        for o in owners:
            if o == "*":
                members.add(-1)
            elif o.endswith("::*"):
                members.update(i for n, i in names.items() if n.startswith(o[:-1]))
            elif o in names:
                members.add(names[o])
            else:
                members.add(-1)
        key = tuple(sorted(members))
        if key not in index:
            index[key] = len(groups)
            groups.append(list(key))
        return index[key]

    paths = sorted({k.split(":", 1)[0] for k in compiled})
    packed = {}
    for k in sorted(compiled):
        parts = {}
        for p, w in sorted(traces[k].items()):
            if "\x00" in w or len(w) > 96:
                parts[p] = None
            else:
                parts[p] = {s: group(v) for s, v in sorted(w.items())}
        packed[k] = [group(unsafe[k] | retained), parts]
    data = {
        "pin": PIN,
        "sources": {
            p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths
        },
        "tests": tests,
        "groups": groups,
        "sites": packed,
    }
    dest.write_text(json.dumps(data, separators=(",", ":")) + "\n")
    print(
        len(compiled),
        "profiled call sites,",
        len(files),
        "native processes,",
        len(tests) - len(retained),
        "passing numbered methods;",
        dest.stat().st_size,
        "bytes",
    )


def main():
    root, app, catalog, dest = map(lambda p: Path(p).resolve(), sys.argv[1:5])
    assert (
        subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
        ).strip()
        == PIN
    )
    assert not subprocess.check_output(
        ["git", "-C", str(root), "diff", "HEAD", "--", "sqlglot", "tests"]
    )
    retain = HEAVY if "--retain-memory-test" in sys.argv[5:] else ""
    with tempfile.TemporaryDirectory() as td:
        st = Path(td)
        (st / "sitecustomize.py").write_text(
            "import lookahead_observer; lookahead_observer.install()\n"
        )
        (st / "br_lex_oracle.py").write_text(PLUGIN)
        env = dict(
            os.environ,
            BR_LEX_ROOT=str(root),
            BR_LEX_RETAIN=retain,
            BR_LEX_STATE=str(st),
            BR_LEX_CATALOG=str(catalog),
            BR_GRADING_LOCK=os.environ.get(
                "BR_GRADING_LOCK",
                str(Path(tempfile.gettempdir()) / "blast-radius-maxnodes.lock"),
            ),
            PYTHONPATH=os.pathsep.join(
                (
                    str(st),
                    str(Path(__file__).resolve().parent),
                    str(app / "tools"),
                    str(root),
                )
            ),
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            SKIP_INTEGRATION="0",
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
        env.pop("BR_POOL_OWNER", None)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-qq",
                "-p",
                "br_lex_oracle",
                "-p",
                "no:cacheprovider",
                "--timeout=180",
                "--tb=short",
                *(["--deselect", retain] if retain else []),
            ],
            cwd=root,
            env=env,
            check=True,
        )
        pack(root, catalog, st, dest, {retain} if retain else set())


if __name__ == "__main__":
    main()
