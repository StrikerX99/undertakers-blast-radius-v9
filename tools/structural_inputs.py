"""Read inherited schemas, declaration dependencies and extra dispatch-table consumers on the pin."""

import ast
import importlib
from pathlib import Path
import sys


def metadata(root):
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    import sqlglot
    from sqlglot import expressions as exp
    from sqlglot.jsonpath import ALL_JSON_PATH_PARTS

    if Path(sqlglot.__file__).resolve() != root / "sqlglot/__init__.py":
        raise ValueError("Could not inspect the supplied pinned checkout")
    inherited, readers, dependencies = {}, [], {}
    generator_types = {}
    for p in sorted((root / "sqlglot/generators").glob("*.py")):
        if p.name == "__init__.py":
            continue
        name = "sqlglot.generators." + p.stem
        mod = importlib.import_module(name)
        for short, obj in vars(mod).items():
            if isinstance(obj, type) and obj.__module__ == name:
                generator_types.setdefault(short, set()).add(name + ":" + short)
    for p in sorted((root / "sqlglot/expressions").glob("*.py")):
        name = "sqlglot.expressions." + p.stem
        if p.stem == "__init__":
            continue
        mod = importlib.import_module(name)
        for short, obj in vars(mod).items():
            if (
                not isinstance(obj, type)
                or obj.__module__ != name
                or not issubclass(obj, exp.Expr)
            ):
                continue
            for base in obj.__mro__[1:]:
                if "arg_types" in vars(base):
                    schema = vars(base)["arg_types"]
                    if type(schema) is dict and all(
                        type(k) is str and type(v) is bool for k, v in schema.items()
                    ):
                        inherited.setdefault(p.relative_to(root).as_posix(), {})[
                            short
                        ] = list(schema.items())
                    break
    for p in sorted(
        (*((root / "sqlglot").rglob("*.py")), *((root / "tests").rglob("*.py")))
    ):
        path = p.relative_to(root).as_posix()
        if "TRANSFORMS" not in p.read_text():
            continue
        tree = ast.parse(p.read_bytes())
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef) or not path.startswith(
                "sqlglot/generators/"
            ):
                continue
            dest = path[:-3].replace("/", ".") + ":" + cls.name
            for statement in cls.body:
                if not isinstance(statement, ast.Assign) or not any(
                    isinstance(t, ast.Name) and t.id == "TRANSFORMS"
                    for t in statement.targets
                ):
                    continue
                for ref in ast.walk(statement.value):
                    if (
                        isinstance(ref, ast.Attribute)
                        and ref.attr == "TRANSFORMS"
                        and isinstance(ref.value, ast.Name)
                    ):
                        for source in generator_types.get(ref.value.id, ()):
                            dependencies.setdefault(source, set()).add(dest)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Generator._build_dispatch copies TRANSFORMS into the lazy lookup table.
            # Every use of that copy in Generator.sql is covered by the native observer.
            if path == "sqlglot/generator.py" and node.name == "_build_dispatch":
                continue
            for x in ast.walk(node):
                if not isinstance(x, ast.Attribute) or x.attr != "TRANSFORMS":
                    continue
                # Dialect.__new__ strips only JSON path handlers from generated classes.
                # Edits to those keys conservatively keep all numbered tests at runtime.
                if path == "sqlglot/dialects/dialect.py" and node.name == "__new__":
                    continue
                keys = set()
                for cmp in ast.walk(node):
                    if (
                        isinstance(cmp, ast.Compare)
                        and len(cmp.comparators) == 1
                        and cmp.comparators[0] is x
                        and isinstance(cmp.left, ast.Attribute)
                        and isinstance(cmp.left.value, ast.Name)
                        and cmp.left.value.id == "exp"
                    ):
                        keys.add(cmp.left.attr)
                entry = [path, x.lineno, sorted(keys)]
                if entry not in readers:
                    readers.append(entry)
    return {
        "inherited": inherited,
        "direct_reads": readers,
        "copies": {k: sorted(v) for k, v in dependencies.items()},
        "dialect_init_keys": sorted(c.__name__ for c in ALL_JSON_PATH_PARTS),
    }
