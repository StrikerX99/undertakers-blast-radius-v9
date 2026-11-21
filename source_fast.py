"""Limit repeated Python-level AST traversal to changed source statements."""

import ast
from collections import deque

COMPOUND = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _first_line(stmt):
    decorators = getattr(stmt, "decorator_list", ())
    # Multiple decorators and multiline decorator expressions can start before def.
    return min(stmt.lineno, decorators[0].lineno) if decorators else stmt.lineno


def _unchanged(left, right, before, after):
    if type(left) is not type(right):
        return False
    if (left.col_offset, left.end_col_offset) != (
        right.col_offset,
        right.end_col_offset,
    ):
        return False
    start = _first_line(left) - 1
    new_start = _first_line(right) - 1
    length = left.end_lineno - start
    if (
        length != right.end_lineno - new_start
        or left.lineno - start != right.lineno - new_start
    ):
        return False
    # Whole physical lines also cover delimiters, decorators and indentation.
    # Excluding decorators with unusual '@ (' formatting avoids truncating a
    # multi-line decorator whose expression begins below the @ token.
    if getattr(left, "decorator_list", ()) or getattr(right, "decorator_list", ()):
        return False
    return before[start : left.end_lineno] == after[new_start : right.end_lineno]


def share_unchanged(old, new, before, after):
    """Reuse baseline nodes for equal statements before semantic comparison.

    Work on the newly parsed tree only. This is called after full compilation.
    Headers of changed compound statements still undergo normal AST comparison.
    """
    if len(old.body) != len(new.body):
        return
    for index, (left, right) in enumerate(zip(old.body, new.body)):
        if _unchanged(left, right, before, after):
            new.body[index] = left
        elif isinstance(left, COMPOUND) and type(left) is type(right):
            share_unchanged(left, right, before, after)


def ancestors_at(tree, line):
    """Locate statements and expressions whose spans touch an original source line."""
    result = []
    todo = deque([tree])
    while todo:
        node = todo.popleft()
        if hasattr(node, "lineno"):
            if line < _first_line(node) or node.end_lineno < line:
                continue
            if node.lineno <= line:
                result.append(node)
        todo.extend(ast.iter_child_nodes(node))
    return result


def local_function(tree, old_lines, new_lines, affected_lines):
    """Parse one modified function in isolation when byte boundaries match.

    A full-file compilation is still necessary to detect syntax/closure errors
    in the enclosing module. The caller falls back to parsing the full AST if
    the local syntax crosses a scope boundary or the edits span functions.
    """
    if not affected_lines:
        return None
    first, last = min(affected_lines), max(affected_lines)
    functions = [
        node
        for node in ancestors_at(tree, first)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno < first
        and last < node.end_lineno
    ]
    if not functions:
        return None
    fn = min(functions, key=lambda node: node.end_lineno - node.lineno)
    start = fn.lineno - 1
    old_end = fn.end_lineno
    end = old_end + len(new_lines) - len(old_lines)
    if end <= start or end > len(new_lines):
        return None
    if old_lines[:start] != new_lines[:start] or old_lines[old_end:] != new_lines[end:]:
        return None
    prefix = "if True:\n" if fn.col_offset else ""
    try:
        parsed = ast.parse(prefix + "".join(new_lines[start:end]))
        children = parsed.body[0].body if prefix else parsed.body
    except (AttributeError, IndexError, SyntaxError, ValueError):
        return None
    if len(children) != 1 or type(children[0]) is not type(fn):
        return None
    body = children[0]
    if body.name != fn.name or fn.col_offset != body.col_offset:
        return None
    # Header/decorator bytes before the isolated body are unchanged. Share
    # actual baseline decorator nodes instead of reparsing omitted syntax.
    body.decorator_list = fn.decorator_list
    # Baseline map coordinates come from fn; retaining the new slice's local
    # coordinates is intentional. Comparisons inspect AST fields only.
    return fn, body
