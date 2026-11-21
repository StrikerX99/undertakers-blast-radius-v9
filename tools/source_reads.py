import pytest
import json
import os
import sys
from pathlib import Path
from collections import defaultdict

root = Path(sys.argv[1]).resolve()
m = json.load(open(sys.argv[2]))
selected = sorted(
    {
        m["tests"][i]
        for p, s in m["files"].items()
        if p.endswith(".py")
        for i in m["sets"][s]
    }
)
# This small rerun isolates explicit source inspection from lazy imports.
active = ""
reads = defaultdict(set)


def audit(event, args):
    if (
        event != "open"
        or not active
        or not isinstance(args[0], str)
        or not args[0].endswith(".py")
    ):
        return
    f = sys._getframe(1)
    while f:
        filename = f.f_code.co_filename
        if filename.startswith("<frozen importlib"):
            return
        if filename.startswith(str(root / "tests")):
            break
        f = f.f_back
    try:
        path = str(Path(args[0]).resolve().relative_to(root))
    except ValueError:
        return
    reads[path].add(active)


os.chdir(root)
sys.path.insert(0, str(root))
os.environ["SKIP_INTEGRATION"] = "0"


class Plugin:
    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_setup(self, item):
        global active
        active = "@setup:" + item.nodeid.rsplit("::", 1)[0]
        yield
        active = item.nodeid

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item, nextitem):
        global active
        active = item.nodeid
        yield
        active = ""


sys.addaudithook(audit)
result = pytest.main(
    [*selected, "-q", "-p", "no:cacheprovider", "--tb=short"], plugins=[Plugin()]
)
reads = {
    p: {
        t
        for n in names
        for t in (
            [x for x in m["tests"] if x.startswith(n[7:] + "::")]
            if n.startswith("@setup:")
            else [n]
        )
    }
    for p, names in reads.items()
}
Path(sys.argv[3]).write_text(json.dumps({p: sorted(v) for p, v in reads.items()}))
print("nodes", len(selected), "files", len(reads))
raise SystemExit(result)
