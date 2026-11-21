"""Record SQLGlot Python files never opened as source during the pinned suite.

On Linux a separate parent watches source-file inodes, including before test
collection and while child interpreters or native extensions run. CPython's
normal imports may use the precompiled pinned bytecode. A queue overflow or a
missing watch invalidates the observations.
"""

from __future__ import annotations

import compileall
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tempfile

EVENT = struct.Struct("iIII")
IN_OPEN = 0x20
IN_QUEUE_OVERFLOW = 0x4000
IN_IGNORED = 0x8000


def run_suite(catalog, receipt):
    import pytest

    table = json.loads(Path(catalog).read_bytes())
    passed, skipped = set(), set()

    class Oracle:
        def pytest_runtest_logreport(self, report):
            if report.skipped:
                skipped.add(report.nodeid)
            elif report.when == "call" and report.passed:
                passed.add(report.nodeid)

    result = pytest.main(
        ["-q", "tests", "--tb=short", "-p", "no:cacheprovider"], plugins=[Oracle()]
    )
    if (
        result
        or passed != {row["id"] for row in table["tests"]}
        or skipped != set(table["skipped"])
    ):
        raise RuntimeError("Incomplete or failing baseline")
    Path(receipt).write_text("passed")


def collect(root, mapping, catalog, out):
    root, mapping, catalog, out = (
        Path(p).resolve() for p in (root, mapping, catalog, out)
    )
    m = json.loads(mapping.read_bytes())
    lib = ctypes.CDLL(None, use_errno=True)
    if not compileall.compile_dir(str(root / "sqlglot"), quiet=1, force=True):
        raise ValueError("Could not precompile pinned baseline")
    sources = {}
    for path, digest in m["source_hashes"].items():
        if path.startswith("sqlglot/") and path.endswith(".py"):
            if hashlib.sha256((root / path).read_bytes()).hexdigest() != digest:
                raise ValueError("Unexpected source hash for " + path)
            sources[path] = digest
    fd = lib.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "Could not create inotify instance")
    watches, opened = {}, set()

    def drain():
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return
            offset = 0
            while offset < len(data):
                watch, mask, _, length = EVENT.unpack_from(data, offset)
                if mask & (IN_QUEUE_OVERFLOW | IN_IGNORED) or watch not in watches:
                    raise RuntimeError("Lost filesystem observation")
                if mask & IN_OPEN:
                    opened.add(watches[watch])
                offset += EVENT.size + length

    try:
        for path in sources:
            wd = lib.inotify_add_watch(fd, os.fsencode(root / path), IN_OPEN)
            if wd < 0 or wd in watches:
                raise OSError(ctypes.get_errno(), "Could not watch a distinct source")
            watches[wd] = path
        env = dict(
            os.environ,
            SKIP_INTEGRATION="0",
            PYTHONPATH=str(root),
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
            PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
        )
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp) / "result"
            with subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--suite",
                    str(catalog),
                    str(receipt),
                ],
                cwd=root,
                env=env,
            ) as child:
                while child.poll() is None:
                    if select.select([fd], [], [], 1)[0]:
                        drain()
                drain()
                if child.returncode or not receipt.is_file():
                    raise ValueError("Baseline suite failed")
    finally:
        os.close(fd)
    result = {
        "commit": m["commit"],
        "unopened": {p: sources[p] for p in sorted(sources.keys() - opened)},
    }
    out.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    print(
        "Observed",
        len(sources),
        "sources;",
        len(result["unopened"]),
        "never opened",
        flush=True,
    )


if __name__ == "__main__":
    if sys.argv[1] == "--suite":
        run_suite(*sys.argv[2:])
    else:
        collect(*sys.argv[1:])
