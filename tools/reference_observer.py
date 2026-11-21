"""Track which pinned dialect settings are read exclusively by the expected-result SQL."""

import atexit
import json
import os
from pathlib import Path
import sys

active = False
reference_reads = set()


def install():
    from dialect_observer import install as watch

    def reference_reader(site):
        if active:
            reference_reads.add(site)
        return active

    watch(reference_reader)
    source_root = str(Path(os.environ["BR_DIALECT_ROOT"]).resolve()) + "/"
    outside_functions, inside_functions = set(), set()
    outside_lines, inside_lines = set(), set()
    from sys import monitoring

    tool = 4
    monitoring.use_tool_id(tool, "blast-reference-source")

    def enter(code, offset):
        filename = code.co_filename
        if not filename.startswith(source_root + "sqlglot/") or "<" in code.co_qualname:
            return monitoring.DISABLE
        site = (filename[len(source_root) :], code.co_qualname)
        if active:
            inside_functions.add(site)
        else:
            outside_functions.add(site)
            return monitoring.DISABLE

    def visit_line(code, line):
        filename = code.co_filename
        if not filename.startswith(source_root + "sqlglot/") or "<" in code.co_qualname:
            return monitoring.DISABLE
        site = (filename[len(source_root) :], code.co_qualname, line)
        if active:
            inside_lines.add(site)
        else:
            outside_lines.add(site)
            return monitoring.DISABLE

    monitoring.register_callback(tool, monitoring.events.PY_START, enter)
    monitoring.register_callback(tool, monitoring.events.LINE, visit_line)
    monitoring.set_events(tool, monitoring.events.PY_START | monitoring.events.LINE)

    @atexit.register
    def save():
        monitoring.set_events(tool, 0)
        where = Path(os.environ["BR_DIALECT_STATE"])
        (where / (str(os.getpid()) + ".lines")).write_text(
            json.dumps([sorted(inside_lines), sorted(outside_lines)])
        )
        (where / (str(os.getpid()) + ".funcs")).write_text(
            json.dumps([sorted(inside_functions), sorted(outside_functions)])
        )
        (where / (str(os.getpid()) + ".inside")).write_text(
            json.dumps(sorted(reference_reads))
        )


def pytest_collection_modifyitems(items):
    module = sys.modules["tests.test_executor"]
    cached_code = module.TestExecutor.cached_execute.__code__
    original = module.transpile

    def transpile(*args, **kwargs):
        global active
        previous = active
        active = sys._getframe(1).f_code is cached_code
        try:
            return original(*args, **kwargs)
        finally:
            active = previous

    module.transpile = transpile


def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed:
        where = Path(os.environ["BR_DIALECT_STATE"])
        (where / "passed").write_text(report.nodeid)
