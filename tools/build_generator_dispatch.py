"""Build a first-use dependency profile of generator transform dispatch from native pytest."""

import argparse
from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ORACLE = 'import json\nimport os\nfrom pathlib import Path\nimport sqlglot\n\nevents = {"calls":{}, "failed":set(), "skipped":set(), "collect_errors":{}, "collected":[], "origin":sqlglot.__file__}\ndef pytest_collection_finish(session):\n    events["collected"] = [item.nodeid for item in session.items]\ndef pytest_collectreport(report):\n    if report.failed:\n        events["collect_errors"][report.nodeid] = str(report.longrepr)\ndef pytest_runtest_logreport(report):\n    if report.failed:\n        events["failed"].add(report.nodeid)\n    if report.skipped:\n        events["skipped"].add(report.nodeid)\n    if report.when == "call":\n        events["calls"].setdefault(report.nodeid,{}).setdefault(report.outcome,0)\n        events["calls"][report.nodeid][report.outcome] += 1\ndef pytest_sessionfinish(session,exitstatus):\n    for key in ("failed","skipped"):\n        events[key]=sorted(events[key])\n    events["exitstatus"]=exitstatus\n    Path(os.environ["ORACLE"]).write_text(json.dumps(events))\n\n\n# Serialize the large parser test outside its method timeout.\nimport fcntl\nimport pytest\n@pytest.hookimpl(wrapper=True,tryfirst=True)\ndef pytest_runtest_protocol(item):\n    if not item.nodeid.endswith(\'::TestParser::test_max_nodes\'):\n        return (yield)\n    with open(os.environ[\'BR_STRESS_LOCK\'],\'a+\') as fd:\n        fcntl.flock(fd,fcntl.LOCK_EX)\n        return (yield)\n'

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def build(root, catalog_path, dest, keep, lock):
    root, catalog_path, dest, keep = map(Path.resolve, (root, catalog_path, dest, keep))
    cat = json.loads(catalog_path.read_bytes())
    tests = [v["id"] for v in cat["tests"]]
    ids = dict(zip(tests, range(len(tests))))
    pin = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if (
        pin != PIN
        or subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip()
    ):
        raise ValueError("Expected pinned unmodified checkout")
    here = Path(__file__).resolve().parent
    keep.mkdir(exist_ok=False)
    (keep / "sitecustomize.py").write_text(
        "from generator_dispatch_observer import install\ninstall()\n"
    )
    (keep / "br_dispatch_oracle.py").write_text(ORACLE)
    core = root / "sqlglot/generator.py"
    pattern = "handler = self._dispatch.get(expression.__class__)"
    (line,) = [
        i
        for i, s in enumerate(core.read_text().splitlines(), 1)
        if s.strip() == pattern
    ]
    report = keep / "pytest.json"
    env = dict(
        os.environ,
        BR_GEN_READS=str(keep),
        BR_PINNED=str(root),
        BR_GEN_LINE=str(line),
        ORACLE=str(report),
        BR_STRESS_LOCK=str(lock),
        PYTHONPATH=os.pathsep.join((str(keep), str(here), str(root))),
        SKIP_INTEGRATION="0",
        OPENBLAS_NUM_THREADS="1",
        OMP_NUM_THREADS="1",
        PYTHON_CPU_COUNT="1",
        PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests",
            "-qq",
            "-p",
            "br_dispatch_oracle",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "--timeout=180",
        ],
        cwd=root,
        env=env,
        check=True,
    )
    receipt = json.loads(report.read_bytes())
    if (
        Path(receipt["origin"]).resolve() != root / "sqlglot/__init__.py"
        or receipt["failed"]
        or receipt["collect_errors"]
        or set(receipt["skipped"]) != set(cat["skipped"])
        or not set(tests) <= set(receipt["calls"])
    ):
        raise ValueError("Incomplete baseline outcomes")
    started = {p.stem for p in keep.glob("*.started")}
    done = {p.stem for p in keep.glob("*.done")}
    if started != done or len(started) < 4:
        raise ValueError(("Incomplete process coverage", started, done))
    all_groups = defaultdict(set)
    per = defaultdict(lambda: defaultdict(set))
    for pid in sorted(done):
        data = json.loads((keep / (pid + ".done")).read_bytes())
        for gen, exprs in data["seen"].items():
            classes = data["generators"][gen]
            for e, readers in exprs.items():
                users = {ids.get(t, -1) for t in readers if t not in cat["skipped"]}
                all_groups[e].update(users)
                for base in classes:
                    if base.startswith("sqlglot.generators."):
                        per[base][e].update(users)
    sets = {}

    def index(vals):
        key = tuple(sorted(vals))
        return sets.setdefault(key, len(sets))

    names = {
        c: {e: index(ts) for e, ts in sorted(es.items())}
        for c, es in sorted(per.items())
    }
    broad = {k: index(v) for k, v in sorted(all_groups.items())}
    sources = {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((root / "sqlglot/generators").glob("*.py"))
    }
    from structural_inputs import metadata

    static = metadata(root)
    dest.write_text(
        json.dumps(
            {
                "commit": PIN,
                "sources": sources,
                "tests": tests,
                "rows": names,
                "across": broad,
                "structural": static,
                "sets": list(sets),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        "verified",
        len(tests),
        "methods",
        len(done),
        "processes",
        len(names),
        "generator classes",
        flush=True,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for k in ["root", "catalog", "dest"]:
        p.add_argument(k, type=Path)
    p.add_argument("--stress-lock", type=Path)
    a = p.parse_args()
    with tempfile.TemporaryDirectory(prefix="br-generator-observer-") as tmp:
        t = Path(tmp)
        build(
            a.root,
            a.catalog,
            a.dest,
            t / "events",
            (a.stress_lock or (t / "lock")).resolve(),
        )
