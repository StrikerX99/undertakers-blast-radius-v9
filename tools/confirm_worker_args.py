"""Extend whole-suite lookup observations using the real fresh process-pool workers."""

import atexit
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def start():
    """Run from sitecustomize before Python imports the pinned library."""
    state = Path(os.environ["BR_TRACE_DIRECTORY"])
    root = Path(os.environ["BR_TRACE_PROJECT"])
    identity = str(os.getpid())
    (state / (identity + ".start")).touch()
    try:
        from build_arg_lookups import install

        data = json.loads((state / "hashes.json").read_bytes())
        rows, _ = install(root, data)
    except BaseException:
        os._exit(78)

    def persist():
        cooked = {}
        for (path, *span), row in rows.items():
            if not row["unsafe"] and not row["seen"]:
                continue
            key = ":".join(map(str, span))
            cooked.setdefault(path, {})[key] = (
                None
                if row["unsafe"]
                else [
                    row["key"],
                    sorted(row["different_on_none"]),
                    None
                    if row["same_on_nonnull"] is None
                    else sorted(row["same_on_nonnull"]),
                ]
            )
        out = state / (identity + ".results")
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(cooked, separators=(",", ":")))
        tmp.replace(out)

    atexit.register(persist)


def main():
    root, mapfile, output = [Path(p).resolve() for p in sys.argv[1:4]]
    base = json.loads(output.read_bytes())
    initial = output.read_bytes()
    m = json.loads(mapfile.read_bytes())
    with tempfile.TemporaryDirectory(prefix="arg-verification-") as tmp:
        state = Path(tmp)
        (state / "hashes.json").write_text(
            json.dumps({"source_hashes": m["source_hashes"]})
        )
        (state / "sitecustomize.py").write_text(
            "from confirm_worker_args import start\nstart()\n"
        )
        env = dict(
            os.environ,
            BR_TRACE_DIRECTORY=tmp,
            BR_TRACE_PROJECT=str(root),
            PYTHON_CPU_COUNT="2",
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PYTHONPATH=os.pathsep.join(
                [tmp, str(Path(__file__).resolve().parent), str(root)]
            ),
        )
        env.pop("PYTEST_ADDOPTS", None)
        commands = [
            "tests/test_executor.py::TestExecutor::test_execute_tpch",
            "tests/test_executor.py::TestExecutor::test_execute_tpcds",
            "tests/test_optimizer.py::TestOptimizer",
        ]
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *commands,
                "-q",
                "--tb=short",
                "-p",
                "no:cacheprovider",
            ],
            check=True,
            env=env,
            cwd=root,
        )
        pids = {p.stem for p in state.glob("*.start")}
        deadline = time.monotonic() + 10
        while (
            pids != {p.stem for p in state.glob("*.results")}
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        if len(pids) < 5 or pids != {p.stem for p in state.glob("*.results")}:
            raise ValueError("Incomplete process observation")
        active = 0
        for pid in sorted(pids):
            observed = json.loads((state / (pid + ".results")).read_bytes())
            active += bool(observed)
            for path, rows in observed.items():
                existing = base["rows"].get(path, {})
                for key, row in rows.items():
                    if key not in existing:
                        continue
                    previous = existing[key]
                    if row is None:
                        del existing[key]
                        continue
                    if previous[0] != row[0]:
                        raise ValueError("Inconsistent source span")
                    previous[1] = sorted(set(previous[1]) | set(row[1]))
                    if row[2] is not None:
                        previous[2] = sorted(
                            set(row[2])
                            if previous[2] is None
                            else set(previous[2]) & set(row[2])
                        )
        if active < 4:
            raise ValueError("Missing records from active workers")
        final = (
            json.dumps(base, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
        output.write_bytes(final)
        print(
            f"Observed {len(pids)} processes, {active} executed lookups; profile identical = {initial == final}"
        )
        print("Profile SHA-256", hashlib.sha256(final).hexdigest())


if __name__ == "__main__":
    main()
