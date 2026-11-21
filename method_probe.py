"""Recognize isolated edits to existing unittest method bodies."""

import ast


def _same_declaration(left, right, equal):
    return type(left) is type(right) and all(
        equal(getattr(left, field), getattr(right, field))
        for field in left._fields
        if field != "body"
    )


def _candidate(old, new, equal):
    if not isinstance(old, ast.FunctionDef) or not old.name.startswith("test_"):
        return None
    if not _same_declaration(old, new, equal):
        return None
    # Test methods can select an assertion using runner state. Such a method
    # gets its normal coverage selection under the pytest scorer.
    if any(
        (
            isinstance(node, ast.Name)
            and node.id in {"sys", "os", "pytest", "inspect", "importlib"}
        )
        or (
            isinstance(node, ast.Attribute)
            and node.attr in {"__file__", "__globals__", "modules", "environ"}
        )
        for node in ast.walk(new)
    ):
        return False
    body = new.body
    if ast.get_docstring(new) is not None:
        body = body[1:]
    # A run cannot confirm this method passed when its first statement raises.
    # Keep the existing coverage selection and avoid paying for that probe.
    return not body or not isinstance(body[0], ast.Raise)


def direct_methods(tree, old, new, equal, path, catalog):
    """Return candidate numbers, or None when edits reach another scope."""
    result = set()
    if isinstance(old, ast.FunctionDef):
        if (worth_probe := _candidate(old, new, equal)) is None:
            return None
        for cls in tree.body:
            if isinstance(cls, ast.ClassDef) and any(
                member is old for member in cls.body
            ):
                number = catalog.get(f"{path}::{cls.name}::{old.name}")
                if number is None:
                    return None
                return {number} if worth_probe else set()
        return None
    if not isinstance(old, ast.Module) or len(old.body) != len(new.body):
        return None
    for a, b in zip(old.body, new.body):
        if equal(a, b):
            continue
        if not isinstance(a, ast.ClassDef) or not _same_declaration(a, b, equal):
            return None
        if len(a.body) != len(b.body):
            return None
        for left, right in zip(a.body, b.body):
            if equal(left, right):
                continue
            worth_probe = _candidate(left, right, equal)
            if worth_probe is None:
                return None
            number = catalog.get(f"{path}::{a.name}::{left.name}")
            if number is None:
                return None
            if worth_probe:
                result.add(number)
    return result


class MissingReferencePackage:
    """Watch unresolved imports, including optional imports a test catches."""

    def __init__(self, reference):
        self.reference = reference
        self.missing = set()

    def find_spec(self, fullname, path, target=None):
        # The guard is appended *after* normal finders. If they returned a spec,
        # importlib never reaches this method.
        if fullname.partition(".")[0] in self.reference:
            self.missing.add(fullname)
        return None
