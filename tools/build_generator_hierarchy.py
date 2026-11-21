"""Describe effect-checked, inherited SQL generators and their observation sites on the pin."""

import ast
from collections import defaultdict
from importlib import import_module
from importlib.util import resolve_name
import inspect
import json
from pathlib import Path
import subprocess
import sys

from build_inherited_methods import ordinary_effects, toplevel_use_index


def build(root, output):
    root, output = map(lambda p: Path(p).resolve(), (root, output))
    pin = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != pin
    ):
        raise ValueError("Wrong checkout")
    sys.path.insert(0, str(root))
    from sqlglot.generator import Generator
    from sqlglot import exp

    if (
        Path(inspect.getsourcefile(Generator)).resolve()
        != root / "sqlglot/generator.py"
    ):
        raise ValueError("Wrong module")
    strkeys, escapes, reflection = toplevel_use_index(root)
    dirs = sorted((root / "sqlglot/generators").glob("*.py"))
    files = [root / "sqlglot/generator.py", *dirs]
    removable = {p.relative_to(root).as_posix() for p in dirs}
    modules = {
        p.relative_to(root).as_posix()[:-3].replace("/", "."): p.relative_to(
            root
        ).as_posix()
        for p in dirs
    }
    # Removing a redundant base-class import must preserve public aliases
    # used from that dialect module, including direct and wildcard imports.
    for folder in ("tests", "sqlglot"):
        for p in (root / folder).rglob("*.py"):
            for n in ast.walk(ast.parse(p.read_bytes())):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name in modules:
                            removable.discard(modules[a.name])
                if isinstance(n, ast.ImportFrom):
                    origin = n.module
                    if n.level:
                        scope = p.relative_to(root).as_posix()[:-3].split("/")[:-1]
                        try:
                            origin = resolve_name(
                                "." * n.level + (n.module or ""), ".".join(scope)
                            )
                        except (ImportError, ValueError):
                            continue
                    if origin in modules and any(
                        a.name in ("Generator", "*") for a in n.names
                    ):
                        removable.discard(modules[origin])
                    if origin == "sqlglot.generators":
                        for a in n.names:
                            if origin + "." + a.name in modules:
                                removable.discard(modules[origin + "." + a.name])
                if (
                    isinstance(n, ast.Constant)
                    and type(n.value) is str
                    and n.value in modules
                ):
                    removable.discard(modules[n.value])
    out = {}
    names = set()
    purity = {}
    for p in files:
        path = p.relative_to(root).as_posix()
        module = import_module(path[:-3].replace("/", "."))
        for cls in vars(module).values():
            if (
                not isinstance(cls, type)
                or cls.__module__ != module.__name__
                or not issubclass(cls, Generator)
                or type(cls) is not type
            ):
                continue
            methods = {}
            for name, fn in vars(cls).items():
                if (
                    not name.endswith("_sql")
                    or name.startswith("_")
                    or name in strkeys
                    or not inspect.isfunction(fn)
                    or fn.__qualname__ != cls.__name__ + "." + name
                ):
                    continue
                expr = exp.EXPR_CLASSES.get(name[:-4])
                if expr is None:
                    continue
                chain = [
                    base.__dict__[name] for base in cls.__mro__ if name in vars(base)
                ]
                if any(
                    not inspect.isfunction(f) or not f.__module__.startswith("sqlglot.")
                    for f in chain
                ):
                    continue
                for f in chain:
                    if f not in purity:
                        purity[f] = (
                            ordinary_effects(f)
                            and f.__code__.co_qualname == f.__qualname__
                        )
                if not all(purity[f] for f in chain):
                    continue
                methods[name] = [expr.__name__, len(chain) > 1]
                names.add(name)
            if methods:
                out.setdefault(path, {})[cls.__name__] = methods
    mentions = {
        name: {path: sorted(nums) for path, nums in escapes.get(name, {}).items()}
        for name in sorted(names)
        if escapes.get(name)
    }
    output.write_text(
        json.dumps(
            dict(
                pin=pin,
                types=out,
                noncall=mentions,
                unreferenced_base_import=sorted(removable),
                reflected=reflection,
            ),
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
