"""Trace independent native generator contexts on the full, passing pinned test suite."""

import hashlib, json, os, subprocess, sys, tempfile
from collections import defaultdict
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PLUGIN = """
import os, fcntl, json, sys, pytest
from pathlib import Path
passed=set()
failed=set()
skipped=set()
def pytest_runtest_logreport(report):
    if report.failed: failed.add(report.nodeid)
    if report.skipped: skipped.add(report.nodeid)
    if report.when == "call" and report.passed: passed.add(report.nodeid)
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_protocol(item):
    if item.nodeid.endswith("::TestParser::test_max_nodes") and os.environ.get("BR_STRESS_LOCK"):
        with open(os.environ["BR_STRESS_LOCK"],"a+") as f:
            fcntl.flock(f,fcntl.LOCK_EX)
            return (yield)
    return (yield)
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_setup(item):
    sys.monitoring.restart_events()
    return (yield)
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_call(item):
    sys.monitoring.restart_events()
    return (yield)
@pytest.hookimpl(wrapper=True,tryfirst=True)
def pytest_runtest_teardown(item):
    sys.monitoring.restart_events()
    return (yield)
def pytest_sessionfinish(session,exitstatus):
    import sqlglot
    state=Path(os.environ['BR_GENERATOR_DATA'])
    (state/'outcomes').write_text(json.dumps([sqlglot.__file__,exitstatus,sorted(passed),sorted(failed),sorted(skipped)]))
"""


def main(repo, catalog, dest):
    repo, catalog, dest = map(lambda p: Path(p).resolve(), (repo, catalog, dest))
    git = ["git", "-C", str(repo)]
    if (
        subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip() != PIN
        or subprocess.check_output(
            git + ["status", "--porcelain", "--untracked-files=no"]
        ).strip()
    ):
        raise ValueError("Unmodified pinned checkout required")
    cat = json.loads(catalog.read_bytes())
    names = [x["id"] for x in cat["tests"]]
    ids = {n: i for i, n in enumerate(names)}
    here = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="gen-native-") as tmp:
        st = Path(tmp)
        (st / "sitecustomize.py").write_text(
            "from generator_context import install\ninstall()\n"
        )
        (st / "gen_plugin.py").write_text(PLUGIN)
        env = dict(
            os.environ,
            BR_GENERATOR_PIN=str(repo),
            BR_GENERATOR_DATA=str(st),
            BR_NAMESPACE_KEYS="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHON_CPU_COUNT="2",
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHONPATH=os.pathsep.join([str(st), str(here), str(repo)]),
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
        env.pop("BR_POOL_OWNER", None)
        env.pop("BR_NAMESPACE_ONLY", None)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "tests",
                "-qq",
                "-p",
                "gen_plugin",
                "-p",
                "no:cacheprovider",
                "--tb=line",
                "--timeout=180",
            ],
            cwd=repo,
            env=env,
        )
        src, rc, passed, failed, skipped = json.loads((st / "outcomes").read_bytes())
        if (
            proc.returncode
            or rc
            or failed
            or set(skipped) != set(cat["skipped"])
            or set(passed) != set(names)
            or Path(src).resolve() != repo / "sqlglot/__init__.py"
        ):
            raise ValueError((rc, failed, skipped, len(passed)))
        pids = {p.stem for p in st.glob("*.started")}
        if len(pids) < 4 or pids != {p.stem for p in st.glob("*.done")}:
            raise ValueError("Unfinished observation")
        vals = defaultdict(set)
        settings = set()
        for p in st.glob("*.done"):
            d = json.loads(p.read_bytes())
            settings.update(d["settings"])
            for k, ts in d["events"].items():
                for t in ts:
                    if t == "*":
                        vals[k].add(-1)
                    elif t.endswith("::*"):
                        vals[k].update(
                            i for i, r in enumerate(cat["tests"]) if r["file"] == t[:-3]
                        )
                    elif t in ids:
                        vals[k].add(ids[t])
                    elif t not in cat["skipped"]:
                        raise ValueError(t)
        sets = {}

        def idx(ts):
            return sets.setdefault(tuple(sorted(ts)), len(sets))

        rows = {k: idx(v) for k, v in sorted(vals.items())}
        if not (
            any(k.startswith("sqlglot/generator.py:") for k in rows)
            and any(k.startswith("sqlglot/generators/") for k in rows)
            and any(k.startswith("value:") for k in rows)
            and "namespace-keys-only" in rows
        ):
            raise ValueError("Missing implementation, setting or namespace observations")
        sources = {
            p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                repo / "sqlglot/generator.py",
                *sorted((repo / "sqlglot/generators").glob("*.py")),
            ]
        }
        dest.write_text(
            json.dumps(
                {
                    "pin": PIN,
                    "field_values": True,
                    "tests": names,
                    "sources": sources,
                    "rows": rows,
                    "settings": sorted(settings),
                    "sets": list(sets),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        print(len(names), len(pids), len(settings), len(rows), len(sets), flush=True)


if __name__ == "__main__":
    main(*sys.argv[1:])
