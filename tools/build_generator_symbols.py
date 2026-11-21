"""Index source-visible uses and passive import bindings on a pinned SQLGlot checkout."""

import ast
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def build(root, output):
    root = Path(root).resolve()
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
    ):
        raise ValueError("Unexpected SQLGlot revision")
    if subprocess.run(
        ["git", "-C", root, "diff", "--quiet", "HEAD", "--", "sqlglot", "tests"]
    ).returncode:
        raise ValueError("Expected unmodified SQLGlot sources")
    definitions = set()
    private_observers = defaultdict(set)
    imports = defaultdict(set)
    constants = {}
    hashes = {}
    external = defaultdict(set)
    for part in ("sqlglot", "tests"):
        for file in sorted((root / part).rglob("*.py")):
            path = file.relative_to(root).as_posix()
            text = file.read_bytes()
            tree = ast.parse(text)
            bound = Counter()
            simple = {}
            for node in tree.body:
                for target in (
                    [node]
                    if isinstance(node, (ast.FunctionDef, ast.ClassDef))
                    else ast.walk(node)
                ):
                    if isinstance(target, (ast.FunctionDef, ast.ClassDef)):
                        bound[target.name] += 1
                    elif isinstance(target, ast.Name) and isinstance(
                        target.ctx, (ast.Store, ast.Del)
                    ):
                        bound[target.id] += 1
                    elif isinstance(target, ast.alias):
                        bound[target.asname or target.name.split(".")[0]] += 1
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                    if (
                        len(targets) == 1
                        and isinstance(targets[0], ast.Name)
                        and isinstance(node.value, ast.Constant)
                        and type(node.value.value) is str
                    ):
                        simple[targets[0].id] = node.value.value
            if part == "sqlglot":
                safe = sorted(
                    name for name in simple if bound[name] == 1 and name.isupper()
                )
                if safe:
                    constants[path[:-3].replace("/", ".")] = safe
            if path.startswith("sqlglot/generators/"):
                hashes[path] = hashlib.sha256(text).hexdigest()
            for n in ast.walk(tree):
                if isinstance(
                    n, (ast.FunctionDef, ast.AsyncFunctionDef)
                ) and n.name.startswith("_"):
                    definitions.add(n.name)
                if isinstance(n, ast.Attribute):
                    name = n.attr
                elif (
                    isinstance(n, ast.Constant)
                    and type(n.value) is str
                    and n.value.isidentifier()
                ):
                    name = n.value
                else:
                    name = None
                if name and name.startswith("_") and not name.startswith("__"):
                    private_observers[name].add(path)
                if isinstance(n, ast.ImportFrom) and n.module:
                    for alias in n.names:
                        if n.module.startswith("sqlglot.generators."):
                            imports[n.module].add(alias.name)
                if name and name.isupper():
                    external[name].add(path)
    result = {
        "pin": PIN,
        "sources": hashes,
        "private_attributes": {
            n: sorted(paths) for n, paths in private_observers.items()
        },
        "private_definitions": sorted(definitions),
        "generator_imports": {m: sorted(v) for m, v in imports.items()},
        "constants": constants,
        "public_attribute_users": {
            n: sorted(v)
            for n, v in external.items()
            if any(n in row for row in constants.values())
        },
    }
    Path(output).write_text(
        json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n"
    )


if __name__ == "__main__":
    build(*sys.argv[1:])
