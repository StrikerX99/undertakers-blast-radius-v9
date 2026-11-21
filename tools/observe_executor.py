"""Record imports and optimizer sites inside and outside the actual worker call."""

import atexit
import json
import os
import sys

root = os.environ["BR_ROOT"] + "/"
package = root + "sqlglot/"
optimizer = package + "optimizer/"
driver = root + "tests/test_executor.py"
worker_driver = package + "executor/__init__.py"
imports, inside, outside, markers = set(), set(), set(), set()
stack = []
monitor = sys.monitoring
slot = monitor.PROFILER_ID


def start(code, offset):
    name, filename = code.co_name, code.co_filename
    if name == "<module>" and filename.startswith(package):
        imports.add(filename[len(root) :])
    if not filename.startswith(optimizer):
        return monitor.DISABLE
    if filename == optimizer + "optimizer.py" and name == "optimize":
        frame = sys._getframe(1).f_back
        active = (
            frame is not None
            and frame.f_code.co_filename == worker_driver
            and frame.f_code.co_name == "execute"
        )
        caller = frame.f_back if active else None
        active = (
            caller is not None
            and caller.f_code.co_filename == driver
            and caller.f_code.co_name == "mp_execute"
        )
        stack.append(active)
        monitor.set_local_events(slot, code, monitor.events.PY_RETURN)
    key = (filename[len(root) :], f"{code.co_firstlineno}:{name}")
    if stack and stack[-1]:
        inside.add(key)
    else:
        outside.add(key)
        if name != "optimize":
            return monitor.DISABLE


def stop(code, offset, result):
    stack.pop()


def spawned(label):
    markers.add(label)


def finish():
    if stack:
        raise RuntimeError("Unclosed optimizer boundary")
    with open(
        os.path.join(os.environ["BR_IMPORT_LOG"], str(os.getpid()) + ".json"), "w"
    ) as output:
        json.dump(
            [sorted(imports), sorted(inside), sorted(outside), sorted(markers)], output
        )


monitor.use_tool_id(slot, "executor inputs")
monitor.register_callback(slot, monitor.events.PY_START, start)
monitor.register_callback(slot, monitor.events.PY_RETURN, stop)
monitor.set_events(slot, monitor.events.PY_START)
atexit.register(finish)
