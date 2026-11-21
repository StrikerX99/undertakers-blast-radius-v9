"""Record unused plain documentation and fixture inputs during a passing pinned suite."""

import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import tarfile
import tempfile

EVENT = struct.Struct("iIII")
OPEN, OVERFLOW, IGNORED = 0x20, 0x4000, 0x8000
SITE = """
import os, sys
from pathlib import Path
import atexit
_log = os.environ["BR_FILE_LOG"]
_watched = set(Path(os.environ["BR_FILE_NAMES"]).read_text().splitlines())
Path(_log, str(os.getpid()) + ".started").touch()
_seen = set()
_original = os.stat
def watch(fn):
    def wrapper(path, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)):
            # pytest's collection walks call Path.is_dir() on regular files.
            # A content-only patch cannot change the directory bit. A direct
            # stat, or any other consumer, still makes the file ineligible.
            caller = sys._getframe(1)
            if (fn is _original and caller.f_code is Path.stat.__code__ and
                    caller.f_back is not None and caller.f_back.f_code is Path.is_dir.__code__):
                return fn(path, *args, **kwargs)
            raw = os.fsdecode(path)
            if kwargs.get("dir_fd") is not None and not os.path.isabs(raw):
                _seen.update(p for p in _watched if os.path.basename(p) == os.path.basename(raw))
            else:
                name = os.path.abspath(raw)
                if name in _watched:
                    _seen.add(name)
        return fn(path, *args, **kwargs)
    return wrapper
for _name in ("stat", "lstat", "access", "readlink"):
    setattr(os, _name, watch(getattr(os, _name)))
@atexit.register
def save():
    Path(_log, str(os.getpid()) + ".observed").write_text("\\n".join(sorted(_seen)))
"""


def candidates(root):
    proposed = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(s.startswith(".") or s == "__pycache__" for s in rel.parts):
            continue
        if not (
            path.suffix.lower() in {".md", ".rst"}
            or (
                rel.parts[:2] == ("tests", "fixtures")
                and path.suffix in {".sql", ".json", ".txt"}
            )
        ):
            continue
        if path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
            proposed.append(path)
    return sorted(proposed)


def collect(root, catalog, output):
    root, catalog = root.resolve(), catalog.resolve()
    pinned = subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()
    if pinned != "eb11aec4be8cb2122afe713a4816662c3f0fb2fa":
        raise ValueError("Unexpected source revision")
    with tempfile.TemporaryDirectory(prefix="br-baseline-inputs-") as tmp:
        private = Path(tmp)
        archive = private / "baseline.tar"
        subprocess.run(
            ["git", "-C", root, "archive", "-o", archive, pinned], check=True
        )
        source = private / "repo"
        with tarfile.open(archive) as tar:
            tar.extractall(source, filter="data")
        _record(source, catalog, output, pinned)


def _record(root, catalog, output, pinned):
    paths = candidates(root)
    digests = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    lib = ctypes.CDLL(None, use_errno=True)
    fd = lib.inotify_init1(os.O_CLOEXEC | os.O_NONBLOCK)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify_init1")
    watches, seen = {}, set()

    def drain():
        while True:
            try:
                block = os.read(fd, 65536)
            except BlockingIOError:
                return
            pos = 0
            while pos < len(block):
                wd, mask, _, length = EVENT.unpack_from(block, pos)
                if mask & (OVERFLOW | IGNORED) or wd not in watches:
                    raise ValueError("Incomplete file access trace")
                if mask & OPEN:
                    seen.add(watches[wd])
                pos += EVENT.size + length

    try:
        for p in paths:
            wd = lib.inotify_add_watch(fd, os.fsencode(p), OPEN)
            if wd < 0 or wd in watches:
                raise ValueError(f"Invalid watch on {p}")
            watches[wd] = p
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "sitecustomize.py").write_text(SITE)
            (folder / "names").write_text("\n".join(str(p) for p in paths))
            env = dict(
                os.environ,
                BR_FILE_LOG=tmp,
                BR_FILE_NAMES=str(folder / "names"),
                PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                PYTHONPATH=tmp + os.pathsep + str(root),
                PYTHON_CPU_COUNT="2",
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
                SKIP_INTEGRATION="0",
            )
            cmd = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--suite",
                str(catalog),
                str(folder / "ok"),
            ]
            with subprocess.Popen(cmd, cwd=root, env=env) as child:
                while child.poll() is None:
                    if select.select([fd], [], [], 1)[0]:
                        drain()
                drain()
                if child.returncode or not (folder / "ok").exists():
                    raise RuntimeError("Full baseline suite did not match the catalog")
            logs = list(folder.glob("*.observed"))
            if not logs or {p.stem for p in logs} != {
                p.stem for p in folder.glob("*.started")
            }:
                raise ValueError("No metadata observation")
            inspected = set().union(*(set(p.read_text().splitlines()) for p in logs))
    finally:
        os.close(fd)
    unknown = {p for p in paths if str(p) in inspected}
    result = {
        "commit": pinned,
        "unread": {
            p.relative_to(root).as_posix(): digests[p]
            for p in paths
            if p not in seen | unknown
        },
    }
    output.write_text(json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n")
    print(
        f"{len(paths)} watched, {len(seen)} opened, {len(unknown)} with metadata queries, {len(result['unread'])} certified unread"
    )


if __name__ == "__main__":
    if sys.argv[1] == "--suite":
        import fcntl
        import pytest

        catalog = json.loads(Path(sys.argv[2]).read_bytes())
        passes, skips = set(), set()

        class Observer:
            @pytest.hookimpl(hookwrapper=True, tryfirst=True)
            def pytest_runtest_protocol(self, item):
                lock = None
                if item.nodeid.endswith("::TestParser::test_max_nodes") and os.getenv(
                    "BR_GRADING_LOCK"
                ):
                    lock = open(os.environ["BR_GRADING_LOCK"], "a")
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    if lock:
                        lock.close()

            def pytest_runtest_logreport(self, report):
                if report.skipped:
                    skips.add(report.nodeid)
                elif report.when == "call" and report.passed:
                    passes.add(report.nodeid)

        rc = pytest.main(
            ["-qq", "tests", "--tb=short", "-p", "no:cacheprovider"],
            plugins=[Observer()],
        )
        if (
            rc
            or passes != {t["id"] for t in catalog["tests"]}
            or skips != set(catalog["skipped"])
        ):
            raise RuntimeError("Invalid baseline outcomes")
        Path(sys.argv[3]).touch()
    else:
        collect(*(Path(p) for p in sys.argv[1:]))
