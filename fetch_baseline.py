"""Download exactly the competition's pinned public SQLGlot source archive."""

import io
import sys
import tarfile
import urllib.request
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def main():
    destination = Path(sys.argv[1])
    url = f"https://github.com/tobymao/sqlglot/archive/{PIN}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        archive = response.read()
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != f"sqlglot-{PIN}":
                raise ValueError("Unexpected archive layout")
            if len(parts) < 2:
                continue
            relative = Path(*parts[1:])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe archive path")
            if member.isfile():
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(tar.extractfile(member).read())
    if not (destination / "sqlglot" / "__init__.py").is_file():
        raise ValueError("Missing SQLGlot source")


if __name__ == "__main__":
    main()
