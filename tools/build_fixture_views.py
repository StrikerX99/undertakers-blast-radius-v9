"""Record how baseline tests that read SQL fixtures obtain their contents."""

from collections import defaultdict
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

root, index, output = (Path(arg).resolve() for arg in sys.argv[1:4])
base = json.loads(index.read_bytes())
helper_path = "tests/helpers.py"
assert base["commit"] == "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
digest = hashlib.sha256((root / helper_path).read_bytes()).hexdigest()
assert digest == base["source_hashes"][helper_path]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from build_map import InlinePool

concurrent.futures.ProcessPoolExecutor = InlinePool
os.chdir(root)
sys.path.insert(0, str(root))
from tests import helpers

modes = {
    helpers.load_sql_fixtures.__code__: "single",
    helpers.load_sql_fixture_pairs.__code__: "pairs",
}
paths = {
    str(root / p): p
    for p in base["files"]
    if p.startswith("tests/fixtures/") and p.endswith(".sql")
}
active = ""
reads = defaultdict(lambda: defaultdict(set))


def opened(event, args):
    if event != "open" or not isinstance(args[0], str):
        return
    absolute = os.path.abspath(args[0])
    path = paths.get(absolute)
    if path is None:
        return
    frame = sys._getframe(1)
    reads[path][active].add(modes.get(frame.f_code, "raw"))


class Tests:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global active
        active = item.nodeid
        try:
            yield
        finally:
            active = ""

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global active
        prior = active
        active = "@" + item.nodeid.rsplit("::", 1)[0]
        try:
            yield
        finally:
            active = prior


if __name__ == "__main__":
    sys.addaudithook(opened)
    nodes = {
        base["tests"][i] for p in paths.values() for i in base["sets"][base["files"][p]]
    }
    if pytest.main(
        ["-q", "-p", "no:cacheprovider", "--tb=short", *sorted(nodes)],
        plugins=[Tests()],
    ):
        raise SystemExit("Fixture reference tests failed")
    table = {}
    for p, contexts in reads.items():
        if "" in contexts:
            continue
        for idx in base["sets"][base["files"][p]]:
            test = base["tests"][idx]
            used = contexts.get(test, set()) | contexts.get(
                "@" + test.rsplit("::", 1)[0], set()
            )
            if used and "raw" not in used:
                table.setdefault(p, {})[str(idx)] = sorted(used)
    output.write_text(
        json.dumps(
            {"commit": base["commit"], "sha256": digest, "files": table},
            separators=(",", ":"),
        )
    )
    print(len(table), "fixtures with identified readers")
