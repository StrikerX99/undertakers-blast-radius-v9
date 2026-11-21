"""Record generator receivers and expression types during a passing pinned suite."""

import atexit
import json
import os
from pathlib import Path
import sys

import profile_pool


def install():
    profile_pool.install()
    root = os.environ["BR_PINNED"]
    state = Path(os.environ["BR_GEN_READS"])
    pid = str(os.getpid())
    (state / (pid + ".started")).touch()
    code_path = root + "/sqlglot/generator.py"
    seen = {}
    generators = {}
    m = sys.monitoring
    tool = m.PROFILER_ID
    m.use_tool_id(tool, "generator dispatch users")

    def start(code, offset):
        if (
            code.co_name == "sql"
            and code.co_filename == code_path
            and code.co_qualname == "Generator.sql"
        ):
            m.set_local_events(tool, code, m.events.LINE)
        return m.DISABLE

    line_number = int(os.environ["BR_GEN_LINE"])

    def line_event(code, line):
        if line != line_number:
            return m.DISABLE
        f = sys._getframe(1).f_locals
        gen, expr = type(f["self"]), type(f["expression"])
        tag = profile_pool.context().split(" (", 1)[0] or "*"
        g = gen.__module__ + ":" + gen.__qualname__
        if g not in generators:
            generators[g] = [c.__module__ + ":" + c.__qualname__ for c in gen.__mro__]
        seen.setdefault(g, {}).setdefault(expr.__name__, set()).add(tag)

    m.register_callback(tool, m.events.PY_START, start)
    m.register_callback(tool, m.events.LINE, line_event)
    m.set_events(tool, m.events.PY_START)

    @atexit.register
    def save():
        (state / (pid + ".done")).write_text(
            json.dumps(
                {
                    "seen": {
                        g: {n: sorted(ids) for n, ids in rows.items()}
                        for g, rows in seen.items()
                    },
                    "generators": generators,
                }
            )
        )
