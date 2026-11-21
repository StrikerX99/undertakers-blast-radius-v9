"""Locate inserted statements whose execution requires reaching a known baseline line."""

import ast


# These additions can change the meaning of code anywhere in the enclosing
# function, even if execution never reaches the added statement.
SCOPE_CHANGES = (
    ast.Store,
    ast.Del,
    ast.Yield,
    ast.YieldFrom,
    ast.Await,
    ast.NamedExpr,
    ast.Global,
    ast.Nonlocal,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Lambda,
    ast.Import,
    ast.ImportFrom,
    ast.ExceptHandler,
    ast.Match,
)


def _guarded_statements(old, new, first, last, in_function=False):
    if old is new or type(old) is not type(new):
        return
    if isinstance(old, (ast.ClassDef, ast.Lambda)):
        in_function = False
    if isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)):
        in_function = True
    for field, old_value in ast.iter_fields(old):
        new_value = getattr(new, field)
        if isinstance(old_value, list) and isinstance(new_value, list):
            count = len(new_value) - len(old_value)
            if count > 0 and in_function and field in ("body", "orelse", "finalbody"):
                for i, anchor in enumerate(old_value):
                    if not isinstance(anchor, ast.stmt) or anchor.lineno != first:
                        continue
                    added = new_value[i : i + count]
                    if all(
                        isinstance(
                            n, (ast.If, ast.Raise, ast.Assert, ast.Return, ast.Expr)
                        )
                        and first <= n.lineno <= n.end_lineno <= last
                        for n in added
                    ):
                        yield new_value, i, added, anchor
            elif count == 0:
                for a, b in zip(old_value, new_value):
                    if isinstance(a, ast.AST) and getattr(
                        a, "lineno", first
                    ) <= first <= getattr(a, "end_lineno", first):
                        yield from _guarded_statements(a, b, first, last, in_function)
        elif isinstance(old_value, ast.AST) and isinstance(new_value, ast.AST):
            yield from _guarded_statements(
                old_value, new_value, first, last, in_function
            )


def inserted_before(old, new, first, line_count, equal):
    """Return physical lines covering the original next statement's evaluation.

    This only accepts a single, pure insertion before an executable sibling in
    a function. Removing the proposed added nodes must restore the baseline AST.
    """
    for suite, index, added, anchor in _guarded_statements(
        old, new, first, first + line_count - 1
    ):
        if any(isinstance(n, SCOPE_CHANGES) for stmt in added for n in ast.walk(stmt)):
            continue
        # For example, an added string as the first statement can alter __doc__.
        if any(
            isinstance(stmt, ast.Expr) and not isinstance(stmt.value, ast.Call)
            for stmt in added
        ):
            continue
        if isinstance(anchor, (ast.Return, ast.Assign, ast.Expr)):
            evaluated = anchor.value
        elif isinstance(anchor, ast.Raise):
            evaluated = anchor.exc
        elif isinstance(anchor, ast.If):
            evaluated = anchor.test
        else:
            continue
        # Restrict to expressions which execute bytecode within the anchor.
        # A literal condition or a bare expression can be optimized away.
        first_eval = evaluated
        while isinstance(first_eval, ast.BoolOp):
            first_eval = first_eval.values[0]
        if not isinstance(
            first_eval, (ast.Call, ast.Name, ast.Attribute, ast.Subscript, ast.Compare)
        ):
            continue
        del suite[index : index + len(added)]
        try:
            if equal(old, new):
                return range(anchor.lineno, evaluated.end_lineno + 1)
        finally:
            suite[index:index] = added
    return None
