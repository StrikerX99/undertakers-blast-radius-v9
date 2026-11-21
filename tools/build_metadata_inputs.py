"""Build a passing-suite map from ordinary metadata lookups and namespace consumers."""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from build_expression_schemas import ORACLE

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
FIELD = "EXPRESSION_METADATA"
STRESS = "tests/test_parser.py::TestParser::test_max_nodes"


def build(root, coverage, catalog, dest):
    root, coverage, catalog, dest = map(
        lambda p: Path(p).resolve(), (root, coverage, catalog, dest)
    )
    m = json.loads(coverage.read_bytes())
    if (
        m["commit"] != PIN
        or subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
        or subprocess.check_output(
            ["git", "-C", root, "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip()
    ):
        raise ValueError("Expected the clean pinned SQLGlot checkout")
    cat = json.loads(catalog.read_bytes())
    byname = {t["id"]: i for i, t in enumerate(cat["tests"])}
    # The compact runtime map uses the class's own name. Only build it for a
    # pin whose public expression bindings are canonical ordinary types.
    sys.path.insert(0, str(root))
    from sqlglot import exp

    if not Path(exp.__file__).resolve().is_relative_to(root) or any(
        type(value) is not type or value.__name__ != key
        for key, value in vars(exp).items()
        if key[:1].isupper() and isinstance(value, type) and issubclass(value, exp.Expr)
    ):
        raise ValueError("Aliased expression type or unexpected metaclass")
    targets, owners, sources, test_files = set(), set(), {}, set()
    for f in (root / "sqlglot").rglob("*.py"):
        raw = f.read_bytes()
        if FIELD.encode() not in raw:
            continue
        path = f.relative_to(root).as_posix()
        if m["source_hashes"].get(path) != hashlib.sha256(raw).hexdigest():
            raise ValueError(path)
        tree = ast.parse(raw)
        for owner in [tree] + [c for c in tree.body if isinstance(c, ast.ClassDef)]:
            for s in owner.body:
                t = (
                    s.target
                    if isinstance(s, ast.AnnAssign)
                    else s.targets[0]
                    if isinstance(s, ast.Assign) and len(s.targets) == 1
                    else None
                )
                if not isinstance(t, ast.Name) or t.id != FIELD:
                    continue
                name = path[:-3].replace("/", ".").removesuffix(".__init__")
                targets.add(name)
                owners.add(name + ("." + owner.name if owner is not tree else ""))
                if path.startswith("sqlglot/typing/"):
                    sources[path] = m["source_hashes"][path]
    for f in (root / "tests").rglob("*.py"):
        if b"expression_metadata" in f.read_bytes().lower():
            test_files.add(f.relative_to(root).as_posix())

    with tempfile.TemporaryDirectory(prefix="br-meta-") as t:
        s = Path(t)
        (s / "targets.json").write_text(json.dumps(sorted(targets)))
        (s / "sitecustomize.py").write_text(
            "from metadata_observer import install\ninstall()\n"
        )
        (s / "br_oracle.py").write_text(ORACLE)
        (s / "tests.json").write_text(
            json.dumps(dict(cat, tests=[x for x in cat["tests"] if x["id"] != STRESS]))
        )
        here = Path(__file__).resolve().parent
        env = dict(
            os.environ,
            BR_META_ROOT=str(root),
            BR_EXPR_STATE=t,
            SKIP_INTEGRATION="0",
            PYTHONPATH=os.pathsep.join(
                [
                    t,
                    str(here),
                    os.environ.get("BR_UPSTREAM_TOOLS", str(here)),
                    str(root),
                ]
            ),
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PATH=str(Path(sys.executable).parent) + ":" + os.environ["PATH"],
        )
        env.pop("BR_POOL_OWNER", None)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-qq",
                "-p",
                "br_oracle",
                "-p",
                "no:cacheprovider",
                "--timeout=180",
                "--tb=short",
                "tests",
                "--deselect",
                STRESS,
            ],
            cwd=root,
            env=env,
            check=True,
        )
        starts = {f.stem for f in s.glob("*.start")}
        if (
            len(starts) < 5
            or starts != {f.stem for f in s.glob("*.reads")}
            or not (s / "suite-ok").is_file()
        ):
            raise ValueError("Incomplete baseline")
        created, reads = set(), {}
        for f in s.glob("*.reads"):
            obs = json.loads(f.read_bytes())
            created.update(obs["created"])
            for k, v in obs["reads"].items():
                reads.setdefault(k, set()).update(v)
        if created != owners:
            raise ValueError(("Unregistered metadata", owners - created))
    wild = reads.setdefault("*", set())
    wild.add(STRESS)
    wild.update(x["id"] for x in cat["tests"] if x["file"] in test_files)

    def nums(xs):
        result = set()
        for x in xs:
            if x == "*":
                return [-1]
            if x.endswith("::*"):
                result.update(
                    i for i, row in enumerate(cat["tests"]) if row["file"] == x[:-3]
                )
            elif x in byname:
                result.add(byname[x])
            elif x not in cat["skipped"]:
                return [-1]
        return sorted(result)

    final = {
        "pin": PIN,
        "sources": sources,
        "names": list(byname),
        "readers": {k: nums(v) for k, v in reads.items()},
        "processes": len(starts),
    }
    dest.write_text(json.dumps(final, sort_keys=True, separators=(",", ":")) + "\n")
    print(
        "Native processes",
        len(starts),
        "registered tables",
        len(created),
        "wildcard readers",
        final["readers"]["*"],
        flush=True,
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
