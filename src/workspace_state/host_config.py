"""Private, host-specific settings kept outside the public source.

`${XDG_CONFIG_HOME:-~/.config}/workspace-state/host.toml` lists the network
drives that the login finalizer starts and the file manager and VS Code wait
for. A missing file means no drives and is not an error. An invalid file also
yields no drives, but carries the exact reason so the login reports it as a
failed stage and `doctor` shows it; reading never raises.

    [[drives]]
    stage = "drive-a"                      # stage id: lower case, digits, dashes
    label = "Drive A"                      # text shown in the HUD
    unit = "example-drive-a.service"       # user unit that mounts it
    mountpoint = "/home/user/Drives/drive-a"
    start = true                           # optional; false: gate on it, never start it
"""
from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .util import environment_path

_STAGE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_UNIT = re.compile(r"[A-Za-z0-9:_.@-]+\.(service|mount)")
_KEYS = {"stage", "label", "unit", "mountpoint", "start"}
# Stage ids that belong to the login itself.
_RESERVED = {"gnome", "displays", "tmux", "terminals", "codex", "browsers", "social-apps", "file-manager",
             "vscode", "workspace", "warmup", "drives", "login-finalization"}


@dataclass(frozen=True)
class Drive:
    stage: str
    label: str
    unit: str
    mountpoint: Path
    start: bool = True


@dataclass(frozen=True)
class HostConfig:
    path: Path
    present: bool
    drives: tuple[Drive, ...] = ()
    error: str | None = None

    @property
    def started(self) -> tuple[Drive, ...]:
        return tuple(drive for drive in self.drives if drive.start)


def config_path() -> Path:
    return environment_path("XDG_CONFIG_HOME", Path.home() / ".config") / "workspace-state/host.toml"


def _parse(text: str) -> tuple[Drive, ...]:
    document = tomllib.loads(text)
    if set(document) - {"drives"}:
        raise ValueError(f"unknown top-level key: {sorted(set(document) - {'drives'})[0]}")
    entries = document.get("drives", [])
    if not isinstance(entries, list):
        raise ValueError("drives must be an array of tables ([[drives]])")
    drives: list[Drive] = []
    for number, entry in enumerate(entries, 1):
        where = f"drives #{number}"
        if not isinstance(entry, dict):
            raise ValueError(f"{where} must be a table")
        if set(entry) - _KEYS:
            raise ValueError(f"{where} has an unknown key: {sorted(set(entry) - _KEYS)[0]}")
        for key in ("stage", "label", "unit", "mountpoint"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                raise ValueError(f"{where} needs a non-empty text {key}")
        if not _STAGE.fullmatch(entry["stage"]) or entry["stage"] in _RESERVED:
            raise ValueError(f"{where} has an invalid or reserved stage: {entry['stage']}")
        if not _UNIT.fullmatch(entry["unit"]):
            raise ValueError(f"{where} has an invalid unit name: {entry['unit']}")
        mountpoint = entry["mountpoint"]
        if "\0" in mountpoint or not mountpoint.startswith("/") or len(Path(mountpoint).parts) < 3:
            raise ValueError(f"{where} needs an absolute mountpoint below a top-level directory: {mountpoint}")
        start = entry.get("start", True)
        if not isinstance(start, bool):
            raise ValueError(f"{where} start must be true or false")
        drives.append(Drive(entry["stage"], entry["label"].strip()[:80], entry["unit"],
                            Path(os.path.normpath(mountpoint)), start))
    for key in ("stage", "unit", "mountpoint"):
        values = [getattr(drive, key) for drive in drives]
        if len(set(values)) != len(values):
            raise ValueError(f"two drives share a {key}")
    return tuple(drives)


_cache: dict[tuple[str, int, int], HostConfig] = {}


def load(path: Path | None = None) -> HostConfig:
    """Read the file; a changed file is read again, an unchanged one from memory."""
    path = path or config_path()
    try:
        details = path.stat()
    except FileNotFoundError:
        return HostConfig(path, False)
    except OSError as error:
        return HostConfig(path, True, error=f"cannot read {path}: {error.strerror or error}")
    key = (str(path), details.st_mtime_ns, details.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    try:
        result = HostConfig(path, True, _parse(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError) as error:  # tomllib.TOMLDecodeError is a ValueError
        result = HostConfig(path, True, error=f"{path}: {error}")
    _cache.clear()
    _cache[key] = result
    return result


def mounts() -> dict[Path, str]:
    """Every configured mountpoint with the unit that provides it."""
    return {drive.mountpoint: drive.unit for drive in load().drives}


def storage_root(mountpoint: Path, home: Path | None = None) -> Path:
    """The storage-backed directory around a mountpoint: its parent, unless that is too broad.

    A parent that is the home directory or a top-level directory would make
    ordinary folders count as storage, so the mountpoint itself is the root then.
    """
    parent = mountpoint.parent
    return mountpoint if parent == (home or Path.home()) or len(parent.parts) < 3 else parent


def storage_roots() -> tuple[Path, ...]:
    """Directories whose contents are storage-backed."""
    return tuple(sorted({storage_root(drive.mountpoint) for drive in load().drives}))


def is_storage_path(path: Path) -> bool:
    return any(path.is_relative_to(root) for root in storage_roots())
