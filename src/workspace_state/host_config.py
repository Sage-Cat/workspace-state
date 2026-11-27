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

import argparse
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .util import environment_path

_STAGE = re.compile(r"[a-z][a-z0-9-]{0,31}")
_UNIT = re.compile(r"[A-Za-z0-9:_.@-]+\.service")
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
        if (not mountpoint.startswith("/") or len(Path(mountpoint).parts) < 3
                or re.search(r"[\s%\"'\\\x00-\x1f]", mountpoint)):
            raise ValueError(f"{where} needs an absolute mountpoint below a top-level directory, "
                             f"without spaces, quotes, % or backslashes: {mountpoint!r}")
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


RESTORE_DROPIN = "50-workspace-restore.conf"
SHUTDOWN_DROPIN = "60-shutdown-unmount.conf"
_OWN_DROPINS = (RESTORE_DROPIN, SHUTDOWN_DROPIN)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text)
    temporary.chmod(0o644)
    temporary.replace(path)


def remove_dropins(destination: Path, keep: frozenset[str] = frozenset()) -> list[str]:
    """Remove the drop-ins this module installs, by file name, from every service directory.

    Only `*.service.d`: the warm-up timer's own drop-in is installed by the
    Makefile and is not ours to reconcile.
    """
    removed: list[str] = []
    for directory in sorted(destination.glob("*.service.d")):
        unit = directory.name[:-2]
        if unit in keep or not directory.is_dir():
            continue
        found = [directory / name for name in _OWN_DROPINS if (directory / name).is_file()]
        for path in found:
            path.unlink()
        if found:
            removed.append(unit)
            try:
                directory.rmdir()
            except OSError:
                pass  # other drop-ins of the unit remain
    return removed


def install_dropins(systemd_directory: Path, destination: Path, config: HostConfig | None = None) -> list[str]:
    """Write the restore gate and the shutdown unmount for every started drive.

    Without a host file nothing is installed and nothing is removed. With one,
    drop-ins of units no longer listed are removed.
    """
    config = config or load()
    if config.error:
        raise ValueError(config.error)
    if not config.present:
        return []
    restore = (systemd_directory / "post-workspace-drive.conf").read_text()
    template = (systemd_directory / "drive-shutdown.conf.in").read_text()
    units = []
    for drive in config.started:
        directory = destination / f"{drive.unit}.d"
        _write(directory / RESTORE_DROPIN, restore)
        _write(directory / SHUTDOWN_DROPIN, template.replace("@MOUNTPOINT@", str(drive.mountpoint)))
        units.append(drive.unit)
    remove_dropins(destination, frozenset(units))
    return units


def existing_dropins(destination: Path) -> list[Path]:
    """Service drop-ins that `make install-session` placed for mounts."""
    return sorted(path for name in _OWN_DROPINS for path in destination.glob(f"*.service.d/{name}"))


def _emit(items) -> None:
    lines = [str(item) for item in items]
    if lines:
        print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m workspace_state.host_config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("mountpoints", help="print the mountpoint of every started drive")
    commands.add_parser("units", help="print the unit of every started drive")
    install = commands.add_parser("install-dropins")
    install.add_argument("--systemd-dir", type=Path, required=True)
    install.add_argument("--dest", type=Path, required=True)
    remove = commands.add_parser("remove-dropins", help="remove ours by file name; print the units")
    remove.add_argument("--dest", type=Path, required=True)
    arguments = parser.parse_args(argv)
    config = load()
    try:
        if config.error and arguments.command in {"mountpoints", "units", "install-dropins"}:
            raise ValueError(config.error)
        if arguments.command == "mountpoints":
            _emit(drive.mountpoint for drive in config.started)
        elif arguments.command == "units":
            _emit(drive.unit for drive in config.started)
        elif arguments.command == "install-dropins":
            units = install_dropins(arguments.systemd_dir, arguments.dest, config)
            if not config.present:
                print(f"no {config.path}: no drive drop-ins installed or removed", file=sys.stderr)
            elif units:
                print("installed drop-ins for: " + ", ".join(units), file=sys.stderr)
        else:
            _emit(remove_dropins(arguments.dest))
    except (OSError, ValueError) as error:
        print(f"host configuration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
