"""Conservative proof of a fresh thread from Codex's explicit native title mode.

The title selects the identity; loaded threads support that identity, and an
exact creation timestamp only rejects titles left by an earlier client. Older
threads and ambiguous effective configuration deliberately remain unsupported.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import time
import tomllib

from . import codex_resume
from .codex_readiness import loaded_thread_ids


_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_NATIVE_PREFIX = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{5}\.\.\.", re.I)
_SYSTEM_CONFIG = Path("/etc/codex/config.toml")


@dataclass(frozen=True)
class NativeTitleProof:
    session_id: str
    pid: int
    pane_id: str
    pane_pid: int
    start_ticks: int
    title: str


def _pane_title(pane: str) -> tuple[str, int, str] | None:
    if not re.fullmatch(r"%\d+", pane):
        return None
    try:
        result = subprocess.run([
            "tmux", "display-message", "-p", "-t", pane,
            "#{pane_id}\t#{pane_pid}\t#{pane_title}",
        ], capture_output=True, text=True, timeout=1)
        fields = result.stdout.removesuffix("\n").split("\t")
        if result.returncode or len(fields) != 3 or fields[0] != pane:
            return None
        pane_pid = int(fields[1])
        title = fields[2]
        if pane_pid <= 0 or not (_UUID.fullmatch(title) or _NATIVE_PREFIX.fullmatch(title)):
            return None
        return pane, pane_pid, title
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _start_bounds(start_ticks: int) -> tuple[int, int] | None:
    """Return earliest start UTC ns and a conservative latest start UTC ms."""
    try:
        frequency = int(os.sysconf("SC_CLK_TCK"))
        before = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        realtime = time.time_ns()
        after = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
        if (frequency <= 0 or start_ticks < 0 or not 0 <= before <= after
                or after - before > 50_000_000
                or start_ticks * 1_000_000_000 > after * frequency):
            return None
        earliest = realtime - after + start_ticks * 1_000_000_000 // frequency
        # /proc truncates start time to clock ticks. Use the upper edge rather
        # than accepting a thread created just before this process in its tick.
        latest = realtime - before + ((start_ticks + 1) * 1_000_000_000 + frequency - 1) // frequency
        return earliest, (latest + 999_999) // 1_000_000
    except (OSError, ValueError, AttributeError):
        return None


def _present(path: Path) -> bool:
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def _explicit_title_mode(pid: int, root: Path, argv: tuple[bytes, ...], earliest_start: int) -> bool:
    """Accept one global source, without guessing profile/project merge rules."""
    try:
        if any(argument in {b"-c", b"-p", b"-C", b"--config", b"--profile", b"--cd"}
               or argument.startswith((b"--config=", b"--profile=", b"--cd=", b"-c", b"-p", b"-C"))
               for argument in argv[1:]):
            return False
        process = codex_resume.PROC_ROOT / str(pid)
        with (process / "environ").open("rb") as stream:
            environment = stream.read(1_048_577)
        if len(environment) > 1_048_576:
            return False
        # Read only configuration-location/profile controls; never expose the
        # process environment or inspect authentication values.
        controls = dict(entry.split(b"=", 1) for entry in environment.split(b"\0")
                        if entry.startswith((b"HOME=", b"CODEX_HOME=", b"CODEX_PROFILE=")))
        if controls.get(b"CODEX_PROFILE"):
            return False
        native_home = controls.get(b"CODEX_HOME")
        if native_home is None:
            native_home = controls.get(b"HOME")
            if native_home is None:
                return False
            native_home = os.fsencode(Path(os.fsdecode(native_home)) / ".codex")
        native_root = Path(os.fsdecode(native_home))
        if not native_root.is_absolute() or Path(os.path.abspath(native_root)) != root:
            return False
        config = root / "config.toml"
        cwd = (process / "cwd").readlink()
        if not cwd.is_absolute() or _present(_SYSTEM_CONFIG) or _present(root / "managed_config.toml"):
            return False
        for parent in (cwd, *cwd.parents):
            project_config = parent / ".codex" / "config.toml"
            if project_config != config and _present(project_config):
                return False
        before = config.stat()
        if before.st_mtime_ns > earliest_start:
            return False
        with config.open("rb") as stream:
            data = stream.read(262_145)
        after = config.stat()
        if (len(data) > 262_144 or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)):
            return False
        value = tomllib.loads(data.decode("utf-8"))
        if "profile" in value or value.get("tui", {}).get("terminal_title") != ["thread-id"]:
            return False
        profiles = value.get("profiles", {})
        return isinstance(profiles, dict) and not any(
            isinstance(profile, dict) and isinstance(profile.get("tui"), dict)
            and "terminal_title" in profile["tui"] for profile in profiles.values()
        )
    except (OSError, ValueError, TypeError, AttributeError, tomllib.TOMLDecodeError):
        return False


def _matched_title(title: str, loaded: set[str] | None) -> str | None:
    if loaded is None:
        return None
    prefix = title[:-3].lower() if _NATIVE_PREFIX.fullmatch(title) else title.lower()
    matches = {identity.lower() for identity in loaded
               if _UUID.fullmatch(identity) and (identity.lower().startswith(prefix)
                                                  if title.endswith("...") else identity.lower() == prefix)}
    return next(iter(matches)) if len(matches) == 1 else None


def _created_at(root: Path, identity: str) -> int | None:
    try:
        database = (root / "state_5.sqlite").absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(database, uri=True, timeout=0.2)) as connection:
            row = connection.execute("SELECT created_at_ms FROM threads WHERE id = ?", (identity,)).fetchone()
        return row[0] if row and isinstance(row[0], int) and row[0] >= 0 else None
    except (OSError, ValueError, sqlite3.Error):
        return None


def _ready_client(pid: int, pane: str) -> codex_resume._TerminalClient | None:
    screen = codex_resume.pane_text(pane)
    client = codex_resume._terminal_client(pid, pane)
    if (client is None or codex_resume._startup_screen(screen)
            or not codex_resume._composer_layout(client, screen)
            or not codex_resume._client_unchanged(pid, client)):
        return None
    return client


def native_title_proof(pid: int, pane: str, codex_home: Path | None = None) -> NativeTitleProof | None:
    """Prove a fresh native title, or leave this client unsupported.

    This assumes native thread-id titles track the active thread. It deliberately
    requires explicit startup configuration and rejects older resumed threads.
    """
    root = Path(os.path.abspath(codex_home or os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    before = _pane_title(pane)
    if before is None:
        return None
    client = _ready_client(pid, pane)
    if client is None or tuple(client.chain)[-1] != before[1]:
        return None
    bounds = _start_bounds(client.chain[pid].start_ticks)
    if bounds is None or not _explicit_title_mode(pid, root, client.argv, bounds[0]):
        return None
    identity = _matched_title(before[2], loaded_thread_ids(root))
    if identity is None:
        return None
    created = _created_at(root, identity)
    if created is None or created < bounds[1]:
        return None
    # Repeat the catalog match too: a new colliding prefix is never resolved
    # with creation time, nearby rows, cwd, or the earlier loaded catalog.
    if _matched_title(before[2], loaded_thread_ids(root)) != identity:
        return None
    latest = _ready_client(pid, pane)
    after = _pane_title(pane)
    final_bounds = _start_bounds(client.chain[pid].start_ticks)
    if (latest is None or after != before or latest.chain != client.chain or latest.argv != client.argv
            or final_bounds is None or created < final_bounds[1]
            or not _explicit_title_mode(pid, root, latest.argv, min(bounds[0], final_bounds[0]))
            or _created_at(root, identity) != created
            or not codex_resume._client_unchanged(pid, latest)):
        return None
    return NativeTitleProof(identity, pid, pane, before[1], client.chain[pid].start_ticks, before[2])
