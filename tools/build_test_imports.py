"""Compute dependencies between the pinned tests, including imports through helpers."""

import ast
import json
from collections import defaultdict
from importlib.util import resolve_name
from pathlib import Path
import subprocess
import sys


def build(root, output):
    rev = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if rev != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Unexpected source revision")
    files = subprocess.check_output(
        ["git", "-C", root, "ls-tree", "-r", "--name-only", rev, "tests"], text=True
    )
    modules = {
        p[:-3].replace("/", ".").removesuffix(".__init__"): p
        for p in files.splitlines()
        if p.endswith(".py")
    }
    dependents = defaultdict(set)
    for module, path in modules.items():
        source = subprocess.check_output(["git", "-C", root, "show", f"{rev}:{path}"])
        parent = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                deps = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                name = (
                    resolve_name("." * node.level + (node.module or ""), parent)
                    if node.level
                    else node.module or ""
                )
                deps = [name] + [f"{name}.{a.name}" for a in node.names]
            else:
                continue
            for dep in deps:
                if dep in modules and dep != module:
                    dependents[dep].add(module)
    result = {}
    for name, path in modules.items():
        found, pending = set(), list(dependents[name])
        while pending:
            dep = pending.pop()
            if dep not in found:
                found.add(dep)
                pending.extend(dependents[dep] - found)
        files = sorted(
            modules[d]
            for d in found - {name}
            if Path(modules[d]).name.startswith("test_")
        )
        if files:
            result[path] = files
    output.write_text(
        json.dumps(
            {"commit": rev, "importers": result}, sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
