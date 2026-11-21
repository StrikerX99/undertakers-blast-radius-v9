"""Collect the two long executor tests at the pinned commit, including class and collection state."""

from collections import defaultdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

TEST_CLASS = "tests/test_executor.py::TestExecutor"
CASES = [TEST_CLASS + "::test_execute_tpcds", TEST_CLASS + "::test_execute_tpch"]
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    root, map_file, output = [Path(p).resolve() for p in sys.argv[1:4]]
    catalog = json.loads(map_file.read_bytes())
    hashes = catalog["source_hashes"]
    if catalog["commit"] != PIN or set(CASES) - set(catalog["tests"]):
        raise ValueError("Unexpected baseline or tests")
    for path, digest in hashes.items():
        if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
            raise ValueError("Unrecognized baseline " + path)
    here = Path(__file__).resolve().parent

    def measure(command):
        with tempfile.TemporaryDirectory(prefix="br-token-trace-") as tmp:
            directory = Path(tmp)
            (directory / "source_hashes").write_text(json.dumps(hashes))
            (directory / "sitecustomize.py").write_text(
                "import os\ntry:\n from token_observer import install\n install()\nexcept BaseException:\n os._exit(78)\n"
            )
            (directory / "token_result.py").write_text(
                "import json,os\nfrom pathlib import Path\nr={}\n"
                "def pytest_runtest_logreport(report):\n"
                " if report.when=='call':r[report.nodeid]=report.outcome\n"
                "def pytest_sessionfinish(session,exitstatus):\n"
                " Path(os.environ['BR_TOKEN_TRACE'],'calls').write_text(json.dumps(r))\n"
            )
            env = dict(
                os.environ,
                BR_TOKEN_ROOT=str(root),
                BR_TOKEN_TRACE=tmp,
                PYTHONPATH=os.pathsep.join([tmp, str(here), str(root)]),
                PYTHON_CPU_COUNT="2",
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
                SKIP_INTEGRATION="0",
            )
            env.pop("PYTEST_ADDOPTS", None)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "--tb=short",
                    "-p",
                    "no:cacheprovider",
                    "-p",
                    "token_result",
                    *command,
                ],
                cwd=root,
                env=env,
                check=True,
            )
            results = json.loads((directory / "calls").read_bytes())
            expected = [c for c in CASES if c in command] or CASES
            if any(results.get(c) != "passed" for c in expected):
                raise ValueError("The numbered executor tests must pass")
            pids = {p.stem for p in directory.glob("*.start")}
            deadline = time.monotonic() + 10
            while (
                pids != {p.stem for p in directory.glob("*.json")}
                and time.monotonic() < deadline
            ):
                time.sleep(0.05)
            if pids != {p.stem for p in directory.glob("*.json")} or len(pids) < 4:
                raise ValueError("Incomplete observation of parent and workers")
            values = defaultdict(set)
            active = 0
            for pid in pids:
                samples = json.loads((directory / (pid + ".json")).read_bytes())
                active += bool(samples)
                for k, v in samples.items():
                    values[k].update(v)
            # TPC-H can run only unmarked benchmark rows. Its workers still
            # deserialize expressions and flush explicit empty observations.
            required_active = 1 if command == [CASES[1]] else 3
            if active < required_active:
                raise ValueError("Missing observations in active workers")
            return values

    # Unions from collection of all test modules and the full executor class
    # protect class setup, teardown, table caches and alternate test ordering.
    shared = measure(["tests", "-k", "TestExecutor"])
    tests = {}
    paths = set()
    for name in CASES:
        seen = measure([name])
        test = {}
        for key, values in seen.items():
            test[key] = sorted(values | shared.get(key, set()))
            paths.add(key.split(":")[0])
        tests[name] = test
    sys.path.insert(0, str(root))
    from sqlglot.tokens import TokenType

    data = dict(
        commit=PIN,
        sources={p: hashes[p] for p in paths},
        tests=tests,
        tokens={name: value.name for name, value in TokenType.__members__.items()},
    )
    output.write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main()
