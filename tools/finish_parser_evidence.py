"""Pack native parser ownership records by the method names in the official catalog."""

import json
from pathlib import Path
import sys


def pack(records, catalog, output):
    data = json.loads(records.read_bytes())
    tests = json.loads(catalog.read_bytes())["tests"]
    names = [test["id"] for test in tests]
    offsets = {name: idx for idx, name in enumerate(names)}
    groups = {}

    def group(owners):
        if "*" in owners:
            return -1
        indices = set()
        for name in owners:
            if name.endswith("::*"):
                indices.update(
                    i for i, test in enumerate(tests) if test["file"] == name[:-3]
                )
            else:
                indices.add(offsets[name])
        encoded = ",".join(str(index) for index in sorted(indices))
        return groups.setdefault(encoded, len(groups))

    result = {"pin": data["pin"], "source": data["source"], "names": names}
    for field in ("owners", "throws", "sites"):
        result[field] = {
            key: group(owners) for key, owners in sorted(data[field].items())
        }
    result["groups"] = list(groups)
    output.write_text(json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n")


if __name__ == "__main__":
    pack(*(Path(value) for value in sys.argv[1:]))
