"""Fingerprint complete CPython module code from the pinned unmodified source."""

import hashlib
import importlib.util
import marshal
from pathlib import Path
import sys


def fingerprint(code):
    return hashlib.sha256(marshal.dumps(code)).digest()


def build(root, destination):
    entries = {}
    for folder in ("sqlglot", "tests"):
        for source in sorted((root / folder).rglob("*.py")):
            name = source.relative_to(root).as_posix()
            module = compile(source.read_bytes(), name, "exec", dont_inherit=True)
            entries[name] = fingerprint(module)
    destination.write_bytes(marshal.dumps((importlib.util.MAGIC_NUMBER, entries)))


def load(destination):
    if destination.is_file():
        magic, entries = marshal.loads(destination.read_bytes())
        if magic == importlib.util.MAGIC_NUMBER:
            return entries
    return {}


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]))
