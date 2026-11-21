"""Evaluate which baseline tests entered a changed conditional subexpression."""

import ast


class GatedReads:
    def __init__(self, data, baseline, by_name, external):
        if data["commit"] != baseline["commit"]:
            raise ValueError("Gated reads and baseline disagree")
        self.paths = data["rows"]
        self.sets = data["sets"]
        self.numbers = [by_name.get(name) for name in baseline["tests"]]
        self.external = external

    def readers(self, path, old, new):
        if not isinstance(old, ast.expr) or not isinstance(new, ast.expr):
            return None
        span = f"{old.lineno}:{old.col_offset}:{old.end_lineno}:{old.end_col_offset}"
        index = self.paths.get(path, {}).get(span)
        if index is None:
            return None
        # These constructs can indirectly affect an enclosing function even
        # when control never reaches the expression, through Python's compiler.
        for node in (old, new):
            for child in ast.walk(node):
                if isinstance(
                    child, (ast.NamedExpr, ast.Yield, ast.YieldFrom, ast.Await)
                ) or (
                    isinstance(child, ast.Name) and child.id in ("__class__", "super")
                ):
                    return None
        readers = {
            self.numbers[i] for i in self.sets[index] if self.numbers[i] is not None
        }
        readers.update(self.external.external)
        readers.update(self.external.external_by_source.get(path, ()))
        return readers
