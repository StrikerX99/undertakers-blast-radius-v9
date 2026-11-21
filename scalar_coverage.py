"""Use recorded reads of class constants when checking a scalar assignment edit."""

import ast
import json
import operator

OPS = {
    "Eq": operator.eq,
    "NotEq": operator.ne,
    "Is": operator.is_,
    "IsNot": operator.is_not,
    "Lt": operator.lt,
    "LtE": operator.le,
    "Gt": operator.gt,
    "GtE": operator.ge,
}


def same_observation(mode, before, after):
    if mode == "masked":
        return True
    if mode == "raw":
        return False
    if mode == "bool":
        return bool(before) == bool(after)
    op, left, other = json.loads(mode)
    fn = OPS[op]
    try:
        return (
            fn(before, other) == fn(after, other)
            if left
            else fn(other, before) == fn(other, after)
        )
    except (ValueError, TypeError):
        return False


class ScalarCoverage:
    def __init__(self, data, by_name, dispatch):
        if data["commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("Unrecognized revision")
        self.rows = data["rows"]
        self.numbers = {
            i: by_name[name] for i, name in enumerate(data["tests"]) if name in by_name
        }
        self.external = dispatch.external
        self.external_by_source = dispatch.external_by_source

    def changed(self, selector, path, old, new, equal):
        if type(old) is not type(new) or not isinstance(
            old, (ast.Assign, ast.AnnAssign)
        ):
            return None
        row = self.rows.get(path, {}).get(str(old.lineno))
        if not row or any(
            not equal(getattr(old, key, None), getattr(new, key, None))
            for key in old._fields
            if key != "value"
        ):
            return None
        try:
            before = ast.literal_eval(old.value)
            after = ast.literal_eval(new.value)
        except (ValueError, TypeError, SyntaxError):
            return None
        primitive_types = (int, bool, str, type(None), float)
        if type(before) not in primitive_types or type(after) not in primitive_types:
            return None
        original = selector.line_tests(path, range(old.lineno, old.end_lineno + 1))
        known = set()
        remaining = set(self.external)
        remaining.update(self.external_by_source.get(path, ()))
        for mode, (during_import, ids) in row.items():
            same = same_observation(mode, before, after)
            if during_import and not same:
                # A value computed while modules load can reach tests that never
                # read the setting or execute its declaration in their own call.
                return set(selector.all)
            names = {self.numbers[i] for i in ids if i in self.numbers}
            known.update(names)
            if not same:
                remaining.update(names)
        # Keep the first baseline instrumenter's positive dependencies whenever
        # the independent read instrumentation could not attribute them.
        if not original:
            return None
        remaining.update(original - known)
        return remaining
