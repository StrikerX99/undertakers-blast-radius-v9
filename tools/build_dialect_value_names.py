"""Record present public class bindings used by static dialect tables."""

import json
from pathlib import Path
import subprocess
import sys


def main(root, output):
    root = Path(root).resolve()
    pin = subprocess.check_output(
        ["git", "-C", root, "rev-parse", "HEAD"], text=True
    ).strip()
    if pin != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError(pin)
    sys.path.insert(0, str(root))
    from sqlglot import exp

    if not Path(exp.__file__).resolve().is_relative_to(root):
        raise ValueError("Wrong source checkout")
    names = sorted(
        n
        for n, o in vars(exp).items()
        if n[:1].isupper() and isinstance(o, type) and issubclass(o, exp.Expr)
    )
    types = sorted(exp.DType.__members__)
    Path(output).write_text(
        json.dumps(
            {"pin": pin, "classes": names, "dtypes": types},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
