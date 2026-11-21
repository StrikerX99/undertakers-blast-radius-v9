import coverage
import importlib
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
m = json.load(open(sys.argv[2]))
out = Path(sys.argv[3]).resolve()
os.chdir(root)
sys.path.insert(0, str(root))
c = coverage.Coverage(data_file=None, config_file=False, source=[str(root / "sqlglot")])
c.start()
for path in sorted(m["lines"]):
    if (
        path.startswith("sqlglot/")
        and path.endswith(".py")
        and not path.endswith("/__main__.py")
    ):
        module = path[:-3].replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        try:
            importlib.import_module(module)
        except Exception:
            pass
c.stop()
d = c.get_data()
result = {
    str(Path(p).relative_to(root)): d.lines(p) for p in d.measured_files() if d.lines(p)
}
out.write_text(json.dumps(result))
print(len(result), "files with import-time executed lines")
