"""Attribute native process-pool tasks to the submitting baseline test."""

from concurrent.futures import ProcessPoolExecutor
import os


_owner = None
_task_context = None


def context():
    if os.getpid() == _owner:
        return os.environ.get("PYTEST_CURRENT_TEST", "")
    return _task_context or ""


def _run(context_name, fn, args, kwargs):
    global _task_context
    old, _task_context = _task_context, context_name
    try:
        return fn(*args, **kwargs)
    finally:
        _task_context = old


def install():
    global _owner
    _owner = int(os.environ.setdefault("BR_POOL_OWNER", str(os.getpid())))
    original = ProcessPoolExecutor.submit

    def submit(self, fn, /, *args, **kwargs):
        return original(self, _run, context(), fn, args, kwargs)

    ProcessPoolExecutor.submit = submit
