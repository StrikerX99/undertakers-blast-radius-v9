"""Preserve path-dependent collection errors when a test module stops importing."""

from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def _index():
    return json.loads(Path(__file__).with_name("test-imports.json").read_bytes())


def affected_by_failed_test_import(selector, path):
    index = _index()
    if index["commit"] != selector.mapping["commit"]:
        return selector.all
    files = {path, *index["importers"].get(path, [])}
    return {row["number"] for row in selector.tests if row["file"] in files}


def imported_test_modules(selector, path):
    index = _index()
    if index["commit"] != selector.mapping["commit"]:
        raise ValueError("Unrecognized test import map")
    return {f[:-3].replace("/", ".") for f in index["importers"].get(path, [])}
