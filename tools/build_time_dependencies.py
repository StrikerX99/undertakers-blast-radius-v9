"""Index consumers of SQLGlot's derived date/time dictionaries and prefix tries.

Uses solely source and dynamic line coverage from the pinned, unchanged revision.
"""

import ast
import hashlib
import json
import sys
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def time_table(name):
    return name.endswith(("TIME_MAPPING", "FORMAT_MAPPING", "TIME_TRIE", "FORMAT_TRIE"))


def gather(root, data):
    names = data["tests"]
    readers = set()
    locations = {}

    def include(path, node):
        rows = data["lines"].get(path, {})
        for line in range(node.lineno, node.end_lineno + 1):
            key = rows.get(str(line))
            if key is not None:
                readers.update(data["sets"][key])

    def walk(path, node, function=None, cls=None):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node
        if isinstance(node, ast.ClassDef):
            cls = node
        if (
            path == "sqlglot/time.py"
            and isinstance(node, ast.FunctionDef)
            and node.name == "format_time"
        ):
            include(path, node)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and function is None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(
                isinstance(t, ast.Name)
                and (time_table(t.id) or t.id == "STRICT_TIME_FORMATS")
                for t in targets
            ) and isinstance(node.value, ast.Dict):
                locations.setdefault(path, []).append(
                    [
                        node.value.lineno,
                        node.value.col_offset,
                        node.value.end_lineno,
                        node.value.end_col_offset,
                    ]
                )
        if (
            isinstance(node, (ast.Attribute, ast.Name))
            and isinstance(node.ctx, ast.Load)
            and function is not None
        ):
            name = node.attr if isinstance(node, ast.Attribute) else node.id
            if time_table(name) or name == "STRICT_TIME_FORMATS":
                if path.startswith("tests/"):
                    include(path, node)
                    test_id = (
                        path + "::" + (cls.name + "::" if cls else "") + function.name
                    )
                    readers.update(
                        i
                        for i, n in enumerate(names)
                        if n == test_id or n.startswith(test_id + "[")
                    )
                elif not (
                    path == "sqlglot/dialects/dialect.py"
                    and (
                        function.name == "__new__"
                        or function.name == "_with_strict_time_inverse"
                    )
                ):
                    include(path, node)
        for child in ast.iter_child_nodes(node):
            walk(path, child, function, cls)

    for path, expected in data["source_hashes"].items():
        if not path.endswith(".py"):
            continue
        blob = (root / path).read_bytes()
        if hashlib.sha256(blob).hexdigest() != expected:
            raise ValueError("Source mismatch: " + path)
        walk(path, ast.parse(blob))
    return {
        "commit": data["commit"],
        "locations": locations,
        "readers": [names[i] for i in sorted(readers)],
    }


if __name__ == "__main__":
    baseline = json.loads(Path(sys.argv[2]).read_bytes())
    if baseline["commit"] != PIN or baseline["baseline_failures"]:
        raise ValueError("Invalid baseline")
    Path(sys.argv[3]).write_text(
        json.dumps(
            gather(Path(sys.argv[1]), baseline), sort_keys=True, separators=(",", ":")
        )
        + "\n"
    )
