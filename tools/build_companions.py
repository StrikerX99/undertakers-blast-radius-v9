"""Observe source modules loaded by the pinned passing TPC-DS integration method."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    source, checkpoints, output = [Path(x).resolve() for x in sys.argv[1:]]
    dirs = ("dialects", "parsers", "generators")
    with tempfile.TemporaryDirectory() as temp:
        tmp = Path(temp)
        # sitecustomize is inherited by the test's spawn workers. Record each
        # observed import, including any module removed from sys.modules later.
        (tmp / "sitecustomize.py").write_bytes(
            Path(__file__).with_name("observe_executor.py").read_bytes()
        )
        # Actual test code uses spawn. Preserve the test's original worker and
        # initializer, and cap workers independently of the build host's CPU count.
        (tmp / "drive.py").write_text("""
import os, sys, unittest
import sitecustomize
if __name__ == '__mp_main__':
    sitecustomize.spawned('worker')
if __name__ == '__main__':
    from tests import test_executor as mod
    from functools import partial
    sitecustomize.spawned('driver')
    mod.ProcessPoolExecutor = partial(mod.ProcessPoolExecutor, max_workers=2)
    suite = unittest.TestSuite([mod.TestExecutor('test_execute_tpcds')])
    result = unittest.TextTestRunner().run(suite)
    if result.skipped or not result.wasSuccessful():
        sys.exit(1)
""")
        env = dict(
            os.environ,
            BR_ROOT=str(source),
            BR_IMPORT_LOG=temp,
            PYTHONPATH=os.pathsep.join([temp, str(source)]),
            SKIP_INTEGRATION="0",
        )
        subprocess.run(
            [sys.executable, str(tmp / "drive.py")], cwd=source, env=env, check=True
        )
        logs = [json.loads(p.read_bytes()) for p in tmp.glob("*.json")]
        if (
            "driver" not in {r for *_, m in logs for r in m}
            or sum("worker" in m for *_, m in logs) != 2
        ):
            raise ValueError("Both actual workers and the driver must report")
        loaded = {path for mod, _, _, _ in logs for path in mod}
        inside = {(path, site) for _, hits, _, _ in logs for path, site in hits}
        outside = {(path, site) for _, _, hits, _ in logs for path, site in hits}
        eligible = {}
        for path, site in sorted(inside - outside):
            eligible.setdefault(path, []).append(site)
        if not eligible:
            raise ValueError("No exclusive optimizer sites recorded")
        candidates = {
            p.relative_to(source).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for d in dirs
            for p in (source / "sqlglot" / d).glob("*.py")
            if p.name != "__init__.py"
            and p.relative_to(source).as_posix() not in loaded
        }
    # Real spawn workers independently validate the exclusive optimizer
    # sites used by the existing per-query baseline. Store only new evidence.
    original = json.loads(checkpoints.read_bytes())["sites"]
    recorded = {(p, k) for p, rows in original.items() for k in rows}
    if not recorded.issubset(inside - outside):
        raise ValueError(
            "A stored optimizer checkpoint is not exclusive to the worker optimizer"
        )
    output.write_text(
        json.dumps(candidates, sort_keys=True, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    main()
