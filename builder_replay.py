"""Replay closed expression builder calls against observed immutable inputs."""

import ast
from functools import lru_cache
import json
from pathlib import Path
import sys
import tempfile

from forms import FACTORIES, normalize, span
from fingerprint import fingerprint
import forkprobe


@lru_cache(maxsize=1)
def records():
    path = Path(__file__).with_name("replay.json")
    return json.loads(path.read_bytes())


def matching_change(old, new, equal):
    """Find a single changed builder expression, preserving all other code."""
    if equal(old, new):
        return []
    if type(old) is not type(new):
        raise ValueError
    if isinstance(old, ast.Call):
        try:
            old_form, old_leaves = normalize(old)
            new_form, new_leaves = normalize(new)
        except ValueError:
            pass
        else:
            if not equal(old_leaves, new_leaves):
                raise ValueError
            if len(old_form) > 1800 or len(new_form) > 1800 or len(old_leaves) > 12:
                raise ValueError

            # With the same calls and operand evaluation order, only primitive
            # arguments or keyword syntax can differ inside the closed tree.
            def calls(node):
                return [n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call)]

            if calls(old) != calls(new):
                raise ValueError
            return [(span(old), old_form, new_form)]
    if isinstance(old, ast.AST):
        changes = []
        for name in old._fields:
            changes.extend(
                matching_change(getattr(old, name), getattr(new, name), equal)
            )
        return changes
    if isinstance(old, list) and len(old) == len(new):
        changes = []
        for left, right in zip(old, new):
            changes.extend(matching_change(left, right, equal))
        return changes
    raise ValueError


def _evaluate(node, values, exp):
    if isinstance(node, ast.Call) and node.func.attr in FACTORIES:
        func = getattr(exp, node.func.attr)
        args = [_evaluate(arg, values, exp) for arg in node.args]
        kwargs = {k.arg: _evaluate(k.value, values, exp) for k in node.keywords}
        return func(*args, **kwargs)
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name) and node.id.startswith("_v"):
        return values[int(node.id[2:])]
    raise ValueError


def _run(request):
    root, old_form, new_form, rows, out = request
    sys.path.insert(0, str(root))
    from sqlglot import exp

    old = ast.parse(old_form, mode="eval").body
    new = ast.parse(new_form, mode="eval").body
    status = {}
    for patterns in rows.values():
        for pattern in patterns:
            if pattern in status:
                continue
            try:
                values, observed = json.loads(pattern)
                left = fingerprint(_evaluate(old, values, exp))
                right = fingerprint(_evaluate(new, values, exp))
                status[pattern] = left == right == observed
            except (ValueError, TypeError, KeyError, AttributeError, IndexError):
                status[pattern] = False
    passed = [
        n
        for n, patterns in rows.items()
        if patterns and all(status[p] for p in patterns)
    ]
    out.write_text(json.dumps(passed))


def equivalent_tests(path, old, new, root, selector, candidates, equal):
    by_site = records().get(path, {})
    if not by_site:
        return set()
    try:
        changes = matching_change(old, new, equal)
    except (ValueError, TypeError, AttributeError, RecursionError):
        return set()
    if len(changes) != 1:
        return set()
    key, old_form, new_form = changes[0]
    baseline = by_site.get(key)
    if not baseline or baseline[0] != old_form:
        return set()
    variants = baseline[1]
    rows = {
        n: variants[name]
        for n in candidates
        if (name := selector.by_number[n]["id"]) in variants
    }
    if not rows:
        return set()
    with tempfile.TemporaryDirectory(prefix="br-builder-") as work:
        out = Path(work) / "equal.json"
        forkprobe.run([root, old_form, new_form, rows, out], {}, _run, 1.5)
        if out.is_file():
            return set(json.loads(out.read_text())) & rows.keys()
    return set()


def independent_of_companions(selector, files, tests, apply, equal):
    """Keep a proof only if all other changes leave this test's inputs untouched."""
    from source_fast import local_function

    for edit in files:
        if not tests:
            break
        path = edit["old"]
        if path == "/dev/null" or edit["new"] != path:
            return set()
        source, tree = selector.source(path)
        updated, removed, inserted = apply(source, edit)
        if updated == source:
            continue
        # Reading raw bytes can make even a Python comment observable.
        if path in selector.files:
            tests.difference_update(selector.sets[selector.files[path]])
        if tree is None:
            if path not in selector.files:
                return set()
            tests.difference_update(selector.file_tests(path))
            continue
        before = source.splitlines(keepends=True)
        after = updated.splitlines(keepends=True)
        changed = removed | inserted
        try:
            compile(updated, path, "exec")
            bodies = local_function(tree, before, after, changed)
            if bodies is None:
                if equal(tree, ast.parse(updated)):
                    continue
                return set()
            first, second = bodies
            if equal(first, second):
                continue
            if path.startswith(("sqlglot/expressions/", "tests/")):
                return set()
            if not first.body or not second.body:
                return set()
            if ast.get_docstring(first, clean=False) != ast.get_docstring(
                second, clean=False
            ):
                return set()
            if not changed or min(changed) < first.body[0].lineno:
                return set()
            if any(
                not equal(getattr(first, field), getattr(second, field))
                for field in first._fields
                if field != "body"
            ):
                return set()
            # Body entry is traced even when individual bytecode paths have
            # no separate rows, except for completely unobserved functions.
            callers = selector.line_tests(
                path, range(first.lineno, first.end_lineno + 1)
            )
            if not callers:
                return set()
            tests.difference_update(callers)
        except (ValueError, TypeError, SyntaxError, RecursionError):
            return set()
    return tests
