"""Discover Agent Deck sessions without owning their transcripts or processes."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .capture import (
    _interactive_codex, _pane_process_chain, _proc_children, _process_identity,
    _same_foreground_terminal, process_snapshot,
)
from .claude_identity import _interactive as _interactive_claude
from .util import CommandError


class AgentDeckError(CommandError):
    """Agent Deck could not provide a complete observation."""


_TIMEOUT = 10.0
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_SOCKET = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")
_PROVIDERS = {"claude", "codex"}
_CALLER_ENV = {
    "TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_SESSION_ID", "CLAUDE_JOB_DIR", "CODEX_HOME", "CODEX_SESSION_ID",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
}
_SESSION_KEYS = frozenset({
    "profile", "id", "group", "account", "tool", "title", "native_id", "open", "stopped", "archived",
})


def _roots() -> tuple[Path, Path, Path]:
    def xdg(name: str, fallback: Path) -> Path:
        value = Path(os.environ.get(name, "").strip())
        return (value if value.is_absolute() else fallback) / "agent-deck"

    home = Path.home()
    return (
        xdg("XDG_CONFIG_HOME", home / ".config"),
        xdg("XDG_DATA_HOME", home / ".local/share"),
        home / ".agent-deck",
    )


def _executable() -> Path | None:
    # The login worker may omit the user's binary directory from PATH.
    candidates = [shutil.which("agent-deck"), Path.home() / ".local/bin/agent-deck"]
    for name in candidates:
        if not name:
            continue
        path = Path(name)
        if not path.is_absolute():
            continue
        try:
            mode = path.stat()
        except OSError:
            continue
        if _trusted_executable(mode) and os.access(path, os.X_OK):
            return path
    return None


def _trusted_executable(info: os.stat_result) -> bool:
    return (stat.S_ISREG(info.st_mode) and info.st_uid in (0, os.getuid())
            and bool(info.st_mode & 0o111) and not info.st_mode & 0o022)


def _has_store(root: Path) -> bool:
    if (root / "sessions.json").is_file():
        return True
    profiles = root / "profiles"
    if not profiles.is_dir():
        return False
    try:
        return any((profile / name).is_file() for profile in profiles.iterdir()
                   for name in ("state.db", "sessions.json"))
    except OSError:
        # A previously configured directory may be temporarily unavailable.
        # Select Deck ownership and report an observation failure if so.
        return True


def enabled() -> bool:
    """Select a configured installation even when its executable is missing."""
    config, data, legacy = _roots()
    config_files = ("config.toml", "config.json")
    if any((config / name).is_file() for name in config_files) or _has_store(data):
        return True
    home = Path.home()
    # Explicitly isolated XDG roots must not discover someone else's legacy
    # registry through HOME. Use the legacy fallback with the standard roots.
    if config != home / ".config/agent-deck" or data != home / ".local/share/agent-deck":
        return False
    return any((legacy / name).is_file() for name in config_files) or _has_store(legacy)


def _required_executable() -> Path:
    binary = _executable()
    if binary is None:
        raise AgentDeckError("Agent Deck executable is unavailable")
    return binary


def _text(value: Any, *, empty: bool = False) -> bool:
    return (
        isinstance(value, str)
        and (empty or bool(value))
        and not any(ord(char) < 32 or 127 <= ord(char) < 160 or 0xD800 <= ord(char) <= 0xDFFF
                    for char in value)
    )


def _component(value: Any) -> bool:
    return (_text(value) and value == value.strip() and not value.startswith("-")
            and "/" not in value and value not in {".", ".."})


def _group(value: Any) -> bool:
    return (_text(value, empty=True) and value == value.strip() and not value.startswith("-")
            and (not value or all(part not in {"", ".", ".."} for part in value.split("/"))))


def validate(record: Any) -> None:
    """Check the versioned checkpoint payload, with no CLI or storage access."""
    if (not isinstance(record, dict) or type(record.get("version")) is not int
            or record["version"] != 1 or not isinstance(record.get("sessions"), list)):
        raise ValueError("invalid Agent Deck inventory")
    seen = set()
    for row in record["sessions"]:
        if not isinstance(row, dict) or row.keys() != _SESSION_KEYS:
            raise ValueError("invalid Agent Deck session fields")
        native = row["native_id"]
        if (not _component(row["profile"]) or not _component(row["id"]) or not _group(row["group"])
                or not _text(row["account"], empty=True) or not _text(row["tool"])
                or not _text(row["title"], empty=True)
                or (native is not None and (row["tool"] not in _PROVIDERS
                    or not isinstance(native, str) or not _UUID.fullmatch(native)))
                or any(type(row[key]) is not bool for key in ("open", "stopped", "archived"))
                or (row["open"] and (row["archived"] or row["stopped"]))):
            raise ValueError("invalid Agent Deck session identity or state")
        identity = (row["profile"], row["id"])
        if identity in seen:
            raise ValueError("duplicate Agent Deck session identity")
        seen.add(identity)


def _deadline(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentDeckError("Agent Deck deadline expired")
    return time.monotonic() + timeout


def _cli(command: list[str], deadline: float, operation: str) -> subprocess.CompletedProcess:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AgentDeckError(f"Agent Deck {operation} timed out")
    # Socket-less Agent Deck sessions belong to the default server. A CLI
    # invoked from a dashboard pane must not redirect their status checks.
    # Resolve accounts from the recorded Deck session and its configuration
    # instead of inheriting another session's identity or API credentials.
    env = {key: value for key, value in os.environ.items()
           if key not in _CALLER_ENV and not key.startswith("AGENTDECK_")}
    env["AGENTDECK_SKIP_UPDATE_CHECK"] = "1"
    try:
        return subprocess.run(
            command, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AgentDeckError(f"Agent Deck {operation} timed out") from error
    except OSError as error:
        raise AgentDeckError(f"could not run Agent Deck {operation}") from error


def _json(binary: Path, args: list[str], deadline: float, operation: str) -> Any:
    result = _cli([str(binary), *args], deadline, operation)
    if result.returncode:
        # Provider diagnostics may include local paths or addresses.
        raise AgentDeckError(f"Agent Deck {operation} command exited with status {result.returncode}")
    try:
        return json.loads(result.stdout)
    except (ValueError, UnicodeError) as error:
        raise AgentDeckError(f"Agent Deck {operation} returned invalid JSON") from error


def _profiles(binary: Path, deadline: float) -> set[str]:
    result = _json(binary, ["profile", "list", "--json"], deadline, "inventory")
    if (not isinstance(result, dict) or result.get("success") is not True
            or type(result.get("total")) is not int or "profiles" not in result):
        raise AgentDeckError("invalid Agent Deck profile list")
    records = result.get("profiles")
    if records is None and result["total"] == 0:
        # The CLI serializes its uninitialized Go slice as null.
        return set()
    if not isinstance(records, list) or len(records) != result["total"]:
        raise AgentDeckError("incomplete Agent Deck profile list")
    profiles = set()
    for row in records:
        if not isinstance(row, dict) or not _component(row.get("name")) or row["name"] in profiles:
            raise AgentDeckError("invalid Agent Deck profile identity")
        profiles.add(row["name"])
    return profiles


def _session(row: Any, profile: str) -> dict[str, Any]:
    if not isinstance(row, dict) or row.get("profile") != profile:
        raise AgentDeckError("Agent Deck returned a session outside the requested profile")
    archived = row.get("archived")
    tool = row.get("tool")
    native = row.get(f"{tool}_session_id") if tool in ("claude", "codex") else None
    if native is not None and native != "" and (not isinstance(native, str) or not _UUID.fullmatch(native)):
        raise AgentDeckError("invalid Agent Deck native conversation identity")
    return {
        "profile": profile,
        "id": row.get("id"),
        "group": row.get("group"),
        "account": row.get("account"),
        "tool": tool,
        "title": row.get("title"),
        "native_id": native.lower() if native else None,
        "open": False,
        "stopped": row.get("status") == "stopped" and row.get("status_source") in ("live", "cached"),
        "archived": archived,
    }


def _route(row: dict[str, Any]) -> tuple[str, str] | None:
    if row.get("archived") or row.get("ssh_host") or row.get("tool") not in ("claude", "codex"):
        return None
    name = row.get("tmux_session", "")
    socket = row.get("tmux_socket_name", "")
    if name == "":
        return None
    if not _text(name) or not isinstance(socket, str) or (socket and not _SOCKET.fullmatch(socket)):
        raise AgentDeckError("invalid Agent Deck tmux route")
    return socket or "default", name


@dataclass(frozen=True)
class _Pane:
    pid: int
    tty: str


def _list_panes(socket: str, deadline: float) -> dict[str, list[_Pane]]:
    fields = "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_dead}"
    result = _cli(["tmux", "-L", socket, "list-panes", "-a", "-F", fields], deadline, "pane inventory")
    if result.returncode:
        if result.returncode == 1 and not result.stdout and (
            result.stderr.startswith(b"no server running on ")
            or (result.stderr.startswith(b"error connecting to ")
                and result.stderr.rstrip().endswith(b"(No such file or directory)"))
        ):
            return {}
        raise AgentDeckError("could not observe Agent Deck tmux socket")
    sessions: dict[str, list[_Pane]] = {}
    pane_ids: set[tuple[str, str]] = set()
    for line in os.fsdecode(result.stdout).splitlines():
        parts = line.split("\t")
        if len(parts) != 5:
            raise AgentDeckError("invalid Agent Deck pane inventory")
        name, ident, pid, tty, dead = parts
        if (not _text(name) or not re.fullmatch(r"%[0-9]+", ident) or not pid.isascii()
                or not pid.isdecimal() or dead not in ("0", "1") or (name, ident) in pane_ids
                or (dead == "0" and (int(pid) <= 0 or not tty.startswith("/dev/pts/")))):
            raise AgentDeckError("invalid Agent Deck pane identity")
        pane_ids.add((name, ident))
        if dead == "0":
            sessions.setdefault(name, []).append(_Pane(int(pid), tty))
    return sessions


def _foreground_processes(pane_pid: int, proc_root: Path, tty: str | None = None):
    pane = _process_identity(pane_pid, proc_root)
    if pane is None or (tty is not None and pane.stdin != tty):
        raise AgentDeckError("Agent Deck pane process changed during capture")
    for pid in (pane_pid, *_proc_children(pane_pid, proc_root=proc_root)):
        identity = _process_identity(pid, proc_root)
        if identity is not None and _same_foreground_terminal(identity, pane):
            chain = _pane_process_chain(pid, pane_pid, pane, proc_root)
            if chain is None or chain[pid] != identity:
                raise AgentDeckError("Agent Deck foreground process changed during capture")
            yield pid, chain
    if _process_identity(pane_pid, proc_root) != pane:
        raise AgentDeckError("Agent Deck pane process changed during capture")


def _foreground_provider(pane: _Pane, proc_root: Path = Path("/proc")) -> str | None:
    providers = []
    for pid, chain in _foreground_processes(pane.pid, proc_root, pane.tty):
        process = proc_root / str(pid)
        try:
            name = (process / "comm").read_text().strip()
            if name not in _PROVIDERS:
                continue
            cmdline = (process / "cmdline").read_bytes()
            argv = tuple(cmdline.removesuffix(b"\0").split(b"\0"))
            interactive = _interactive_claude(argv) if name == "claude" else _interactive_codex(list(argv))
            if (not cmdline or (process / "comm").read_text().strip() != name
                    or (process / "cmdline").read_bytes() != cmdline):
                raise AgentDeckError("Agent Deck foreground client changed during capture")
        except (OSError, ValueError) as error:
            raise AgentDeckError("could not verify Agent Deck foreground client") from error
        if not all(_process_identity(member, proc_root) == identity for member, identity in chain.items()):
            raise AgentDeckError("Agent Deck foreground client changed during capture")
        if interactive is None:
            raise AgentDeckError("could not classify Agent Deck foreground client")
        if interactive:
            providers.append(name)
    if len(providers) > 1:
        raise AgentDeckError("ambiguous Agent Deck foreground clients")
    return providers[0] if providers else None


def _observe_open(routes: list[tuple[dict[str, Any], str, str]], deadline: float) -> set[str]:
    sockets = {socket: _list_panes(socket, deadline) for socket in sorted({route[1] for route in routes})}
    verified: set[tuple[str, str]] = set()
    with process_snapshot():
        for row, socket, name in routes:
            if time.monotonic() >= deadline:
                raise AgentDeckError("Agent Deck inventory timed out")
            providers = [provider for pane in sockets[socket].get(name, ())
                         if (provider := _foreground_provider(pane)) is not None]
            if not providers:
                continue
            if len(providers) != 1 or providers[0] != row["tool"] or (socket, name) in verified:
                raise AgentDeckError("ambiguous Agent Deck tmux route")
            row["open"] = True
            row["stopped"] = False
            verified.add((socket, name))
    return {name for socket, name in verified if socket == "default"}


def inventory(*, timeout: float = _TIMEOUT,
              visible_routes: set[str] | None = None) -> dict[str, Any]:
    """Return session references instead of copying Agent Deck's database."""
    binary = _required_executable()
    deadline = _deadline(timeout)
    profiles = _profiles(binary, deadline)
    sessions = []
    routes = []
    for profile in sorted(profiles):
        result = _json(binary, ["-p", profile, "list", "--json", "--include-superseded"], deadline, "inventory")
        if not isinstance(result, list):
            raise AgentDeckError("invalid Agent Deck session list")
        for row in result:
            session = _session(row, profile)
            sessions.append(session)
            if route := _route(row):
                routes.append((session, *route))
    verified_routes = _observe_open(routes, deadline) if routes else set()
    if _profiles(binary, deadline) != profiles:
        raise AgentDeckError("Agent Deck profiles changed during capture")
    record = {"version": 1, "sessions": sessions}
    try:
        validate(record)
    except ValueError as error:
        raise AgentDeckError(str(error)) from error
    if visible_routes is not None:
        visible_routes.update(verified_routes)
    return record


def start_exact(row: dict[str, Any], *, timeout: float) -> None:
    """Use the guarded Deck launch extension or report a pending session."""
    validate({"version": 1, "sessions": [row]})
    native = row["native_id"]
    if row["archived"] or native is None:
        raise AgentDeckError("Agent Deck session has no resumable native identity")
    result = _json(_required_executable(), [
        "-p", row["profile"], "session", "start", row["id"],
        "--expected-native-id", native, "--expected-native-tool", row["tool"],
        "--expected-account", row["account"], "--expected-group", row["group"], "--json",
    ], _deadline(timeout), "exact start")
    if (not isinstance(result, dict) or result.get("success") is not True
            or result.get("id") != row["id"] or result.get("native_id") != native
            or not _text(result.get("tmux")) or result.get("queued") or result.get("deferred")
            or result.get("status") in ("queued", "pending", "starting", "stopped", "degraded", "error")):
        raise AgentDeckError("Agent Deck exact start did not confirm the requested identity")


def validate_dashboard(recipe: Any) -> None:
    if (not isinstance(recipe, dict) or recipe.keys() != {"profile", "group"}
            or not _component(recipe["profile"]) or not _group(recipe["group"])):
        raise ValueError("invalid Agent Deck dashboard")


def _dashboard_flags(args: list[str]) -> dict[str, str] | None:
    names = {"-p": "profile", "--profile": "profile", "-g": "group", "--group": "group"}
    fields: dict[str, str] = {}
    tokens = iter(args)
    for arg in tokens:
        flag, separator, value = arg.partition("=")
        field = names.get(flag)
        if field is None or field in fields:
            return None
        value = value if separator else next(tokens, "")
        check = _component if field == "profile" else _group
        if not value or not check(value):
            return None
        fields[field] = value
    return fields


def _opened_profile(process: Path) -> str | None:
    # An unscoped CLI can select its profile through its environment or
    # config. Match the store opened by the TUI without reading either.
    _, data, legacy = _roots()
    folders = {(root / "profiles").resolve() for root in (data, legacy)}
    profiles = set()
    try:
        descriptors = list((process / "fd").iterdir())
    except OSError:
        return None
    for descriptor in descriptors:
        try:
            path = descriptor.readlink()
        except OSError:
            continue
        if (path.name == "state.db" and path.parent.parent in folders
                and _component(path.parent.name)):
            profiles.add(path.parent.name)
    return profiles.pop() if len(profiles) == 1 else None


def _matches_image(image: Path, binary: Path) -> bool:
    target = image.readlink()
    if target.name == "agent-deck":
        return image.samefile(binary)
    if target.name != "agent-deck (deleted)" or Path(str(target).removesuffix(" (deleted)")) != binary.resolve():
        return False
    # Atomic upgrades leave the still-running inode open. Accept it only at
    # the configured executable's actual path and with the same permissions.
    previous, installed = image.stat(), binary.stat()
    return (_trusted_executable(previous) and previous.st_uid == installed.st_uid
            and stat.S_IMODE(previous.st_mode) == stat.S_IMODE(installed.st_mode))


def _dashboard_process(process: Path, binary: Path) -> dict[str, str] | None:
    image = process / "exe"
    try:
        original_image = image.stat()
        if not _matches_image(image, binary):
            return None
        cmdline = (process / "cmdline").read_bytes()
        argv = [os.fsdecode(value) for value in cmdline.removesuffix(b"\0").split(b"\0")]
        if Path(argv[0]).name != "agent-deck":
            return None
        fields = _dashboard_flags(argv[1:])
        if fields is None:
            return None
        profile = fields.get("profile") or _opened_profile(process)
        current_image = image.stat()
        if (not profile or not os.path.samestat(original_image, current_image)
                or (process / "cmdline").read_bytes() != cmdline):
            return None
    except (OSError, ValueError):
        return None
    return {"profile": profile, "group": fields.get("group", "")}


def dashboard(pane_pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, str] | None:
    """Recognize the unique trusted Agent Deck dashboard in a foreground pane."""
    if not isinstance(pane_pid, int) or pane_pid <= 0 or (binary := _executable()) is None:
        return None
    found = []
    try:
        for pid, chain in _foreground_processes(pane_pid, proc_root):
            if (recipe := _dashboard_process(proc_root / str(pid), binary)) is None:
                continue
            if not all(_process_identity(member, proc_root) == identity for member, identity in chain.items()):
                return None
            found.append(recipe)
    except AgentDeckError:
        return None
    return found[0] if len(found) == 1 else None


def dashboard_command(recipe: dict[str, str]) -> list[str]:
    """Launch the same scoped dashboard, without starting a conversation."""
    validate_dashboard(recipe)
    command = ["env", "AGENT_DECK_ALLOW_OUTER_TMUX=1", str(_required_executable()), "-p", recipe["profile"]]
    if recipe["group"]:
        command.extend(["-g", recipe["group"]])
    return command
