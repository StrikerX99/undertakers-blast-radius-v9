"""Bounded runtime verification for cheap groups of baseline test candidates.

The parent starts a clean Python process against the patched checkout. A
completed unittest result can remove a candidate. A skip, fixture error,
partial result, import failure or resource limit keeps the candidate selected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import forkprobe
from method_probe import MissingReferencePackage


def direct_only(selector, changes, selected):
    return bool(
        selector.direct_tests
        and selected <= selector.direct_tests
        and (
            selector.mixed_fixture_methods
            or all(
                change["old"] == change["new"]
                and change["old"].startswith("tests/")
                and change["old"].endswith(".py")
                for change in changes
            )
        )
    )


def json_fixture_only(changes):
    return bool(changes) and all(
        c["old"] == c["new"]
        and c["old"].startswith("tests/fixtures/")
        and c["old"].endswith(".json")
        for c in changes
    )


def tiny_production_group(selector, changes, selected):
    """Verify small sets of ordinary test methods on a patched production file."""
    if not selected or len(selected) > 6 or len(changes) != 1:
        return False
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return False
    tests = [selector.by_number[number] for number in selected]
    return sum(round(test["seconds"] * 1000) for test in tests) <= 50 and all(
        len(test["id"].split("::")) == 3 for test in tests
    )


def eligible(selector, changes, selected):
    if not selected or selected & selector.unknown:
        return False
    python_sources = {
        change["old"] for change in changes if change["old"].endswith(".py")
    }
    tests = [selector.by_number[number] for number in selected]
    seconds = sum(test["seconds"] for test in tests)
    if not python_sources:
        return json_fixture_only(changes) and seconds <= 0.25
    if seconds > 0.75:
        return False
    if direct_only(selector, changes, selected) or tiny_production_group(
        selector, changes, selected
    ):
        return True
    readers = set()
    for source in python_sources:
        if source in selector.files:
            readers.update(selector.sets[selector.files[source]])
    return all(
        test["file"].startswith("tests/dialects/") or test["number"] in readers
        for test in tests
    )


def verify(selector, changes, selected, seconds, make_checkout, remaining_wall=5):
    if seconds <= 0 or remaining_wall <= 0 or not eligible(selector, changes, selected):
        return selected
    with tempfile.TemporaryDirectory(prefix="br-probe-") as tmp:
        folder = Path(tmp)
        root = folder / "repo"
        make_checkout(root, changes, selector)
        request = folder / "request.json"
        result = folder / "passed.jsonl"
        tests = [selector.by_number[number] for number in sorted(selected)]
        request.write_text(
            json.dumps(
                {
                    "root": str(root),
                    "result": str(result),
                    "tests": tests,
                    "cpu": seconds,
                    "direct": direct_only(selector, changes, selected)
                    or json_fixture_only(changes)
                    or tiny_production_group(selector, changes, selected),
                    "imports": sorted(selector.import_modules),
                }
            )
        )
        env = dict(
            os.environ,
            PYTHONPATH=str(root),
            PYTHONDONTWRITEBYTECODE="1",
            SKIP_INTEGRATION="0",
            OPENBLAS_NUM_THREADS="1",
            OMP_NUM_THREADS="1",
        )
        timeout = min(remaining_wall, max(2.0, seconds * 4.0))
        if not forkprobe.run(json.loads(request.read_text()), env, run_child, timeout):
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), str(request)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                process.wait(timeout=min(remaining_wall, max(2.0, seconds * 4.0)))
            except subprocess.TimeoutExpired:
                pass
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        # A failed import or interruption before the import guard returns
        # broadens the selection exactly as a stand-alone import check would.
        if selector.import_modules and not (folder / "imports-ok").is_file():
            return set(selector.all)
        passed = set()
        if result.is_file():
            for line in result.read_text().splitlines():
                try:
                    passed.update(json.loads(line))
                except (ValueError, TypeError):
                    continue
        return selected - passed


def run_child(request):
    import time

    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(
        signal.ITIMER_PROF, max(0.001, request["cpu"] - time.process_time())
    )
    optional = None
    if request.get("direct"):
        sys.path.insert(0, str(Path(__file__).with_name("method-deps")))
        reference = json.loads(
            Path(__file__).with_name("reference-imports.json").read_bytes()
        )
        optional = MissingReferencePackage(set(reference))
        sys.meta_path.append(optional)

    import importlib
    import itertools
    import unittest

    root = request["root"]
    os.chdir(root)
    sys.path.insert(0, root)

    for module in request.get("imports", []):
        importlib.import_module(module)
    Path(request["result"]).with_name("imports-ok").touch()

    class PositiveResult(unittest.TestResult):
        def __init__(self):
            super().__init__()
            self.passed = set()
            self.skips = set()
            self.fixture_error = False

        def addSuccess(self, test):
            self.passed.add(test.id())
            super().addSuccess(test)

        def addSkip(self, test, reason):
            owner = getattr(test, "test_case", test)
            self.skips.add(owner.id())
            super().addSkip(test, reason)

        def addError(self, test, err):
            if not isinstance(test, unittest.TestCase):
                self.fixture_error = True
            super().addError(test, err)

    cases = []
    numbers = {}
    for test in request["tests"]:
        parts = test["id"].split("::")
        if len(parts) != 3:
            continue
        try:
            module = importlib.import_module(parts[0][:-3].replace("/", "."))
            cls = getattr(module, parts[1])
            if not issubclass(cls, unittest.TestCase):
                continue
            case = cls(parts[2])
        except Exception:
            continue
        cases.append(case)
        numbers[case.id()] = test["number"]
    with open(request["result"], "w", encoding="utf-8") as out:
        for _, group in itertools.groupby(cases, lambda case: type(case).__module__):
            result = PositiveResult()
            unittest.TestSuite(list(group)).run(result)
            if not result.fixture_error and not (optional and optional.missing):
                out.write(
                    json.dumps(
                        [numbers[n] for n in sorted(result.passed - result.skips)]
                    )
                    + "\n"
                )
                out.flush()


if __name__ == "__main__":
    run_child(json.loads(Path(sys.argv[1]).read_text()))
