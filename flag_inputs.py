"""Select observed readers of a dialect setting changed on its single declaration line."""

from functools import lru_cache
import json
from pathlib import Path

import dormant_settings
import inherited_methods


@lru_cache(maxsize=1)
def profile():
    return json.loads(Path(__file__).with_name("flag-inputs.json").read_bytes())


def verify(selector, changes, selected, apply, equal):
    if not selected or selector.unknown or len(changes) != 1:
        return selected
    change = changes[0]
    path = change["old"]
    if (
        path != change["new"]
        or not path.startswith("sqlglot/dialects/")
        or len(change["hunks"]) != 1
    ):
        return selected
    hunk = change["hunks"][0]["lines"]
    removed = [line[1:] for line in hunk if line.startswith("-")]
    added = [line[1:] for line in hunk if line.startswith("+")]
    if (
        len(removed) != 1
        or len(added) != 1
        or removed[0][: len(removed[0]) - len(removed[0].lstrip())]
        != added[0][: len(added[0]) - len(added[0].lstrip())]
    ):
        return selected
    try:
        old = dormant_settings._literal_assignment(removed[0])
        new = dormant_settings._literal_assignment(added[0])
    except (ValueError, SyntaxError, RecursionError):
        return selected
    if not old or not new or old[:2] != new[:2] or old[1] not in (bool, str, int):
        return selected
    left, right = old[2], new[2]
    if type(left) is not type(right) or not all(
        equal(getattr(left, field), getattr(right, field))
        for field in left._fields
        if field != "value"
    ):
        return selected
    data = profile()
    raw = dormant_settings._sources()
    digest = data["sources"].get(path)
    if (
        not digest
        or data["pin"] != selector.mapping["commit"]
        or raw["commit"] != data["pin"]
        or digest != selector.mapping["source_hashes"].get(path)
        or digest != raw["unopened"].get(path)
        or path in selector.files
    ):
        return selected
    original, _ = selector.source(path)
    _, rows, _ = apply(original, change)
    if len(rows) != 1:
        return selected
    row = data["sites"].get(path, {}).get(str(next(iter(rows))))
    if row is None or row[1] != old[0]:
        return selected
    _, _, direct, ascii_string = row
    if direct == -1 or (old[1] is not bool and ascii_string == -1):
        return selected
    # A boolean switch between case conversion and ASCII translation produces
    # exactly the same string for the recorded ordinary ASCII inputs. Opaque
    # reads and inputs outside that domain retain their test identity.
    groups = [direct] if old[1] is bool else [direct, ascii_string]
    names = {data["names"][i] for g in groups for i in data["groups"][g]}
    ext = selector.dispatch_map()
    readers = (
        ext.external
        | ext.external_by_source.get(path, set())
        | {selector.by_name[n] for n in names if n in selector.by_name}
    )
    sensitive = set(inherited_methods.catalog()["reflective_test_files"])
    readers.update(t["number"] for t in selector.tests if t["file"] in sensitive)
    return selected & readers
