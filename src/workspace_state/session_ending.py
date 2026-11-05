"""Private marker that the desktop session is ending and the final save owns the checkpoint."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path

from .login_status import runtime_root
from .operations import boot_id
from .util import atomic_json

MARKER_NAME = "session-ending.json"


def marker_path() -> Path:
    return runtime_root() / MARKER_NAME


def process_start_time(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        return raw[raw.rfind(")") + 2:].split()[19]
    except (OSError, IndexError, ValueError):
        return None


def _private_directory() -> bool:
    try:
        metadata = os.lstat(runtime_root())
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()


def write_marker(trigger: str) -> bool:
    """Record that this agent saw the first end-of-session signal."""
    start = process_start_time(os.getpid())
    try:
        runtime_root().mkdir(parents=True, exist_ok=True)
        if not _private_directory():
            return False
        atomic_json(marker_path(), {
            "schema_version": 1,
            "boot_id": boot_id(),
            "pid": os.getpid(),
            "process_start": start,
            "trigger": trigger,
            "written_at": time.time(),
        })
    except OSError:
        return False
    return True


def clear_marker() -> None:
    try:
        marker_path().unlink()
    except OSError:
        pass


def marker_active() -> str | None:
    """Return the trigger of a valid marker; invalid or stale markers are ignored."""
    try:
        if not _private_directory():
            return None
        descriptor = os.open(marker_path(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o077):
                return None
            document = json.load(stream)
        pid = document["pid"]
        start = document["process_start"]
        if (document["boot_id"] != boot_id() or not isinstance(pid, int) or pid <= 0
                or not isinstance(start, str) or process_start_time(pid) != start):
            return None
        return str(document.get("trigger", "end-session"))
    except (OSError, ValueError, KeyError, TypeError):
        return None
