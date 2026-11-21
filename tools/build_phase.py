"""Profile the reference SQL builder separately from the native executor test and its workers."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CASE = "tests/test_executor.py::TestExecutor::test_execute_tpcds"


def main():
    root, app, output = [Path(s).resolve() for s in sys.argv[1:4]]
    tools = app / "tools"
    sys.path.insert(1, str(tools))
    from build_dialect_reads import catalog

    m = json.loads((app / "coverage-map.json").read_bytes())
    if (
        m["commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
        or m["baseline_failures"]
    ):
        raise ValueError("Unexpected coverage baseline")
    for name, digest in m["source_hashes"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Baseline source mismatch at " + name)
    ref = catalog(root, m["source_hashes"])

    def profile(select):
        with tempfile.TemporaryDirectory(prefix="br-ref-phase-") as tmp:
            state = Path(tmp)
            (state / "catalog.json").write_text(json.dumps(ref))
            (state / "sitecustomize.py").write_text(
                "import os\ntry:\n import reference_observer\n reference_observer.install()\nexcept BaseException:\n os._exit(78)\n"
            )
            env = dict(
                os.environ,
                BR_DIALECT_STATE=tmp,
                BR_DIALECT_ROOT=str(root),
                PYTHONPATH=os.pathsep.join(
                    [tmp, str(Path(__file__).resolve().parent), str(tools), str(root)]
                ),
                PYTHON_CPU_COUNT="2",
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
                SKIP_INTEGRATION="0",
                PYTHONWARNINGS="ignore",
            )
            env.pop("PYTEST_ADDOPTS", None)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "-p",
                    "reference_observer",
                    *select,
                ],
                cwd=root,
                env=env,
                check=True,
            )
            starts = {p.stem for p in state.glob("*.start")}
            done = {p.stem for p in state.glob("*.json") if p.stem.isdigit()}
            seen = {p.stem for p in state.glob("*.inside")}
            if not (
                len(starts) >= 4
                and starts == done == seen
                and (state / "passed").read_text() == CASE
            ):
                raise ValueError(("Incomplete processes", starts - done, starts - seen))
            inside, outside, initialized = set(), set(), set()
            functions_in, functions_out = set(), set()
            lines_in, lines_out = set(), set()
            for pid in starts:
                created, read = json.loads((state / (pid + ".json")).read_bytes())
                fi, fo = json.loads((state / (pid + ".funcs")).read_bytes())
                li, lo = json.loads((state / (pid + ".lines")).read_bytes())
                lines_in.update(map(tuple, li))
                lines_out.update(map(tuple, lo))
                functions_in.update(map(tuple, fi))
                functions_out.update(map(tuple, fo))
                initialized.update(created)
                outside.update(read)
                inside.update(json.loads((state / (pid + ".inside")).read_bytes()))
            return (
                initialized & inside - outside,
                functions_in - functions_out,
                lines_in - lines_out,
            )

    # An isolated call and a call after pytest imports all test modules.
    separate = profile([CASE])
    collected = profile(["tests", "-k", "test_execute_tpcds"])
    lines = {}
    for path, qualname, line in sorted(separate[2] & collected[2]):
        lines.setdefault(path, {})[str(line)] = qualname
    allowed = separate[0] & collected[0]
    functions = {}
    for path, name in sorted(separate[1] & collected[1]):
        functions.setdefault(path, []).append(name)
    sites = {}
    for key in sorted(allowed):
        path, line = key.split(":")
        sites.setdefault(path, {})[line] = ref["sites"][path][line]
    output.write_text(
        json.dumps(
            dict(
                commit=m["commit"],
                sites=sites,
                functions=functions,
                lines=lines,
                sources={
                    p: m["source_hashes"][p]
                    for p in set(sites) | set(functions) | set(lines)
                },
            ),
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
