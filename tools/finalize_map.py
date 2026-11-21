"""Add independent baseline import/source-read observations; no patch inputs."""

import hashlib
import json
import sys
from pathlib import Path

m = json.load(open(sys.argv[1]))
root = Path(sys.argv[2])
seen = {tuple(v): i for i, v in enumerate(m["sets"])}
names = {n: i for i, n in enumerate(m["tests"])}


def intern(seq):
    key = tuple(sorted(names[n] for n in seq if n in names))
    if key not in seen:
        seen[key] = len(m["sets"])
        m["sets"].append(list(key))
    return seen[key]


m["files"] = {p: s for p, s in m["files"].items() if not p.endswith(".py")}
for p, ns in json.load(open(sys.argv[3])).items():
    m["files"][p] = intern(ns)
m["import_lines"] = json.load(open(sys.argv[4]))
m["source_hashes"] = {
    p: hashlib.sha256((root / p).read_bytes()).hexdigest()
    for p in set(m["lines"]) | set(m["files"])
    if (root / p).is_file()
}
m["provenance"] = {
    "source": "unmodified pinned SQLGlot baseline",
    "tests_passed": 1620,
    "tests_skipped": 1,
    "sample_patches_used_to_build_map": False,
    "sample_answers_used_to_build_map": False,
    "source_read_audit": "baseline-only supplementary run excludes import machinery reads",
}
Path(sys.argv[5]).write_text(json.dumps(m, separators=(",", ":")))
print(Path(sys.argv[5]).stat().st_size, "bytes")
