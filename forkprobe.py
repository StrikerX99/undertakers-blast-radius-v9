"""Launch a Linux probe with a fresh view of SQLGlot and the test modules."""

import os
import signal
import sys
import time


def run(payload, env, worker, timeout):
    # Only the selector and the standard library may be inherited. If another
    # caller has already imported project code, request a fresh interpreter.
    if any(
        k in ("sqlglot", "tests") or k.startswith(("sqlglot.", "tests."))
        for k in sys.modules
    ):
        return False
    # unittest is used by every probe. Reuse only this standard-library import.
    import unittest  # noqa: F401

    pid = os.fork()
    if not pid:
        try:
            os.setsid()
            with open(os.devnull, "w") as output:
                os.dup2(output.fileno(), 1)
                os.dup2(output.fileno(), 2)
            os.environ.update(env)
            sys.dont_write_bytecode = True
            worker(payload)
            os._exit(0)
        except BaseException:
            os._exit(1)
    deadline = time.monotonic() + timeout
    try:
        while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.01)
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            # If the child did not reach setsid() before timeout, kill its pid.
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        os.waitpid(pid, 0)
    return True
