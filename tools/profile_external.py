"""Refine baseline invocation coverage with Python subprocess source dependencies.

Input is the coverage map from the unmodified baseline. Each test that launched
an external program is rerun. Unsupported commands, nested processes, failed
runs, or missing observations keep the original conservative fallback.
"""

import ast
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import pytest

# Runs in a private globals dictionary before the child executes its original
# -c string. The child's interpreter, cwd, env, argv and main globals are kept.
CHILD = """
import atexit, json, os, sys
from pathlib import Path
root=Path(baseline_root).resolve()
paths=set()
opaque=False

def add(path):
    if not isinstance(path,(str,bytes)):
        return
    try:
        p=Path(os.fsdecode(path)).resolve().relative_to(root)
    except (ValueError, OSError):
        return
    if p.suffix=='.pyc' and p.parent.name=='__pycache__':
        p=p.parent.parent / (p.name.split('.')[0]+'.py')
    if p.suffix=='.py':paths.add(p.as_posix())

def audit(event,args):
    global opaque
    if event=='open' and args:add(args[0])
    if event in ('subprocess.Popen','os.posix_spawn','os.fork','os.forkpty','os.exec','os.system','ctypes.dlopen'):
        opaque=True

def finish():
    for module in tuple(sys.modules.values()):
        origin=getattr(module,'__file__',None)
        if origin:add(origin)
    (Path(report_path)).write_text(json.dumps({'paths':sorted(paths),'opaque':opaque}))

sys.addaudithook(audit)
atexit.register(finish)
"""


def simple_import_probe(source):
    """Certify the direct program only imports modules and checks values.

    Arbitrary file access/introspection, eval, dynamic execution and other
    commands remain opaque even if an audit hook could see some of their work.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError):
        return False
    for node in ast.walk(tree):
        if not isinstance(
            node,
            (
                ast.Module,
                ast.Import,
                ast.ImportFrom,
                ast.alias,
                ast.Assert,
                ast.Expr,
                ast.Compare,
                ast.cmpop,
                ast.Constant,
                ast.Attribute,
                ast.Name,
                ast.Load,
                ast.Call,
            ),
        ):
            return False
        if isinstance(node, ast.Call) and not (
            isinstance(node.func, ast.Name) and node.func.id in {"len", "print"}
        ):
            return False
    return True


def main():
    root = Path(sys.argv[1]).resolve()
    source = Path(sys.argv[2]).resolve()
    original = json.loads(source.read_bytes())
    out = Path(sys.argv[3]).resolve()
    if original["commit"] != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Incorrect source revision")
    sys.path.insert(0, str(root))
    os.chdir(root)
    os.environ["SKIP_INTEGRATION"] = "0"
    # pathlib/sys.modules in the tracer observe source loads and cached bytecode.
    # Import machinery is exercised with exactly the -c arguments of the test.
    records = {}
    old = subprocess.Popen
    active = None
    pending = []
    opaque = set()

    def on_spawn(event, args):
        if (
            active is not None
            and event
            in (
                "subprocess.Popen",
                "os.posix_spawn",
                "os.fork",
                "os.forkpty",
                "os.exec",
                "os.system",
            )
            and not pending
        ):
            opaque.add(active)

    sys.addaudithook(on_spawn)
    with tempfile.TemporaryDirectory() as tmp:

        def tracked(*pos, **kw):
            if active is None:
                return old(*pos, **kw)
            argv = pos[0] if pos else kw.get("args")
            env = kw.get("env") or os.environ
            executable = (
                shutil.which(str(argv[0]), path=env.get("PATH"))
                if isinstance(argv, (list, tuple)) and argv
                else None
            )
            eligible = (
                len(pos) <= 1
                and isinstance(argv, (list, tuple))
                and len(argv) == 3
                and argv[1] == "-c"
                and isinstance(argv[2], str)
                and executable
                and Path(executable).is_absolute()
                and re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(executable).name)
                and not kw.get("shell")
                and not kw.get("executable")
                and not kw.get("preexec_fn")
                and simple_import_probe(argv[2])
            )
            if not eligible:
                opaque.add(active)
                return old(*pos, **kw)
            report = Path(tmp) / f"{sum(len(v) for v in records.values())}.json"
            records.setdefault(active, []).append(report)
            private = {"baseline_root": str(root), "report_path": str(report)}
            prefix = f"exec({CHILD!r},{private!r});"
            args = [
                executable,
                "-c",
                prefix + f'exec(compile({argv[2]!r},"<string>","exec"))',
            ]
            if pos:
                pos = (args,)
            else:
                kw["args"] = args
            pending.append(True)
            try:
                return old(*pos, **kw)
            finally:
                pending.pop()

        class Select:
            @pytest.hookimpl(hookwrapper=True)
            def pytest_runtest_protocol(self, item, nextitem):
                nonlocal active
                active = item.nodeid
                try:
                    yield
                finally:
                    active = None

        subprocess.Popen = tracked
        nodes = [original["tests"][i] for i in original["external"]]
        try:
            status = (
                pytest.main(
                    [*nodes, "-q", "-p", "no:cacheprovider", "--tb=short"],
                    plugins=[Select()],
                )
                if nodes
                else 0
            )
        finally:
            subprocess.Popen = old
        if status:
            raise RuntimeError("Baseline subprocess profile did not pass")
        known = {}
        for idx in original["external"]:
            node = original["tests"][idx]
            if node in opaque or not records.get(node):
                continue
            paths = set()
            for report in records[node]:
                if not report.is_file():
                    break
                data = json.loads(report.read_bytes())
                if data["opaque"] or "sqlglot/__init__.py" not in data["paths"]:
                    break
                paths.update(data["paths"])
            else:
                known[str(idx)] = sorted(paths)
        original["external_paths"] = known
        out.write_text(json.dumps(original, separators=(",", ":")) + "\n")
        print(
            f"{len(known)} / {len(nodes)} external tests have certified child source dependencies"
        )


if __name__ == "__main__":
    main()
