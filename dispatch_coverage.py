"""Use invocation coverage for pure, deferred SQLGlot dispatch expressions."""

import ast


def literal_change(left, right, equal):
    if equal(left, right):
        return True
    try:
        ast.literal_eval(left)
        ast.literal_eval(right)
    except (ValueError, TypeError, SyntaxError):
        return False
    return True


def deferred_edit(left, right, equal):
    if isinstance(left, ast.Lambda) and isinstance(right, ast.Lambda):
        # Defaults and annotations can run while the class is defined.
        return equal(left.args, right.args)
    if not isinstance(left, ast.Call) or not isinstance(right, ast.Call):
        return False
    if not equal(left.func, right.func) or len(left.args) != len(right.args):
        return False
    if len(left.keywords) != len(right.keywords):
        return False
    for a, b in zip(left.keywords, right.keywords):
        if a.arg != b.arg or not literal_change(a.value, b.value, equal):
            return False
    return all(literal_change(a, b, equal) for a, b in zip(left.args, right.args))


class DispatchCoverage:
    def __init__(self, data, id_to_number):
        if data.get("commit") != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
            raise ValueError("Unrecognized dispatch coverage version")
        self.rows = data["regions"]
        self.numbers = {
            i: id_to_number[name]
            for i, name in enumerate(data["tests"])
            if name in id_to_number
        }
        certified = data.get("external_paths", {})
        self.external = {
            self.numbers[i]
            for i in data["external"]
            if i in self.numbers and str(i) not in certified
        }
        self.external_by_source = {}
        for i in data["external"]:
            if i not in self.numbers:
                continue
            for source in certified.get(str(i), []):
                self.external_by_source.setdefault(source, set()).add(self.numbers[i])

    def changed(self, path, before, after, equal):
        """Return None if any edited value lacks a qualified observed origin."""
        rows = self.rows.get(path)
        if not rows or len(before.keys) != len(after.keys):
            return None
        if not all(equal(a, b) for a, b in zip(before.keys, after.keys)):
            return None
        tests = self.external.union(self.external_by_source.get(path, ()))
        count = 0
        for key, old, new in zip(before.keys, before.values, after.values):
            if equal(old, new):
                continue
            if key is None or not deferred_edit(old, new, equal):
                return None
            region = (
                f"{key.lineno}:{key.col_offset}:{old.end_lineno}:{old.end_col_offset}"
            )
            if region not in rows:
                return None
            count += 1
            tests.update(self.numbers[i] for i in rows[region] if i in self.numbers)
        return tests if count else None
