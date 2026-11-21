"""Identify ordinary pinned public function bindings and import-time closure builders."""

import ast
import hashlib
import importlib
import json
from pathlib import Path
import subprocess
import sys
import types


def delayed(fn):
    """Admit a closure only if calling the outer function cannot execute user code."""
    body = fn.body[:]
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and type(body[0].value.value) is str
    ):
        body = body[1:]
    if not body or not isinstance(body[-1], ast.Return) or fn.decorator_list:
        return False

    def safe_default(arg):
        return arg is None or isinstance(arg, ast.Constant)

    if any(
        isinstance(p, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom))
        for p in body
    ):
        return False
    result = body[-1].value
    if len(body) == 1 and isinstance(result, ast.Lambda):
        return all(
            safe_default(d) for d in [*result.args.defaults, *result.args.kw_defaults]
        )
    if (
        len(body) == 2
        and isinstance(body[0], ast.FunctionDef)
        and isinstance(result, ast.Name)
    ):
        child = body[0]
        return (
            child.name == result.id
            and not child.decorator_list
            and not child.type_params
            and all(
                safe_default(d) for d in [*child.args.defaults, *child.args.kw_defaults]
            )
        )
    return False


def main(root, out):
    root = Path(root).resolve()
    pin = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if pin != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError(pin)
    sys.path.insert(0, str(root))
    from sqlglot import exp

    if not Path(exp.__file__).resolve().is_relative_to(root):
        raise ValueError("Unexpected package")
    names = sorted(
        n
        for n, o in vars(exp).items()
        if n[:1].isupper() and isinstance(o, type) and issubclass(o, exp.Expr)
    )
    parsed = {}
    for py in sorted((root / "sqlglot").rglob("*.py")):
        if not (
            py.parent.name in {"generators", "parsers"}
            or py.relative_to(root).as_posix()
            in (
                "sqlglot/transforms.py",
                "sqlglot/dialects/dialect.py",
                "sqlglot/generator.py",
                "sqlglot/parser.py",
                "sqlglot/helper.py",
            )
        ):
            continue
        if py.name.startswith("__"):
            continue
        key = py.relative_to(root).with_suffix("").as_posix().replace("/", ".")
        mod = importlib.import_module(key)
        if Path(mod.__file__).resolve() != py:
            raise ValueError(key)
        tree = ast.parse(py.read_bytes())
        functions = []
        factories = []
        for n in tree.body:
            fn = vars(mod).get(getattr(n, "name", None))
            if (
                isinstance(n, ast.FunctionDef)
                and type(fn) is types.FunctionType
                and fn.__module__ == key
                and fn.__qualname__ == n.name
                and Path(fn.__code__.co_filename)
                .resolve()
                .is_relative_to(root / "sqlglot")
            ):
                functions.append(n.name)
                if (
                    Path(fn.__code__.co_filename).resolve() == py
                    and delayed(n)
                    and any(
                        isinstance(x, ast.ImportFrom)
                        and x.module == "__future__"
                        and any(a.name == "annotations" for a in x.names)
                        for x in tree.body
                    )
                ):
                    factories.append(n.name)
        parsed[key] = {
            "functions": sorted(set(functions)),
            "factories": sorted(set(factories)),
            "sha256": hashlib.sha256(py.read_bytes()).hexdigest(),
        }
    data = {"pin": pin, "expressions": names, "modules": parsed}
    Path(out).write_text(json.dumps(data, sort_keys=True, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    main(*sys.argv[1:])
