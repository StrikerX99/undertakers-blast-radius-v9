"""Restrict replay to nested, detached SQLGlot AST builders with immutable leaves."""

import ast

FACTORIES = {
    "table_",
    "column",
    "to_identifier",
    "Identifier",
    "Table",
    "Column",
    "Literal",
    "TableAlias",
    "Alias",
    "Paren",
    "alias_",
}
LITERALS = (str, int, bool, type(None))


def normalize(call):
    if not isinstance(call, ast.Call):
        raise ValueError
    leaves = []

    def visit(n):
        if isinstance(n, ast.Call):
            if (
                not isinstance(n.func, ast.Attribute)
                or not isinstance(n.func.value, ast.Name)
                or n.func.value.id != "exp"
                or n.func.attr not in FACTORIES
                or any(k.arg is None for k in n.keywords)
            ):
                raise ValueError
            # alias_ can parse raw SQL strings; only permit already-owned expressions.
            if n.func.attr == "alias_" and (
                not n.args or not isinstance(n.args[0], ast.Call)
            ):
                raise ValueError
            return ast.copy_location(
                ast.Call(
                    func=n.func,
                    args=[visit(a) for a in n.args],
                    keywords=[
                        ast.keyword(arg=k.arg, value=visit(k.value)) for k in n.keywords
                    ],
                ),
                n,
            )
        if isinstance(n, ast.Constant) and type(n.value) in LITERALS:
            return n
        if isinstance(n, (ast.Name, ast.Attribute)):
            item = n
            while isinstance(item, ast.Attribute):
                item = item.value
            if not isinstance(item, ast.Name):
                raise ValueError
            i = len(leaves)
            leaves.append(n)
            return ast.copy_location(ast.Name(id=f"_v{i}", ctx=ast.Load()), n)
        raise ValueError

    body = visit(call)
    if not any(
        isinstance(a, ast.Call)
        for a in list(call.args) + [k.value for k in call.keywords]
    ):
        raise ValueError
    return ast.unparse(body), leaves


def span(n):
    return f"{n.lineno}:{n.col_offset}:{n.end_lineno}:{n.end_col_offset}"
