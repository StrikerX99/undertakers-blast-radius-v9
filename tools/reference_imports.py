"""Write importable package roots in the pinned baseline test environment."""
from importlib import metadata
import json
from pathlib import Path
import sys

roots = sorted(name for name in metadata.packages_distributions() if name.isidentifier())
Path(sys.argv[1]).write_text(json.dumps(roots, separators=(",", ":")) + "\n")
