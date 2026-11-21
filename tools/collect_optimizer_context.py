"""Record optimizer functions touched during pinned pytest collection.

Executing an optimizer function during test collection makes it ineligible for
exclusive worker replay, even if it is otherwise reached only while optimizing.
"""

import json
import os
from pathlib import Path
import sys

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    import pytest

    root, catalog_file, out = [Path(p).resolve() for p in sys.argv[1:]]
    os.chdir(root)
    sys.path.insert(0, str(root))
    from subprocess import check_output, run

    if check_output(["git", "rev-parse", "HEAD"], text=True).strip() != PIN:
        raise ValueError("Unexpected revision")
    run(["git", "diff", "--quiet", "HEAD", "--"], check=True)
    if any(name == "sqlglot" or name.startswith("sqlglot.") for name in sys.modules):
        raise ValueError("SQLGlot imported before the monitor started")
    catalog = json.loads(catalog_file.read_bytes())
    observed = set()
    collected = set()
    prefix = str(root / "sqlglot/optimizer") + "/"
    monitor = sys.monitoring
    slot = monitor.PROFILER_ID

    def enter(code, offset):
        if code.co_filename.startswith(prefix):
            observed.add(
                (
                    Path(code.co_filename).relative_to(root).as_posix(),
                    f"{code.co_firstlineno}:{code.co_name}",
                )
            )
        return monitor.DISABLE

    class Cases:
        def pytest_collection_finish(self, session):
            collected.update(item.nodeid for item in session.items)

    monitor.use_tool_id(slot, "test collection")
    monitor.register_callback(slot, monitor.events.PY_START, enter)
    monitor.set_events(slot, monitor.events.PY_START)
    try:
        rc = pytest.main(
            ["--collect-only", "-q", "-p", "no:cacheprovider", "tests"],
            plugins=[Cases()],
        )
    finally:
        monitor.set_events(slot, 0)
        monitor.free_tool_id(slot)
    if rc or collected != {row["id"] for row in catalog["tests"]} | set(
        catalog["skipped"]
    ):
        raise ValueError("Incomplete collection")
    by_file = {}
    for path, site in sorted(observed):
        by_file.setdefault(path, []).append(site)
    out.write_text(
        json.dumps(
            {"revision": PIN, "sites": by_file}, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
    print(
        f"{len(collected)} collected. {len(observed)} optimizer sites outside target."
    )


if __name__ == "__main__":
    main()
