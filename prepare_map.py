"""Partition one trusted baseline JSON map for cheap per-file access in the image."""

import hashlib
import json
import marshal
import sys
from pathlib import Path


def shard_name(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def prepare(mapping_path, target):
    mapping = json.loads(mapping_path.read_bytes())
    target.mkdir(exist_ok=True)
    fields = ("lines", "comparisons", "regions", "import_lines")
    tables = {field: mapping.pop(field, {}) for field in fields}
    paths = set().union(*(table.keys() for table in tables.values()))
    # Store sets used by individual sources inside their own shard. The
    # lightweight master index still refers to the original numbered sets.
    used = set(mapping["files"].values())
    sets = mapping.pop("sets")
    for path in paths:
        data = {name: rows[path] for name, rows in tables.items() if path in rows}
        needed = set(data.get("lines", {}).values()) | set(
            data.get("regions", {}).values()
        )
        for record in data.get("comparisons", {}).values():
            needed.add(record["unknown"])
            needed.update(row[1] for row in record["variants"])
        data["sets"] = {i: sets[i] for i in needed}
        (target / shard_name(path)).write_bytes(marshal.dumps(data))
    mapping["sets"] = {i: sets[i] for i in used}
    # Timings from the offline collection are not needed by the running entry.
    mapping.pop("durations", None)
    mapping["paths"] = paths
    (target / "index").write_bytes(marshal.dumps(mapping))


if __name__ == "__main__":
    prepare(Path(sys.argv[1]), Path(sys.argv[2]))
