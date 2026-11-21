"""Record entry to each native parser method once per test and worker task."""

import atexit
from collections import defaultdict
import json
import os
from pathlib import Path
import sys
import threading

_seen = defaultdict(set)
_uncertain = defaultdict(set)


def pool_task(context, fn, args, kwargs):
    import profile_pool

    old = profile_pool._task_context
    profile_pool._task_context = context
    sys.monitoring.restart_events()
    try:
        return fn(*args, **kwargs)
    finally:
        profile_pool._task_context = old
        sys.monitoring.restart_events()


def install():
    import profile_pool

    # Use the native process pool. Preserve the full task identity while each
    # worker executes and restart one-shot coverage after switching owners.
    profile_pool._run = pool_task
    path = str(Path(os.environ["BR_PARSE_ROOT"]) / "sqlglot/parser.py")
    st = Path(os.environ["BR_PARSE_STATE"])
    main = threading.get_ident()
    mon = sys.monitoring
    id_ = mon.COVERAGE_ID
    mon.use_tool_id(id_, "baseline parser method ownership")

    def context():
        if threading.get_ident() != main:
            return "*"
        name, _, when = profile_pool.context().rpartition(" (")
        if not name:
            return "*"
        return name if when == "call)" else name.partition("::")[0] + "::*"

    def on_enter(code, offset):
        if (
            code.co_qualname == "mp_execute"
            and code.co_filename
            == str(Path(os.environ["BR_PARSE_ROOT"]) / "tests/test_executor.py")
            and (context() != "*")
        ):
            (st / f"{os.getpid()}.pool").touch()
        if code.co_filename == path and code.co_qualname.startswith("Parser."):
            name = code.co_qualname.split(".", 2)[1]
            _seen[name].add(context())
        return mon.DISABLE

    def on_exception(code, offset, exc):
        if code.co_filename == path and code.co_qualname.startswith("Parser."):
            line = next(
                (l for start, end, l in code.co_lines() if start <= offset < end), None
            )
            _uncertain[str(line)].add(context())
        return None

    mon.register_callback(id_, mon.events.PY_START, on_enter)
    mon.register_callback(id_, mon.events.RAISE, on_exception)
    mon.set_events(id_, mon.events.PY_START | mon.events.RAISE)

    @atexit.register
    def save():
        (st / f"{os.getpid()}.owners").write_text(
            json.dumps(
                [{k: sorted(v) for k, v in t.items()} for t in (_seen, _uncertain)]
            )
        )
