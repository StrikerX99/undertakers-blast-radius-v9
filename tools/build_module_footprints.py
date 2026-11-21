"""Collect per-test-group on-disk Python module dependencies in isolated native runs."""

import argparse
import compileall
import concurrent.futures
import ctypes
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tarfile
import tempfile

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
IN_OPEN = 0x20
IN_OVERFLOW = 0x4000
IN_IGNORED = 0x8000
EVENT = struct.Struct("iIII")


def selector_group_mode(group, receipt):
    import pytest
    import sqlglot

    if Path(sqlglot.__file__).resolve() != Path.cwd() / "sqlglot/__init__.py":
        raise RuntimeError("Wrong package loaded")
    required = set(json.loads(group.read_bytes()))
    passed, failures = set(), set()

    class Observer:
        def pytest_collection_modifyitems(self, items):
            items[:] = [item for item in items if item.nodeid in required]

        def pytest_runtest_logreport(self, report):
            if report.failed or report.skipped:
                failures.add(report.nodeid)
            elif report.when == "call" and report.passed:
                passed.add(report.nodeid)

        @pytest.hookimpl(hookwrapper=True, tryfirst=True)
        def pytest_runtest_protocol(self, item):
            if not item.nodeid.endswith(
                "::TestParser::test_max_nodes"
            ) or not os.getenv("BR_GRADING_LOCK"):
                yield
            else:
                with open(os.environ["BR_GRADING_LOCK"], "a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    yield

    rc = pytest.main(
        ["tests", "-qq", "--tb=short", "-p", "no:cacheprovider"], plugins=[Observer()]
    )
    if rc or failures or passed != required:
        raise RuntimeError(
            (
                "Incomplete or failing group",
                rc,
                sorted(failures),
                sorted(required - passed),
            )
        )
    receipt.write_text(json.dumps(sorted(passed)))


def by_group(catalog):
    groups, rest = [], {}
    for t in catalog["tests"]:
        if t["seconds"] >= 0.40:
            groups.append([t["id"]])
        else:
            rest.setdefault(t["file"], []).append(t["id"])
    # Keep suite setup and class-level/transitive readers in a common group.
    for ids in rest.values():
        for lo in range(0, len(ids), 32):
            groups.append(ids[lo : lo + 32])
    return groups


def opened_under(argv, cwd, env, files, log):
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    watches, seen = {}, set()

    def drain():
        while True:
            try:
                data = os.read(fd, 65536)
            except BlockingIOError:
                return
            cur = 0
            while cur < len(data):
                wd, mask, cookie, length = EVENT.unpack_from(data, cur)
                if mask & (IN_OVERFLOW | IN_IGNORED) or wd not in watches:
                    raise ValueError("Incomplete module trace")
                if mask & IN_OPEN:
                    seen.add(watches[wd])
                cur += EVENT.size + length

    try:
        for src in files:
            for p in (
                cwd / src,
                Path(importlib.util.cache_from_source(str(cwd / src))),
            ):
                if not p.is_file() or p.is_symlink():
                    raise ValueError(f"Missing module source or bytecode {p}")
                w = libc.inotify_add_watch(fd, os.fsencode(p), IN_OPEN)
                if w < 0 or w in watches:
                    raise OSError(ctypes.get_errno(), "inotify_add_watch")
                watches[w] = src
        with (
            log.open("w") as stream,
            subprocess.Popen(
                argv, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT
            ) as proc,
        ):
            while proc.poll() is None:
                if select.select([fd], [], [], 1)[0]:
                    drain()
            drain()
            if proc.returncode:
                raise ValueError(f"Group failed; see {log}")
    finally:
        os.close(fd)
    return seen


def namespace_dependencies(root):
    """Identify classes exported only to their corresponding dialect definition."""
    import ast
    from collections import defaultdict

    paths = sorted((root / "sqlglot").rglob("*.py"))
    uses, definitions, registry_lines = defaultdict(list), defaultdict(list), {}
    parts = ("Parser", "Generator")
    for file in paths:
        path = file.relative_to(root).as_posix()
        source = file.read_bytes()
        tree = ast.parse(source)
        parent = {s: n for n in ast.walk(tree) for s in ast.iter_child_nodes(n)}
        for node in ast.walk(tree):
            # Ignore comments, docstrings and fuzzy substring matches. String
            # references naming a class can be used by getattr or __all__.
            if (
                isinstance(node, ast.ClassDef)
                and node.name.endswith(parts)
                and parent.get(node) is tree
            ):
                definitions[node.name].append(path)
            if isinstance(node, ast.alias) and node.name.endswith(parts):
                uses[node.name].append(
                    (
                        path,
                        "import"
                        if node.asname is None
                        and isinstance(parent[node], ast.ImportFrom)
                        else "other",
                    )
                )
            if (
                isinstance(node, ast.Name)
                and node.id.endswith(parts)
                and isinstance(node.ctx, ast.Load)
            ):
                par = parent[node]
                dest = None
                if (
                    isinstance(par, ast.Assign)
                    and par.value is node
                    and len(par.targets) == 1
                    and isinstance(par.targets[0], ast.Name)
                ):
                    dest = par.targets[0].id
                owner = parent.get(par)
                if not (
                    dest in parts
                    and isinstance(owner, ast.ClassDef)
                    and parent.get(owner) is tree
                ):
                    dest = "other"
                uses[node.id].append((path, dest))
            if isinstance(node, ast.Attribute) and node.attr.endswith(parts):
                uses[node.attr].append((path, "other"))
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value.endswith(parts)
            ):
                uses[node.value].append((path, "other"))
            if isinstance(node, ast.Attribute) and node.attr in {
                "_classes",
                "classes",
                "__subclasses__",
            }:
                # The registry's keyed lookups see the actual requested dialect
                # in the isolated group. Preserve readers observing the whole
                # registry, returned mapping, or set of registered class names.
                par = parent[node]
                if path == "sqlglot/dialects/dialect.py":
                    if node.attr == "_classes" and (
                        isinstance(par, (ast.Subscript, ast.Compare))
                        or isinstance(par, ast.Attribute)
                        and par.attr == "get"
                    ):
                        continue
                registry_lines.setdefault(path, []).append(node.lineno)
    eligible = {}
    for cls, locations in definitions.items():
        if len(locations) != 1:
            continue
        path = locations[0]
        if path.startswith("sqlglot/parsers/"):
            binding = "Parser"
        elif path.startswith("sqlglot/generators/"):
            binding = "Generator"
        else:
            continue
        dialect = path.replace("/parsers/", "/dialects/").replace(
            "/generators/", "/dialects/"
        )
        external = [(p, kind) for p, kind in uses[cls] if p != path]
        if sorted(external) != sorted([(dialect, "import"), (dialect, binding)]):
            continue
        # Check the binding belongs to that public dialect, with the built-in
        # Dialect metaclass as its only base and no custom decorators.
        mod = ast.parse((root / dialect).read_bytes())
        owner = [
            c
            for c in mod.body
            if isinstance(c, ast.ClassDef)
            and any(
                isinstance(a, ast.Assign)
                and len(a.targets) == 1
                and isinstance(a.targets[0], ast.Name)
                and a.targets[0].id == binding
                and isinstance(a.value, ast.Name)
                and a.value.id == cls
                for a in c.body
            )
        ]
        if (
            len(owner) != 1
            or owner[0].decorator_list
            or owner[0].keywords
            or len(owner[0].bases) != 1
            or not isinstance(owner[0].bases[0], ast.Name)
            or owner[0].bases[0].id != "Dialect"
        ):
            continue
        # Disallow a dialect used as a base or bound in a different production
        # module, where an initializer can access its inherited parser/generator.
        name = owner[0].name
        if any(
            ri != root / dialect and name in _imported_names(ri)
            for ri in paths
            if name.encode() in ri.read_bytes()
        ):
            continue
        eligible[path] = cls
    obs = sorted(
        p.relative_to(root).as_posix()
        for p in (root / "tests").rglob("test_*.py")
        if any(
            word in p.read_bytes()
            for word in (
                b"._classes",
                b".classes",
                b"__subclasses__",
                b"vars(Dialect)",
                b"dir(Dialect)",
            )
        )
    )
    return {
        "leaf": eligible,
        "registry_read_lines": registry_lines,
        "registry_test_files": obs,
    }


def _imported_names(path):
    import ast

    return {
        a.name
        for a in ast.walk(ast.parse(path.read_bytes()))
        if isinstance(a, ast.alias)
    }


def collect(root, catalog_path, output, jobs, work):
    root, catalog_path, output, work = (
        p.resolve() for p in (root, catalog_path, output, work)
    )
    if (
        subprocess.check_output(
            ["git", "-C", root, "rev-parse", "HEAD"], text=True
        ).strip()
        != PIN
    ):
        raise ValueError("Unrecognized baseline")
    work.mkdir(parents=True, exist_ok=False)
    template = work / "template"
    template.mkdir()
    archive = work / "baseline.tar"
    subprocess.run(["git", "-C", root, "archive", "-o", archive, PIN], check=True)
    with tarfile.open(archive) as f:
        f.extractall(template, filter="data")
    if not compileall.compile_dir(str(template / "sqlglot"), quiet=1):
        raise ValueError("Incomplete baseline")
    names = sorted(
        p.relative_to(template).as_posix()
        for p in (template / "sqlglot").rglob("*.py")
        if p.is_file() and not p.is_symlink()
    )
    hashes = {
        path: hashlib.sha256((template / path).read_bytes()).hexdigest()
        for path in names
    }
    groups = by_group(json.loads(catalog_path.read_bytes()))

    def one(args):
        idx, ids = args
        name = str(idx).zfill(3)
        dest = work / (name + ".json")
        with tempfile.TemporaryDirectory(dir=work, prefix=name + "-") as private:
            tmp = Path(private)
            checkout = tmp / "sqlglot-baseline"
            subprocess.run(
                ["cp", "-a", "--reflink=auto", template, checkout], check=True
            )
            group = tmp / "group.json"
            group.write_text(json.dumps(ids))
            receipt = tmp / "ok.json"
            env = dict(
                os.environ,
                PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                PYTHONPATH=str(checkout),
                SKIP_INTEGRATION="0",
                PYTHON_CPU_COUNT="2",
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
                PYTHONDONTWRITEBYTECODE="1",
            )
            seen = opened_under(
                [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--suite",
                    str(group),
                    str(receipt),
                ],
                checkout,
                env,
                names,
                work / (name + ".log"),
            )
            if (
                set(json.loads(receipt.read_bytes())) != set(ids)
                or "sqlglot/__init__.py" not in seen
            ):
                raise ValueError("Failed to observe test group")
        row = dict(ids=ids, files=sorted(seen))
        dest.write_text(json.dumps(row))
        print(name, len(ids), len(seen), flush=True)
        return row

    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        records = list(pool.map(one, enumerate(groups)))
    # Store positive file observations only. Incomplete run leaves no output.
    allids = sorted(t["id"] for t in json.loads(catalog_path.read_bytes())["tests"])
    assert sorted(n for record in records for n in record["ids"]) == allids
    groupread = {
        path: [idx for idx, r in enumerate(records) if path in r["files"]]
        for path in names
    }
    output.write_text(
        json.dumps(
            dict(
                commit=PIN,
                hashes=hashes,
                groups=groups,
                readers=groupread,
                **namespace_dependencies(template),
            ),
            separators=(",", ":"),
        )
        + "\n"
    )
    print("complete", len(groups), len(names), flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--suite":
        selector_group_mode(*(Path(x) for x in sys.argv[2:]))
    else:
        cli = argparse.ArgumentParser(description=__doc__)
        for key in ("root", "catalog", "output", "work"):
            cli.add_argument(key, type=Path)
        cli.add_argument("--jobs", type=int, default=2)
        a = cli.parse_args()
        collect(a.root, a.catalog, a.output, a.jobs, a.work)
