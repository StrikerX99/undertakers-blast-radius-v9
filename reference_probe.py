"""Verify expected-result SQL for an executor test when its actual-result path is unchanged."""

import ast
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import tempfile

import forkprobe
from query_case import CASE
from workers import schedule


@lru_cache(maxsize=2)
def data(filename):
    return json.loads(Path(__file__).with_name(filename).read_bytes())


def allowed_sql(sql, pieces):
    """Accept exactly the pinned fragments and optional valid outer-key sort modifiers."""
    if not pieces:
        return False
    cursor = 0
    for part in pieces[:-1]:
        if not sql.startswith(part, cursor):
            return False
        cursor += len(part)
        for modifier in (" NULLS FIRST", " NULLS LAST"):
            if sql.startswith(modifier, cursor):
                cursor += len(modifier)
                break
    return sql[cursor:] == pieces[-1]


def eligible_body(old, new, equal):
    """Only literal or comparison operator substitutions inside an existing method."""
    if equal(old, new):
        return True
    if isinstance(old, ast.Constant) and isinstance(new, ast.Constant):
        return type(old.value) is type(new.value) and type(old.value) in (
            bool,
            str,
            int,
        )
    if isinstance(old, ast.cmpop) and isinstance(new, ast.cmpop):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.AST):
        return all(
            eligible_body(getattr(old, k), getattr(new, k), equal) for k in old._fields
        )
    if isinstance(old, list):
        return len(old) == len(new) and all(
            eligible_body(a, b, equal) for a, b in zip(old, new)
        )
    return False


def exclusive_line(profile, path, before, after, tree, removed, inserted, equal):
    """Accept an isolated single-line statement observed only when producing reference SQL."""
    if len(removed) != 1 or removed != inserted or len(before) != len(after):
        return False
    line = next(iter(removed))
    name = profile.get("lines", {}).get(path, {}).get(str(line))
    if (
        not name
        or before[: line - 1] != after[: line - 1]
        or before[line:] != after[line:]
    ):
        return False
    a, b = before[line - 1], after[line - 1]
    if a.partition(a.lstrip())[0] != b.partition(b.lstrip())[0]:
        return False
    # Standalone executable statements avoid labels on multiline constants
    # which can be compiled into another line's instruction. Both expressions
    # must occupy the same complete statement in the original method.
    from source_fast import ancestors_at

    enclosing = ancestors_at(tree, line)
    fn = [
        n
        for n in enclosing
        if isinstance(n, ast.FunctionDef) and n.name == name.rsplit(".", 1)[-1]
    ]
    if len(fn) != 1 or not any(
        isinstance(n, (ast.Return, ast.Assign, ast.Expr))
        and n.lineno == n.end_lineno == line
        for n in enclosing
    ):
        return False
    try:
        a, b = ast.parse(a.strip()).body, ast.parse(b.strip()).body
    except SyntaxError:
        return False
    return (
        len(a) == len(b) == 1
        and type(a[0]) is type(b[0])
        and isinstance(a[0], (ast.Return, ast.Assign, ast.Expr))
        and eligible_body(a[0], b[0], equal)
    )


def eligible(selector, change, apply, equal, profile):
    path = change["old"]
    if path != change["new"] or path not in profile["sources"]:
        return False
    digest = selector.mapping["source_hashes"].get(path)
    if (
        profile["sources"][path] != digest
        or profile["commit"] != selector.mapping["commit"]
    ):
        return False
    from dormant_settings import _literal_assignment, _sources

    raw = _sources()
    if raw["commit"] != profile["commit"] or raw["unopened"].get(path) != digest:
        return False
    case = selector.by_name[CASE]
    external = selector.dispatch_map()
    if (
        case in selector.unknown
        or case in external.external
        or case in external.external_by_source.get(path, ())
        or path in selector.files
        and case in selector.sets[selector.files[path]]
    ):
        return False
    source, tree = selector.source(path)
    updated, removed, inserted = apply(source, change)
    before, after = source.splitlines(True), updated.splitlines(True)
    settings = profile["sites"].get(path, {})
    if (
        len(removed) == len(inserted) == 1
        and (line := next(iter(removed))) == next(iter(inserted))
        and str(line) in settings
    ):
        old = _literal_assignment(before[line - 1])
        new = _literal_assignment(after[line - 1])
        if (
            not old
            or not new
            or old[:2] != new[:2]
            or old[0] != settings[str(line)][1]
            or type(old[2]) is not type(new[2])
            or before[: line - 1] != after[: line - 1]
            or before[line:] != after[line:]
            or before[line - 1].partition(before[line - 1].lstrip())[0]
            != after[line - 1].partition(after[line - 1].lstrip())[0]
            or not all(
                equal(getattr(old[2], k), getattr(new[2], k))
                for k in old[2]._fields
                if k != "value"
            )
        ):
            return False
        # Scalar reads during an import can propagate via persistence into other stages.
        row = selector.scalars and selector.scalars.rows.get(path, {}).get(str(line))
        return bool(
            row and not any(during_import for during_import, ids in row.values())
        )
    if exclusive_line(profile, path, before, after, tree, removed, inserted, equal):
        return True
    from source_fast import local_function

    if not (path == "sqlglot/generator.py" or path.startswith("sqlglot/generators/")):
        return False
    names = profile.get("functions", {}).get(path)
    if not names:
        return False
    pair = local_function(tree, before, after, removed | inserted)
    if pair is None:
        return False
    old, new = pair
    if not all(
        equal(getattr(old, k), getattr(new, k)) for k in old._fields if k != "body"
    ):
        return False
    if old in tree.body and old.name in names:
        return eligible_body(old.body, new.body, equal)
    for cls in tree.body:
        if (
            isinstance(cls, ast.ClassDef)
            and old in cls.body
            and cls.name.endswith("Generator")
            and f"{cls.name}.{old.name}" in names
        ):
            return eligible_body(old.body, new.body, equal)
    return False


def check(root, marker, limit, query_data, proofs):
    os.chdir(root)
    sys.path.insert(0, str(root))
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, limit)
    if proofs["commit"] != query_data["commit"]:
        return
    for name, expected in proofs["files"].items():
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != expected:
            return
    # Import the SQLGlot modules from the native test driver and parse all of
    # its submitted inputs before evaluating reference queries, as the parent
    # process of TestExecutor._mp_execute does.
    from sqlglot import parse_one, transpile
    from sqlglot.executor import env, execute  # noqa: F401
    from sqlglot.executor.python import Python, PythonExecutor  # noqa: F401
    from sqlglot.optimizer import optimize  # noqa: F401
    from sqlglot.planner import Plan  # noqa: F401
    from tests.helpers import load_sql_fixture_pairs

    fixtures = list(load_sql_fixture_pairs("optimizer/tpc-ds/tpc-ds.sql"))
    for _, source, _ in fixtures:
        parse_one(source)
    observed = set()
    for i, (meta, source, _) in enumerate(fixtures):
        if not meta.get("execute"):
            continue
        key = str(i)
        expected = query_data["queries"][key]
        if hashlib.sha256(source.encode()).hexdigest() != expected["sql"]:
            return
        sql = transpile(source, write="duckdb")[0]
        if hashlib.sha256(sql.encode()).hexdigest() != expected[
            "duck_sql"
        ] and not allowed_sql(sql, proofs["patterns"].get(key)):
            return
        observed.add(key)
    if observed == query_data["queries"].keys():
        marker.touch()


def verify(selector, changes, selected, apply, equal, checkout, remaining):
    case = selector.by_name.get(CASE)
    if (
        case not in selected
        or remaining < 4
        or not changes
        or any(not change["old"].startswith("sqlglot/") for change in changes)
    ):
        return selected
    profile = data("reference-phases.json")
    if not all(eligible(selector, change, apply, equal, profile) for change in changes):
        return selected
    others = [selector.by_number[n] for n in selected if n != case]
    saving = schedule(others + [selector.by_number[case]])[1] - schedule(others)[1]
    if saving < 4000:
        return selected
    from query_impact import reference_data

    refs, proofs = reference_data(), data("reference-rows.json")
    limit = min(4, saving / 3000)
    with tempfile.TemporaryDirectory(prefix="br-ref-sql-") as tmp:
        root, marker = Path(tmp) / "repo", Path(tmp) / "verified"
        checkout(root, changes, selector)

        def worker(payload):
            check(*payload)

        env = dict(os.environ, PYTHONPATH=str(root), PYTHONDONTWRITEBYTECODE="1")
        ok = forkprobe.run(
            (root, marker, limit, refs, proofs), env, worker, min(remaining, limit + 2)
        )
        if ok and marker.is_file():
            return selected - {case}
    return selected
