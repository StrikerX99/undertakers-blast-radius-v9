"""Dependency and compiler checks on the single pinned revision."""

import ast
import dis
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import tempfile
import types


@lru_cache(maxsize=4)
def profile(filename):
    return json.loads(Path(__file__).with_name(filename).read_bytes())


def pinned(table, selector, field, path, source):
    digest = table.get(field, {}).get(path)
    return bool(
        digest
        and table["commit"] == selector.mapping["commit"]
        and digest == hashlib.sha256(source.encode()).hexdigest()
    )


def unread_data(selector, changes, path, original):
    # A concurrent code or observed fixture edit could introduce a new reader.
    # Accept only patches consisting entirely of previously untouched data files.
    if path in selector.files or not changes:
        return False
    data = profile("unread-data.json")
    if not pinned(data, selector, "unread", path, original):
        return False
    return all(
        c["old"] == c["new"]
        and c["old"] in data["unread"]
        and c["old"] not in selector.files
        for c in changes
    )


# These instructions read an already initialized local, an immutable literal,
# or perform a non-dispatching stack/identity operation. A traceback cannot
# originate from a Python comparison override at any of these instructions.
_POSITION_ONLY = {
    "CACHE",
    "COPY",
    "EXTENDED_ARG",
    "IS_OP",
    "JUMP_FORWARD",
    "LOAD_CONST",
    "LOAD_FAST",
    "LOAD_FAST_BORROW",
    "LOAD_SMALL_INT",
    "NOP",
    "NOT_TAKEN",
    "POP_JUMP_IF_NONE",
    "POP_JUMP_IF_NOT_NONE",
    "RESUME",
    "SWAP",
}
_CODE_FIELDS = (
    "co_argcount",
    "co_posonlyargcount",
    "co_kwonlyargcount",
    "co_nlocals",
    "co_stacksize",
    "co_flags",
    "co_code",
    "co_names",
    "co_varnames",
    "co_filename",
    "co_name",
    "co_qualname",
    "co_firstlineno",
    "co_freevars",
    "co_cellvars",
    "co_exceptiontable",
)


def same_code(a, b):
    import marshal

    if any(getattr(a, f) != getattr(b, f) for f in _CODE_FIELDS):
        return False
    if len(a.co_consts) != len(b.co_consts):
        return False
    for x, y in zip(a.co_consts, b.co_consts):
        if type(x) is not type(y):
            return False
        if isinstance(x, types.CodeType):
            if not same_code(x, y):
                return False
        elif marshal.dumps(x) != marshal.dumps(y):
            return False
    if a.co_linetable == b.co_linetable:
        return True
    for offset, (x, y) in enumerate(
        zip(a.co_positions(), b.co_positions(), strict=True)
    ):
        if x != y and (
            x[:2] != y[:2] or dis.opname[a.co_code[2 * offset]] not in _POSITION_ONLY
        ):
            return False
    return True


def equivalent_guard(selector, changes, selected, apply):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/")
        or not path.endswith(".py")
    ):
        return selected
    edited = [s for h in change["hunks"] for s in h["lines"] if s[:1] in "+-"]
    if not (len(edited) == 2 and any("not " in line for line in edited)):
        return selected
    source, _ = selector.source(path)
    if path in selector.files or not pinned(
        profile("unopened-sources.json"), selector, "unopened", path, source
    ):
        return selected
    after, _, _ = apply(source, change)
    before_code = compile(source.encode(), path, "exec", dont_inherit=True)
    after_code = compile(after.encode(), path, "exec", dont_inherit=True)
    return set() if same_code(before_code, after_code) else selected


def _confirm_missing_symbol(payload):
    import importlib
    import sys

    root, module, producer, previous, replacement, marker = payload
    os.chdir(root)
    sys.path.insert(0, str(root))
    try:
        importlib.import_module(module)
    except ImportError as exc:
        imported = sys.modules.get(producer)
        if imported is None or exc.name != producer or module in sys.modules:
            return
        namespace = vars(imported)
        if (
            previous in namespace
            and replacement not in namespace
            and "__getattr__" not in namespace
        ):
            marker.touch()


def can_narrow_failed_import(selector, changes, remaining):
    if len(changes) != 1 or selector.unknown or remaining < 2:
        return False
    change = changes[0]
    path = change["old"]
    return (
        path == change["new"]
        and path.startswith("sqlglot/dialects/")
        and path not in selector.files
    )


def unaffected_by_import_failure(
    selector, changes, selected, apply, equal, checkout, remaining
):
    # Restrict to a renamed binding in one top-level from-import. The same
    # dependency is imported first, and the rest of the dialect is unchanged.
    if not can_narrow_failed_import(selector, changes, remaining):
        return selected
    change = changes[0]
    path = change["old"]
    data = profile("import-footprints.json")
    source, tree = selector.source(path)
    if not (
        pinned(data, selector, "sources", path, source)
        and pinned(profile("unopened-sources.json"), selector, "unopened", path, source)
    ):
        return selected
    after, _, _ = apply(source, change)
    updated = ast.parse(after)
    if len(tree.body) != len(updated.body):
        return selected
    edits = [
        (i, a, b)
        for i, (a, b) in enumerate(zip(tree.body, updated.body))
        if not equal(a, b)
    ]
    if len(edits) != 1:
        return selected
    i, a, b = edits[0]
    prefix = tree.body[:i]
    if prefix and ast.get_docstring(tree) is not None:
        prefix = prefix[1:]
    if not (
        all(isinstance(s, (ast.Import, ast.ImportFrom)) for s in prefix)
        and isinstance(a, ast.ImportFrom)
        and isinstance(b, ast.ImportFrom)
        and a.module == b.module
        and a.level == b.level == 0
        and a.module
        and a.module.startswith(("sqlglot.generators.", "sqlglot.parsers."))
        and len(a.names) == len(b.names) == 1
        and a.names[0].name != b.names[0].name
        and (a.names[0].asname or a.names[0].name)
        == (b.names[0].asname or b.names[0].name)
    ):
        return selected
    independent = {
        selector.by_name[test]
        for test, absent in data["absent"].items()
        if test in selector.by_name and path in absent
    }
    if not independent:
        return selected
    import forkprobe

    with tempfile.TemporaryDirectory(prefix="br-failed-import-") as tmp:
        root, marker = Path(tmp) / "repo", Path(tmp) / "missing"
        checkout(root, changes, selector)
        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        payload = (
            root,
            path[:-3].replace("/", "."),
            a.module,
            a.names[0].name,
            b.names[0].name,
            marker,
        )
        ok = forkprobe.run(payload, env, _confirm_missing_symbol, min(2, remaining))
        return selected - independent if ok and marker.is_file() else selected
