"""Collect resolved dialect options during the clean native suite."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from build_dialect_reads import catalog
from build_expression_schemas import ORACLE

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


STRESS = "tests/test_parser.py::TestParser::test_max_nodes"


def build(source, coverage, manifest, output, retain_stress=False):
    root, mapfile, catfile, dest = map(
        lambda p: Path(p).resolve(), (source, coverage, manifest, output)
    )
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
        or subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip()
    ):
        raise ValueError("Expected clean pinned checkout")
    base = json.loads(mapfile.read_bytes())
    ref = catalog(root, base["source_hashes"])
    cat = json.loads(catfile.read_bytes())
    with tempfile.TemporaryDirectory(prefix="br-flags-") as tmp:
        s = Path(tmp)
        (s / "catalog.json").write_text(json.dumps(ref))
        expected = dict(cat)
        if retain_stress:
            expected["tests"] = [t for t in cat["tests"] if t["id"] != STRESS]
        (s / "tests.json").write_text(json.dumps(expected))
        (s / "sitecustomize.py").write_text(
            "from flag_inputs_observer import install\ninstall()\n"
        )
        (s / "br_oracle.py").write_text(ORACLE)
        here = Path(__file__).resolve().parent
        parts = [
            str(s),
            str(here),
            str(here.parent),
            os.environ.get("BR_UPSTREAM_TOOLS", str(here)),
            str(root),
        ]
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join(parts),
            BR_DIALECT_ROOT=str(root),
            BR_DIALECT_STATE=tmp,
            BR_EXPR_STATE=tmp,
            SKIP_INTEGRATION="0",
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
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
                "br_oracle",
                "-p",
                "no:cacheprovider",
                "--timeout=180",
                "--tb=short",
                *(["--deselect", STRESS] if retain_stress else []),
            ],
            cwd=root,
            env=env,
            check=True,
        )
        starts = {p.stem for p in s.glob("*.start")}
        if (
            len(starts) < 5
            or starts != {p.stem for p in s.glob("*.reads")}
            or not (s / "suite-ok").exists()
        ):
            raise ValueError("Incomplete baseline suite")
        created, opaque, by = {}, {}, {}
        for pid in starts:
            a, b = json.loads((s / (pid + ".json")).read_bytes())
            created.update(dict.fromkeys(a))
            opaque.update(dict.fromkeys(b))
            for site, row in json.loads((s / (pid + ".reads")).read_bytes()).items():
                for kind, tests in row.items():
                    by.setdefault(site, {}).setdefault(kind, set()).update(tests)
        audit = sorted({tuple(e) for f in s.glob("*.namespace") for e in json.loads(f.read_bytes())})
        if "BR_NAMESPACE_AUDIT" in os.environ:
            detail = sorted({tuple(e) for f in s.glob("*.literal") for e in json.loads(f.read_bytes())})
            Path(os.environ["BR_NAMESPACE_AUDIT"]).write_text(json.dumps({"opaque":audit,"literal":detail}, indent=2))
        ids = {t["id"]: i for i, t in enumerate(cat["tests"])}
        groups = {}

        def slot(xs):
            numbers = set()
            for x in xs:
                if x == "*":
                    return -1
                if x.endswith("::*"):
                    numbers.update(
                        i for i, t in enumerate(cat["tests"]) if t["file"] == x[:-3]
                    )
                elif x in ids:
                    numbers.add(ids[x])
                elif x not in cat["skipped"]:
                    return -1
            return groups.setdefault(tuple(sorted(numbers)), len(groups))

        out = {}
        for path, classes in ref["sites"].items():
            for line, (cls, name) in classes.items():
                site = path + ":" + line
                if site in created and site not in opaque:
                    r = by.get(site, {})
                    out.setdefault(path, {})[line] = [
                        cls,
                        name,
                        slot(set(r.get("read", ())) | ({STRESS} if retain_stress else set())),
                        slot(r.get("ascii_string", ())),
                    ]
        data = dict(
            pin=PIN,
            sources=ref["sources"],
            names=list(ids),
            groups=list(groups),
            sites=out,
            processes=len(starts),
            retained=[STRESS] if retain_stress else [],
        )
        dest.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n")
        print("Native processes", len(starts), "sites", sum(map(len, out.values())))


if __name__ == "__main__":
    build(*sys.argv[1:5], retain_stress="--retain-memory-test" in sys.argv[5:])
