"""Measure tokenizer configuration value reads on the competition's pinned baseline.

The pinned implementation sends NUMERIC_ESCAPES unchanged into TokenizerCore.
All value reads go through mapping.get there. Other uses in the generator and
metaclass inspect only emptiness and key membership, so key-preserving literal
value changes cannot affect those operations. Record value readers by key; if a
consumer or its source changes, the pin guard disables this supplementary map.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import sys
from collections import defaultdict
from concurrent import futures
import pytest

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    root = Path(sys.argv[1]).resolve()
    coverage = json.loads(Path(sys.argv[2]).read_text())
    output = Path(sys.argv[3]).resolve()
    if coverage["commit"] != PIN or coverage["baseline_failures"]:
        raise RuntimeError("Baseline coverage does not match the pinned project")
    # Shared build_map.py lives one level above tools in the submission.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    for path in (
        "sqlglot/tokenizer_core.py",
        "sqlglot/tokens.py",
        "sqlglot/generator.py",
        "sqlglot/dialects/dialect.py",
    ):
        if (
            hashlib.sha256((root / path).read_bytes()).hexdigest()
            != coverage["source_hashes"][path]
        ):
            raise RuntimeError("Re-audit configuration consumers in " + path)
    os.chdir(root)
    sys.path.insert(0, str(root))
    os.environ["SKIP_INTEGRATION"] = "0"
    from sqlglot import Dialect

    # Loading all dialects registers inherited and overridden tokenizer settings.
    tokenizer_classes = {d.tokenizer_class for d in Dialect.classes.values()}
    tests = coverage["tests"]
    symbols = {}
    lookup = defaultdict(set)
    emitted = defaultdict(set)
    current = None
    replacements = {}
    observed = set()

    class Reads(dict):
        def __init__(self, base, site):
            super().__init__(base)
            self.site = site

        def get(self, key, default=None):
            if key in self:
                if current is None:
                    raise RuntimeError(
                        "Unexpected configuration value read outside a test"
                    )
                for entry in current:
                    lookup[self.site, key].add(entry)
            return super().get(key, default)

    for tokenizer in tokenizer_classes:
        for owner in tokenizer.__mro__:
            if "NUMERIC_ESCAPES" not in vars(owner):
                continue
            path = owner.__module__.replace(".", "/") + ".py"
            if path not in coverage["source_hashes"]:
                raise RuntimeError("No source hash for " + path)
            data = (root / path).read_bytes()
            if hashlib.sha256(data).hexdigest() != coverage["source_hashes"][path]:
                raise RuntimeError(path + " differs from pinned source")
            tree = ast.parse(data)
            for name in owner.__qualname__.split("."):
                tree = next(
                    n
                    for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.name == name
                )
            for node in tree.body:
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                    if isinstance(node, ast.AnnAssign)
                    else []
                )
                if not any(
                    isinstance(t, ast.Name) and t.id == "NUMERIC_ESCAPES"
                    for t in targets
                ):
                    continue
                val = node.value
                if not isinstance(val, ast.Dict):
                    continue
                rows = {}
                for key, value in zip(val.keys, val.values):
                    rows[ast.literal_eval(key)] = (
                        f"{key.lineno}:{key.col_offset}:{value.end_lineno}:{value.end_col_offset}"
                    )
                symbols[path, owner.__qualname__] = rows
                old = vars(owner)["NUMERIC_ESCAPES"]
                if not isinstance(old, Reads):
                    wrapped = Reads(old, (path, owner.__qualname__))
                    owner.NUMERIC_ESCAPES = wrapped
                    replacements[id(old)] = (old, wrapped)
            break

    # A tokenizer instance can cache the original dictionary before instrumentation.
    # Redirect those instances at each value-consuming method. An unknown nonempty
    # map means this baseline cannot certify complete observation.
    from sqlglot.tokenizer_core import TokenizerCore
    from functools import wraps

    def check_mapping(fn):
        @wraps(fn)
        def checked(core, *args, **kwargs):
            config = core.numeric_escapes
            if config and not isinstance(config, Reads):
                original, wrapper = replacements[id(config)]
                if original is not config:
                    raise RuntimeError("Unexpected dictionary identity")
                core.numeric_escapes = wrapper
            return fn(core, *args, **kwargs)

        return checked

    for consumer in ("_scan_numeric_escape", "_read_byte_escape"):
        fn = getattr(TokenizerCore, consumer)
        setattr(TokenizerCore, consumer, check_mapping(fn))

    class Recorder:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_protocol(self, item, nextitem):
            nonlocal current
            observed.add(item.nodeid)
            current = [item.nodeid]
            try:
                yield
            finally:
                current = None

        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_setup(self, item):
            nonlocal current
            previous = current
            current = [
                t for t in tests if t.startswith(item.nodeid.rsplit("::", 1)[0] + "::")
            ]
            try:
                yield
            finally:
                current = previous

    # Match process serialization with the baseline coverage collector.
    from build_map import InlinePool

    futures.ProcessPoolExecutor = InlinePool
    result = pytest.main(
        ["tests", "-q", "-p", "no:cacheprovider", "--tb=short"], plugins=[Recorder()]
    )
    if result:
        raise RuntimeError("Baseline dependency collection failed")
    if set(tests) != observed:
        raise RuntimeError("Baseline test inventory changed")
    for site, rows in symbols.items():
        path, _ = site
        for key, region in rows.items():
            emitted[path, region].update(lookup[site, key])
    extra = {}
    for (path, region), readers in sorted(emitted.items()):
        extra.setdefault(path, {})[region] = sorted(readers)
    output.write_text(
        json.dumps(
            {
                "commit": PIN,
                "value_reads": extra,
                "baseline_tests_examined": len(observed),
            },
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
