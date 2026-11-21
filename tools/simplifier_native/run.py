import os
from pathlib import Path
import unittest

if __name__ == "__main__":
    import observe
    from tests import test_executor

    if (
        Path(test_executor.__file__).resolve()
        != Path(os.environ["OPT_ROOT"]) / "tests/test_executor.py"
    ):
        raise ValueError("Wrong executor test")
    TestExecutor = test_executor.TestExecutor

    s = unittest.TestSuite([TestExecutor("test_execute_tpcds")])
    outcome = unittest.TextTestRunner().run(s)
    if not outcome.wasSuccessful() or outcome.skipped or observe.source_hash is None:
        raise SystemExit(1)
