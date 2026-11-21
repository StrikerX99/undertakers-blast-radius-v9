"""Identify changes to Python code that can execute while a module loads."""

import ast
from bisect import bisect_left
from source_fast import ancestors_at

_FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)


def same_initializers(a, b):
    """Compare module/class statements and function declarations, excluding method bodies."""
    if a is b:
        return True
    if type(a) is not type(b):
        return False
    if isinstance(a, ast.AST):
        for field in a._fields:
            if field == "body" and isinstance(a, _FUNCTION):
                # Decorators and metaclasses can inspect the new function's docstring.
                if ast.get_docstring(a, clean=False) != ast.get_docstring(
                    b, clean=False
                ):
                    return False
            elif not same_initializers(
                getattr(a, field, None), getattr(b, field, None)
            ):
                return False
        return True
    if isinstance(a, list):
        return len(a) == len(b) and all(same_initializers(x, y) for x, y in zip(a, b))
    return a == b


def initializer_may_change(old, new, anchors, import_lines):
    if any(line in import_lines for line in anchors) or not same_initializers(old, new):
        return True
    # An inserted statement has no baseline coverage. If it lands in a Python
    # function that the baseline called during an import, recheck that import.
    if not import_lines or not anchors:
        return False
    imported = sorted(import_lines)
    enclosing = {
        node
        for line in anchors
        for node in ancestors_at(old, line)
        if isinstance(node, _FUNCTION)
    }
    for fn in enclosing:
        index = bisect_left(imported, fn.body[0].lineno)
        if index < len(imported) and imported[index] <= fn.end_lineno:
            return True
    return False
