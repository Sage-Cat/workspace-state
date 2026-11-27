"""Read saved Codex working directories and check access without blocking startup."""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path

_STATE_DATABASE = re.compile(r"state_(\d+)\.sqlite\Z")


def saved_cwd(session_id: str, codex_home: Path | None = None) -> Path | None:
    """Read the exact thread's cwd from the newest local state schema, read-only."""
    root = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    try:
        candidates = [
            (int(match.group(1)), path)
            for path in root.glob("state_*.sqlite")
            if (match := _STATE_DATABASE.fullmatch(path.name))
        ]
        if not candidates:
            return None
        database = max(candidates, key=lambda item: item[0])[1]
        with closing(sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True, timeout=0.2)) as connection:
            row = connection.execute("SELECT cwd FROM threads WHERE id = ?", (session_id,)).fetchone()
        if row and isinstance(row[0], str) and row[0] and "\0" not in row[0]:
            path = Path(row[0])
            if path.is_absolute():
                return path
    except (OSError, sqlite3.Error, ValueError):
        pass
    return None


def _mount_for(path: Path, home: Path, mounts: tuple[Path, ...]) -> Path | None:
    """The mountpoint that must be mounted before `path` is touched, else None."""
    covering = [mount for mount in mounts if path.is_relative_to(mount)]
    if covering:
        return max(covering, key=lambda mount: len(mount.parts))
    # Anything else below a storage root needs its first component mounted.
    # The parent of a mountpoint, unless it is the home directory or a top-level directory.
    parents = {mount.parent for mount in mounts if mount.parent != home and len(mount.parent.parts) >= 3}
    for root in {home / "Drives", *parents}:
        if path.is_relative_to(root) and path != root:
            return root / path.relative_to(root).parts[0]
    return None


def _directory_accessible(path: Path, home: Path, mounts: tuple[Path, ...] = ()) -> bool:
    """Run inside a disposable child: network-backed filesystem calls may stall."""
    if not path.is_absolute():
        return False
    # Normalize lexically only. resolve() would touch a disconnected drive before
    # the mount gate, and parent components must not bypass that gate.
    path = Path(os.path.normpath(path))
    mount = _mount_for(path, home, mounts)
    if mount is not None and not os.path.ismount(mount):
        return False
    try:
        # Opening and reading one entry proves directory access. Empty
        # directories are valid, and no full recursive listing is necessary.
        with os.scandir(path) as entries:
            next(entries, None)
        return True
    except OSError:
        return False


def directory_ready(path: Path | str | None, *, timeout: float = 1.0) -> bool:
    """Check that a directory can be read within a bounded wall-clock interval."""
    if path is None or timeout <= 0:
        return False
    try:
        from .host_config import mounts
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).absolute()), str(path), str(Path.home()), *map(str, mounts())],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            return process.wait(timeout=timeout) == 0
        except subprocess.TimeoutExpired:
            process.kill()
            # A stalled FUSE operation can delay even SIGKILL completion. Do
            # not use subprocess.run's unbounded post-kill wait here.
            try:
                process.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass
            return False
    except (OSError, ValueError):
        return False


if __name__ == "__main__":
    try:
        ready = len(sys.argv) >= 3 and _directory_accessible(Path(sys.argv[1]), Path(sys.argv[2]),
                                                          tuple(Path(item) for item in sys.argv[3:]))
    except (OSError, ValueError):
        ready = False
    raise SystemExit(0 if ready else 1)
