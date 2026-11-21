"""Record conditional suites reached in the pinned native TPC-DS test."""

import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
PATH = "sqlglot/optimizer/simplify.py"


def collect(root, selector, out):
    changed = subprocess.check_output(
        ["git", "-C", str(root), "status", "--porcelain", "-uno"]
    )
    revision = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != PIN or changed:
        raise ValueError("Clean pinned checkout required")
    ref = json.loads((selector / "executor-queries.json").read_bytes())
    source = (root / PATH).read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    if (
        ref["commit"] != PIN
        or hashlib.sha256((root / "tests/test_executor.py").read_bytes()).hexdigest()
        != ref["driver_sha"]
    ):
        raise ValueError("Incompatible executor evidence")
    (cls,) = (
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.ClassDef) and n.name == "Simplifier"
    )
    keys = {
        f"{n.lineno}:{n.col_offset}:{field}"
        for method in cls.body
        if isinstance(method, ast.FunctionDef)
        for n in ast.walk(method)
        if isinstance(n, ast.If)
        for field in ("body", "orelse")
        if getattr(n, field)
    }
    native = Path(__file__).resolve().parent / "simplifier_native"
    with tempfile.TemporaryDirectory() as raw:
        env = dict(
            os.environ,
            OPT_ROOT=str(root),
            OPT_OUT=raw,
            SKIP_INTEGRATION="0",
            PYTHON_CPU_COUNT="2",
            OMP_NUM_THREADS="1",
            OPENBLAS_NUM_THREADS="1",
            PYTHONPATH=os.pathsep.join(map(str, (native, selector, root))),
        )
        subprocess.run(
            [sys.executable, str(native / "run.py")], env=env, cwd=root, check=True
        )
        ids = {}
        seen = set()
        introspected = set()
        for path in Path(raw).glob("*.json"):
            rows, suites, compiled, inspected = json.loads(path.read_bytes())
            introspected.update(inspected)
            if (rows or suites) and compiled != source_hash:
                raise ValueError("Unverified interpreter")
            if rows.keys() & ids.keys() or not suites.keys() <= keys:
                raise ValueError("Unexpected worker results")
            ids.update(rows)
            seen.update(suites)
    if set(map(int, ids)) != set(range(99)) or {
        i: v for i, v in ids.items() if v is not None
    } != {i: r["result"] for i, r in ref["queries"].items()}:
        raise ValueError("Incomplete or changed query results")
    unentered = sorted(keys - seen)
    out.write_text(
        json.dumps(
            {
                "pin": PIN,
                "sha": source_hash,
                "unentered": unentered,
                "introspected": sorted(introspected),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    print(
        f"{len(unentered)} of {len(keys)} suites unentered; all {len(ids)} native tasks accounted for"
    )


if __name__ == "__main__":
    collect(*(Path(p).resolve() for p in sys.argv[1:4]))
