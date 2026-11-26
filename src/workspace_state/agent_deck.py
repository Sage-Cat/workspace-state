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
from pathlib import Path
from typing import Any

from .util import CommandError


class AgentDeckError(CommandError):
    """Agent Deck could not provide a complete observation."""


_TIMEOUT = 10.0
_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
_OPEN = {"running", "waiting", "idle"}
_STATUSES = _OPEN | {"starting", "stopped", "queued", "error"}
_CALLER_ENV = {
    "TMUX", "TMUX_PANE", "CLAUDE_CONFIG_DIR", "CLAUDE_SECURESTORAGE_CONFIG_DIR",
    "CLAUDE_SESSION_ID", "CLAUDE_JOB_DIR", "CODEX_HOME", "CODEX_SESSION_ID",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY",
}
_SESSION_KEYS = frozenset({
    "profile", "id", "group", "account", "tool", "title", "native_id", "open", "archived",
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
        if (stat.S_ISREG(mode.st_mode) and mode.st_uid in (0, os.getuid())
                and not mode.st_mode & 0o022 and os.access(path, os.X_OK)):
            return path
    return None


def enabled() -> bool:
    """Keep Agent Deck ownership when a previously installed CLI is absent."""
    config, data, legacy = _roots()
    return _executable() is not None or any(
        path.exists() for path in (
            config / "config.toml", data / "profiles",
            legacy / "config.toml", legacy / "profiles", legacy / "sessions.json",
        )
    )


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
                or (native is not None and (row["tool"] not in {"claude", "codex"}
                    or not isinstance(native, str) or not _UUID.fullmatch(native)))
                or type(row["open"]) is not bool or type(row["archived"]) is not bool
                or (row["open"] and row["archived"])):
            raise ValueError("invalid Agent Deck session identity or state")
        identity = (row["profile"], row["id"])
        if identity in seen:
            raise ValueError("duplicate Agent Deck session identity")
        seen.add(identity)


def _deadline(timeout: float) -> float:
    if not math.isfinite(timeout) or timeout <= 0:
        raise AgentDeckError("Agent Deck deadline expired")
    return time.monotonic() + timeout


def _json(binary: Path, args: list[str], deadline: float, operation: str) -> Any:
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
        result = subprocess.run(
            [str(binary), *args], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=remaining, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise AgentDeckError(f"Agent Deck {operation} timed out") from error
    except OSError as error:
        raise AgentDeckError(f"could not run Agent Deck {operation}") from error
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
    status, source = row.get("status"), row.get("status_source")
    if not isinstance(status, str) or status not in _STATUSES:
        raise AgentDeckError("invalid Agent Deck session status")
    if not archived and (source not in ("live", "cached") or source == "cached" and status in _OPEN):
        raise AgentDeckError("Agent Deck session status could not be observed")
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
        "open": not archived and source == "live" and status in _OPEN and _text(row.get("tmux_session")),
        "archived": archived,
    }


def inventory(*, timeout: float = _TIMEOUT) -> dict[str, Any]:
    """Return session references instead of copying Agent Deck's database."""
    binary = _required_executable()
    deadline = _deadline(timeout)
    profiles = _profiles(binary, deadline)
    sessions = []
    for profile in sorted(profiles):
        result = _json(binary, ["-p", profile, "list", "--json", "--include-superseded"], deadline, "inventory")
        if not isinstance(result, list):
            raise AgentDeckError("invalid Agent Deck session list")
        sessions.extend(_session(row, profile) for row in result)
    if _profiles(binary, deadline) != profiles:
        raise AgentDeckError("Agent Deck profiles changed during capture")
    record = {"version": 1, "sessions": sessions}
    try:
        validate(record)
    except ValueError as error:
        raise AgentDeckError(str(error)) from error
    return record


def start_exact(row: dict[str, Any], *, timeout: float) -> None:
    """Use the guarded Deck launch extension or report a pending session."""
    validate({"version": 1, "sessions": [row]})
    native = row["native_id"]
    if row["archived"] or native is None:
        raise AgentDeckError("Agent Deck session has no resumable native identity")
    result = _json(_required_executable(), [
        "-p", row["profile"], "session", "start", row["id"],
        "--expected-native-id", native, "--json",
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


def dashboard(pane_pid: int, *, proc_root: Path = Path("/proc")) -> dict[str, str] | None:
    """Recognize a directly launched, supported Agent Deck TUI command."""
    if not isinstance(pane_pid, int) or pane_pid <= 0:
        return None
    process = proc_root / str(pane_pid)
    image = process / "exe"
    try:
        if image.readlink().name != "agent-deck":
            return None
        binary = _executable()
        if binary is None or not image.samefile(binary):
            return None
        cmdline = (process / "cmdline").read_bytes()
        argv = [os.fsdecode(value) for value in cmdline.removesuffix(b"\0").split(b"\0")]
        if Path(argv[0]).name != "agent-deck":
            return None
        fields = _dashboard_flags(argv[1:])
        if fields is None:
            return None
        profile = fields.get("profile") or _opened_profile(process)
        if not profile or not image.samefile(binary) or (process / "cmdline").read_bytes() != cmdline:
            return None
    except (OSError, ValueError):
        return None
    return {"profile": profile, "group": fields.get("group", "")}


def dashboard_command(recipe: dict[str, str]) -> list[str]:
    """Launch the same scoped dashboard, without starting a conversation."""
    validate_dashboard(recipe)
    command = ["env", "AGENT_DECK_ALLOW_OUTER_TMUX=1", str(_required_executable()), "-p", recipe["profile"]]
    if recipe["group"]:
        command.extend(["-g", recipe["group"]])
    return command
