"""Describe pure short-circuit guard candidates and optional parser arguments."""

import ast


def guard(node):
    if not (
        isinstance(node, ast.If)
        and not node.orelse
        and isinstance(node.test, ast.BoolOp)
    ):
        return False
    words = (
        ast.UnaryOp,
        ast.Not,
        ast.And,
        ast.Or,
        ast.BoolOp,
        ast.Name,
        ast.Load,
        ast.Attribute,
    )
    return all(
        isinstance(n, words)
        and (
            not isinstance(n, ast.Attribute)
            or isinstance(n.value, ast.Name)
            and n.value.id == "self"
        )
        for n in ast.walk(node.test)
    ) and any(isinstance(n, ast.Attribute) for n in ast.walk(node.test))


def optional(node):
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_match"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ):
        return None
    for k in node.keywords:
        if k.arg != "expression":
            continue
        v = k.value
        if isinstance(v, ast.Name):
            return k
        if (
            isinstance(v, ast.IfExp)
            and isinstance(v.orelse, ast.Constant)
            and v.orelse.value is None
            and isinstance(v.body, ast.Name)
            and isinstance(v.test, ast.Call)
            and isinstance(v.test.func, ast.Name)
            and v.test.func.id == "isinstance"
            and len(v.test.args) == 2
            and not v.test.keywords
            and isinstance(v.test.args[0], ast.Name)
            and v.test.args[0].id == v.body.id
            and isinstance(v.test.args[1], ast.Attribute)
            and v.test.args[1].attr == "Expr"
            and isinstance(v.test.args[1].value, ast.Name)
            and v.test.args[1].value.id == "exp"
        ):
            return k
    return None


def site(node):
    return f"{node.lineno}:{node.col_offset}"
