"""Declarative, recoverable jobs executed before GNOME ends the session."""

from __future__ import annotations

import hashlib
import json
import os
import re
import select
import signal
import socket
import stat
import subprocess
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .desktop import (
    capture_shell,
    move_window_result,
    remap_monitor,
    remap_workspace,
)
from .login_status import append_diagnostic, runtime_root, state_root, update_stage
from .util import CommandError, atomic_json


SCHEMA_VERSION = 1
MAX_PROFILES = 16
MAX_CONFIG_BYTES = 128 * 1024
PROFILE_ID = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,46}[a-z0-9])?$")
SUPPORTED_ACTIONS = frozenset({"poweroff", "restart"})
SUPPORTED_ADAPTERS = frozenset({"command", "qemu-windows-hibernate"})
SUPPORTED_CANCEL_POLICIES = frozenset(
    {"terminate-then-rollback", "finish-then-rollback"}
)
TRANSACTION_FILENAME = "shutdown-profile-transaction.json"
STARTUP_RESTORE_FILENAME = "startup-profile-restore.json"
PROFILE_PREFLIGHT_FILENAME = "shutdown-profile-preflight.json"
QEMU_VIEWER_APP_ID = "org.virt-manager.virt-viewer"
QEMU_VIEWER_CLASS = "remote-viewer"
STARTUP_RESTORE_SCHEMA_VERSION = 1
QEMU_VIEWER_CAPTURE_TIMEOUT_SECONDS = 10.0

Reporter = Callable[[str, str, str], None]
Diagnostic = Callable[[str, str], bool]


class ShutdownProfileError(RuntimeError):
    """A profile is invalid or could not reach a verified safe state."""


class ShutdownProfilesCancelled(Exception):
    """Cancellation was observed while profile work was active."""


class CancellationProbe(Protocol):
    def requested(self) -> bool: ...


@dataclass(frozen=True)
class ShutdownProfile:
    identifier: str
    label: str
    adapter: str
    actions: frozenset[str]
    critical: bool
    timeout_seconds: float
    rollback_timeout_seconds: float
    cancel_policy: str
    source: Path | None = None
    probe: tuple[str, ...] | None = None
    prepare: tuple[str, ...] | None = None
    verify: tuple[str, ...] | None = None
    rollback: tuple[str, ...] | None = None
    adapter_config: dict[str, str] = field(default_factory=dict)

    @property
    def stage_id(self) -> str:
        return f"profile-{self.identifier}"


@dataclass
class ProfileRuntime:
    profile: ShutdownProfile
    state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StartupProfileRestoreOutcome:
    restored: int
    total: int
    message: str


def _config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


def profile_directories() -> tuple[Path, ...]:
    configured = os.environ.get("WSCTL_SHUTDOWN_PROFILE_DIRS")
    if configured is not None:
        return tuple(Path(item) for item in configured.split(os.pathsep) if item)
    return (
        Path("/etc/workspace-state/shutdown-profiles.d"),
        _config_home() / "workspace-state" / "shutdown-profiles.d",
    )


def transaction_path() -> Path:
    return runtime_root() / TRANSACTION_FILENAME


def startup_restore_path() -> Path:
    return state_root() / STARTUP_RESTORE_FILENAME


def profile_preflight_path() -> Path:
    return runtime_root() / PROFILE_PREFLIGHT_FILENAME


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError as error:
        raise ShutdownProfileError(f"could not read the kernel boot ID: {error}") from error
    if not value or len(value) > 128:
        raise ShutdownProfileError("the kernel boot ID is empty or invalid")
    return value


def _read_private_regular_file(
    path: Path,
    *,
    max_bytes: int = MAX_CONFIG_BYTES,
    allow_root_owner: bool = False,
) -> bytes:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as error:
        raise ShutdownProfileError(f"could not open {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        owners = {os.getuid()}
        if allow_root_owner:
            owners.add(0)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid not in owners
            or metadata.st_mode & 0o022
            or metadata.st_size > max_bytes
        ):
            raise ShutdownProfileError(
                f"{path} must be a non-writable-by-others regular file owned by "
                "the current user or root and no larger than "
                f"{max_bytes} bytes"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read(max_bytes + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ShutdownProfileError(f"{field_name} must be a non-empty string")
    return value


def _number(
    value: Any,
    field_name: str,
    *,
    minimum: float = 1.0,
    maximum: float = 300.0,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShutdownProfileError(f"{field_name} must be a number")
    result = float(value)
    if not minimum <= result <= maximum:
        raise ShutdownProfileError(
            f"{field_name} must be between {minimum:g} and {maximum:g} seconds"
        )
    return result


def _command(value: Any, field_name: str) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 64
        or any(not isinstance(item, str) or not item or len(item) > 4096 for item in value)
    ):
        raise ShutdownProfileError(
            f"{field_name} must be an argv array containing 1 to 64 strings"
        )
    if not Path(value[0]).is_absolute():
        raise ShutdownProfileError(
            f"{field_name}[0] must be an absolute executable path"
        )
    return tuple(value)


def _profile_from_mapping(
    raw: Any,
    *,
    source: Path | None = None,
) -> ShutdownProfile:
    if not isinstance(raw, dict):
        raise ShutdownProfileError("profile document must be a table")
    allowed = {
        "schema_version", "id", "label", "adapter", "enabled", "actions",
        "critical", "timeout_seconds", "rollback_timeout_seconds",
        "cancel_policy", "probe", "prepare", "verify", "rollback",
        "adapter_config",
    }
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ShutdownProfileError("unknown profile keys: " + ", ".join(unknown))
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise ShutdownProfileError(
            f"profile schema_version must be {SCHEMA_VERSION}"
        )
    identifier = _string(raw.get("id"), "id")
    if not PROFILE_ID.fullmatch(identifier):
        raise ShutdownProfileError(
            "id must contain 1 to 48 lowercase ASCII letters, digits, or hyphens"
        )
    label = _string(raw.get("label"), "label")
    adapter = _string(raw.get("adapter"), "adapter")
    if adapter not in SUPPORTED_ADAPTERS:
        raise ShutdownProfileError(f"unsupported adapter: {adapter}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ShutdownProfileError("enabled must be a boolean")
    if not enabled:
        raise ShutdownProfileError("disabled")
    actions_raw = raw.get("actions", sorted(SUPPORTED_ACTIONS))
    if (
        not isinstance(actions_raw, list)
        or not actions_raw
        or any(not isinstance(item, str) for item in actions_raw)
        or not set(actions_raw) <= SUPPORTED_ACTIONS
    ):
        raise ShutdownProfileError(
            "actions must be a non-empty subset of poweroff and restart"
        )
    critical = raw.get("critical", True)
    if not isinstance(critical, bool):
        raise ShutdownProfileError("critical must be a boolean")
    timeout = _number(raw.get("timeout_seconds", 180), "timeout_seconds")
    rollback_timeout = _number(
        raw.get("rollback_timeout_seconds", timeout),
        "rollback_timeout_seconds",
    )
    default_cancel = (
        "finish-then-rollback"
        if adapter == "qemu-windows-hibernate"
        else "terminate-then-rollback"
    )
    cancel_policy = _string(
        raw.get("cancel_policy", default_cancel), "cancel_policy"
    )
    if cancel_policy not in SUPPORTED_CANCEL_POLICIES:
        raise ShutdownProfileError(
            "cancel_policy must be terminate-then-rollback or finish-then-rollback"
        )

    command_fields = {}
    for name in ("probe", "prepare", "verify", "rollback"):
        value = raw.get(name)
        command_fields[name] = _command(value, name) if value is not None else None
    adapter_config_raw = raw.get("adapter_config", {})
    if not isinstance(adapter_config_raw, dict):
        raise ShutdownProfileError("adapter_config must be a table")

    if adapter == "command":
        if adapter_config_raw:
            raise ShutdownProfileError("command profiles do not use adapter_config")
        missing = [name for name, value in command_fields.items() if value is None]
        if missing:
            raise ShutdownProfileError(
                "command profiles require probe, prepare, verify, and rollback: "
                + ", ".join(missing)
            )
    else:
        if any(value is not None for value in command_fields.values()):
            raise ShutdownProfileError(
                "qemu-windows-hibernate profiles use adapter_config, not command fields"
            )
        if set(adapter_config_raw) != {"vm_directory"}:
            raise ShutdownProfileError(
                "qemu-windows-hibernate adapter_config requires only vm_directory"
            )
        vm_directory = Path(
            _string(adapter_config_raw.get("vm_directory"), "adapter_config.vm_directory")
        )
        if not vm_directory.is_absolute():
            raise ShutdownProfileError("adapter_config.vm_directory must be absolute")
        if cancel_policy != "finish-then-rollback":
            raise ShutdownProfileError(
                "qemu-windows-hibernate requires finish-then-rollback cancellation"
            )

    return ShutdownProfile(
        identifier=identifier,
        label=label,
        adapter=adapter,
        actions=frozenset(actions_raw),
        critical=critical,
        timeout_seconds=timeout,
        rollback_timeout_seconds=rollback_timeout,
        cancel_policy=cancel_policy,
        source=source,
        adapter_config={str(key): str(value) for key, value in adapter_config_raw.items()},
        **command_fields,
    )


def load_profiles() -> list[ShutdownProfile]:
    profiles: list[ShutdownProfile] = []
    identifiers: set[str] = set()
    for directory in profile_directories():
        try:
            directory_metadata = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ShutdownProfileError(f"could not inspect {directory}: {error}") from error
        if (
            not stat.S_ISDIR(directory_metadata.st_mode)
            or directory.is_symlink()
            or directory_metadata.st_uid not in {0, os.getuid()}
            or directory_metadata.st_mode & 0o022
        ):
            raise ShutdownProfileError(
                f"shutdown profile directory is unsafe: {directory}"
            )
        try:
            candidates = sorted(directory.glob("*.toml"))
        except OSError as error:
            raise ShutdownProfileError(f"could not list {directory}: {error}") from error
        for path in candidates:
            try:
                data = _read_private_regular_file(path, allow_root_owner=True)
                raw = tomllib.loads(data.decode("utf-8"))
                profile = _profile_from_mapping(raw, source=path)
            except ShutdownProfileError as error:
                if str(error) == "disabled":
                    continue
                raise ShutdownProfileError(f"invalid shutdown profile {path}: {error}") from error
            except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                raise ShutdownProfileError(f"invalid shutdown profile {path}: {error}") from error
            if profile.identifier in identifiers:
                raise ShutdownProfileError(
                    f"duplicate shutdown profile id: {profile.identifier}"
                )
            identifiers.add(profile.identifier)
            profiles.append(profile)
            if len(profiles) > MAX_PROFILES:
                raise ShutdownProfileError(
                    f"no more than {MAX_PROFILES} shutdown profiles are allowed"
                )
    return profiles


def _profile_mapping(profile: ShutdownProfile) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "id": profile.identifier,
        "label": profile.label,
        "adapter": profile.adapter,
        "actions": sorted(profile.actions),
        "critical": profile.critical,
        "timeout_seconds": profile.timeout_seconds,
        "rollback_timeout_seconds": profile.rollback_timeout_seconds,
        "cancel_policy": profile.cancel_policy,
    }
    for name in ("probe", "prepare", "verify", "rollback"):
        value = getattr(profile, name)
        if value is not None:
            result[name] = list(value)
    if profile.adapter_config:
        result["adapter_config"] = dict(profile.adapter_config)
    return result


def _validate_qemu_placement(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ShutdownProfileError("Windows VM restore placement must be an object")
    workspace = value.get("workspace")
    workspace_name = value.get("workspace_name")
    monitor = value.get("monitor")
    state = value.get("state")
    geometry = value.get("geometry")
    monitor_geometry = value.get("monitor_geometry")
    identity = value.get("monitor_intent") or value.get("monitor_identity")
    if isinstance(workspace, bool) or not isinstance(workspace, int) or workspace < 0:
        raise ShutdownProfileError("Windows VM restore workspace is invalid")
    if (
        not isinstance(workspace_name, str)
        or not workspace_name
        or len(workspace_name) > 4096
    ):
        raise ShutdownProfileError("Windows VM restore workspace name is missing")
    if isinstance(monitor, bool) or not isinstance(monitor, int) or monitor < 0:
        raise ShutdownProfileError("Windows VM restore monitor index is invalid")
    if state not in {"normal", "maximized", "fullscreen", "minimized"}:
        raise ShutdownProfileError("Windows VM restore window state is invalid")
    stable_identity = (
        identity.get("edid_hash")
        or identity.get("edid_checksum")
        or identity.get("serial")
        if isinstance(identity, dict)
        else None
    )
    if (
        not isinstance(identity, dict)
        or not isinstance(stable_identity, str)
        or not stable_identity
        or len(stable_identity) > 4096
    ):
        raise ShutdownProfileError(
            "Windows VM viewer has no stable physical display identity"
        )
    if not isinstance(geometry, dict) or any(
        isinstance(geometry.get(key), bool)
        or not isinstance(geometry.get(key), int)
        for key in ("x", "y", "width", "height")
    ) or geometry["width"] <= 0 or geometry["height"] <= 0:
        raise ShutdownProfileError("Windows VM restore geometry is invalid")
    if not isinstance(monitor_geometry, dict) or any(
        isinstance(monitor_geometry.get(key), bool)
        or not isinstance(monitor_geometry.get(key), int)
        for key in ("x", "y", "width", "height")
    ) or (
        monitor_geometry["width"] <= 0
        or monitor_geometry["height"] <= 0
        or not isinstance(monitor_geometry.get("connector"), str)
        or not monitor_geometry["connector"]
    ):
        raise ShutdownProfileError("Windows VM restore monitor geometry is invalid")
    result = {
        "workspace": workspace,
        "workspace_name": workspace_name,
        "monitor": monitor,
        "monitor_identity": dict(identity),
        "monitor_intent": dict(identity),
        "monitor_geometry": dict(monitor_geometry),
        "geometry": dict(geometry),
        "state": state,
    }
    relative = value.get("geometry_relative")
    if isinstance(relative, dict) and all(
        isinstance(relative.get(key), int)
        and not isinstance(relative.get(key), bool)
        for key in ("x", "y", "width", "height")
    ) and relative["width"] > 0 and relative["height"] > 0:
        result["geometry_relative"] = dict(relative)
    return result


def _runtime_restore_intents(runtimes: list[ProfileRuntime]) -> list[dict[str, Any]]:
    intents: list[dict[str, Any]] = []
    for runtime in runtimes:
        placement = runtime.state.get("restore_placement")
        if placement is None:
            continue
        if runtime.profile.adapter != "qemu-windows-hibernate":
            raise ShutdownProfileError(
                f"profile {runtime.profile.identifier} cannot publish a startup restore intent"
            )
        intents.append({
            "profile_id": runtime.profile.identifier,
            "placement": _validate_qemu_placement(placement),
        })
    return intents


def _transaction_document(
    operation_id: str,
    session_id: str,
    action: str,
    runtimes: list[ProfileRuntime],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "operation_id": operation_id,
        "session_id": session_id,
        "action": action,
        "updated_at": time.time(),
        "profiles": [_profile_mapping(runtime.profile) for runtime in runtimes],
        "restore_intents": _runtime_restore_intents(runtimes),
    }


def _read_transaction(operation_id: str) -> tuple[dict[str, Any], list[ProfileRuntime]] | None:
    path = transaction_path()
    if not path.exists():
        return None
    data = _read_private_regular_file(path, max_bytes=512 * 1024)
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShutdownProfileError(f"invalid {path.name}: {error}") from error
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != SCHEMA_VERSION
        or document.get("operation_id") != operation_id
        or document.get("action") not in SUPPORTED_ACTIONS
        or not isinstance(document.get("session_id"), str)
        or not isinstance(document.get("profiles"), list)
        or len(document["profiles"]) > MAX_PROFILES
    ):
        raise ShutdownProfileError(
            f"{path.name} is malformed or belongs to another shutdown operation"
        )
    runtimes = [
        ProfileRuntime(_profile_from_mapping(item))
        for item in document["profiles"]
    ]
    by_identifier = {runtime.profile.identifier: runtime for runtime in runtimes}
    restore_intents = document.get("restore_intents", [])
    if not isinstance(restore_intents, list) or len(restore_intents) > len(runtimes):
        raise ShutdownProfileError(f"{path.name} has invalid startup restore intents")
    seen_intents: set[str] = set()
    for intent in restore_intents:
        if not isinstance(intent, dict) or set(intent) != {"profile_id", "placement"}:
            raise ShutdownProfileError(f"{path.name} has a malformed startup restore intent")
        identifier = intent.get("profile_id")
        runtime = by_identifier.get(identifier) if isinstance(identifier, str) else None
        if (
            runtime is None
            or identifier in seen_intents
            or runtime.profile.adapter != "qemu-windows-hibernate"
        ):
            raise ShutdownProfileError(f"{path.name} has an unknown startup restore intent")
        runtime.state["restore_placement"] = _validate_qemu_placement(
            intent.get("placement")
        )
        seen_intents.add(identifier)
    return document, runtimes


def transaction_exists(operation_id: str) -> bool:
    try:
        return _read_transaction(operation_id) is not None
    except ShutdownProfileError:
        return True


def _startup_restore_document(
    operation_id: str,
    session_id: str,
    action: str,
    runtimes: list[ProfileRuntime],
) -> dict[str, Any]:
    entries = []
    for runtime in runtimes:
        placement = runtime.state.get("restore_placement")
        if placement is None:
            continue
        entries.append({
            "profile": _profile_mapping(runtime.profile),
            "placement": _validate_qemu_placement(placement),
        })
    return {
        "schema_version": STARTUP_RESTORE_SCHEMA_VERSION,
        "operation_id": operation_id,
        "session_id": session_id,
        "action": action,
        "source_boot_id": _boot_id(),
        "committed_at": time.time(),
        "restored_boot_id": None,
        "entries": entries,
    }


def _discard_startup_restore(operation_id: str) -> None:
    path = startup_restore_path()
    try:
        document = json.loads(_read_private_regular_file(path, max_bytes=512 * 1024))
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ShutdownProfileError):
        return
    if isinstance(document, dict) and document.get("operation_id") == operation_id:
        path.unlink(missing_ok=True)


def disarm_transaction(
    operation_id: str,
    *,
    action: str | None = None,
    session_id: str | None = None,
) -> bool:
    """Commit startup restore intent and remove rollback at final EndSession."""
    try:
        transaction = _read_transaction(operation_id)
        if transaction is None:
            if action is None or session_id is None:
                return True
            runtimes: list[ProfileRuntime] = []
        else:
            document, runtimes = transaction
            if action is not None and action != document["action"]:
                raise ShutdownProfileError("shutdown action changed before EndSession")
            if session_id is not None and session_id != document["session_id"]:
                raise ShutdownProfileError("GNOME session changed before EndSession")
            action = str(document["action"])
            session_id = str(document["session_id"])
        if action not in SUPPORTED_ACTIONS or not isinstance(session_id, str) or not session_id:
            raise ShutdownProfileError("missing final shutdown transaction context")
        atomic_json(
            startup_restore_path(),
            _startup_restore_document(
                operation_id,
                session_id,
                action,
                runtimes,
            ),
        )
        startup_restore_path().chmod(0o600)
        if transaction is not None:
            transaction_path().unlink()
        return True
    except (OSError, ShutdownProfileError) as error:
        try:
            _discard_startup_restore(operation_id)
        except OSError:
            pass
        append_diagnostic("shutdown profile disarm", str(error))
        return False


def _write_transaction(
    operation_id: str,
    session_id: str,
    action: str,
    runtimes: list[ProfileRuntime],
) -> None:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    if runtimes:
        if transaction_path().exists():
            # A different operation's journal is evidence of unfinished
            # recovery and must never be overwritten by a new shutdown.
            _read_transaction(operation_id)
        atomic_json(
            transaction_path(),
            _transaction_document(operation_id, session_id, action, runtimes),
        )
        transaction_path().chmod(0o600)
    else:
        transaction_path().unlink(missing_ok=True)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def _run_external(
    command: tuple[str, ...] | list[str],
    *,
    label: str,
    timeout: float,
    cancel: CancellationProbe | None = None,
    defer_cancel: bool = False,
) -> tuple[int, str]:
    with tempfile.TemporaryFile() as output:
        try:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            raise ShutdownProfileError(f"could not start {label}: {error}") from error
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            if cancel is not None and cancel.requested() and not defer_cancel:
                _terminate_process_group(process)
                raise ShutdownProfilesCancelled
            if time.monotonic() >= deadline:
                _terminate_process_group(process)
                raise ShutdownProfileError(f"{label} exceeded {timeout:g} seconds")
            time.sleep(0.1)
        output.seek(0, os.SEEK_END)
        size = output.tell()
        output.seek(max(0, size - 32_000))
        text = output.read().decode("utf-8", errors="replace").rstrip()
        return int(process.returncode or 0), text


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    start_time: int


def _proc_start_time(pid: int) -> int | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        return int(raw[closing + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _live_qemu(vm_directory: Path) -> ProcessIdentity | None:
    pid_file = vm_directory / "run" / "qemu.pid"
    try:
        descriptor = os.open(pid_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="ascii") as stream:
            metadata = os.fstat(stream.fileno())
            raw_pid = stream.read(32).strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ShutdownProfileError(f"could not inspect {pid_file}: {error}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not raw_pid.isascii()
        or not raw_pid.isdecimal()
    ):
        raise ShutdownProfileError(f"unsafe or invalid QEMU PID file: {pid_file}")
    pid = int(raw_pid)
    start_time = _proc_start_time(pid)
    if start_time is None:
        return None
    expected_executable = (vm_directory / "tools" / "qemu-system-x86_64-smb").resolve()
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        command = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError as error:
        if _proc_start_time(pid) is None:
            return None
        raise ShutdownProfileError(f"could not verify QEMU PID {pid}: {error}") from error
    required_arguments = {
        str(vm_directory / "run" / "qmp.sock").encode(),
        str(vm_directory / "disk" / "windows10-22h2.qcow2").encode(),
    }
    joined = b"\0".join(command)
    if executable != expected_executable or any(item not in joined for item in required_arguments):
        raise ShutdownProfileError(
            f"PID file {pid_file} points to an unexpected process"
        )
    return ProcessIdentity(pid, start_time)


def _same_process(identity: ProcessIdentity) -> bool:
    return _proc_start_time(identity.pid) == identity.start_time


def _live_qemu_viewer(vm_directory: Path) -> ProcessIdentity | None:
    pid_file = vm_directory / "run" / "remote-viewer.pid"
    try:
        descriptor = os.open(pid_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="ascii") as stream:
            metadata = os.fstat(stream.fileno())
            raw_pid = stream.read(32).strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ShutdownProfileError(f"could not inspect {pid_file}: {error}") from error
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not raw_pid.isascii()
        or not raw_pid.isdecimal()
    ):
        raise ShutdownProfileError(f"unsafe or invalid viewer PID file: {pid_file}")
    pid = int(raw_pid)
    started = _proc_start_time(pid)
    if started is None:
        return None
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        command = [
            item.decode("utf-8", errors="replace")
            for item in Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if item
        ]
    except OSError as error:
        if _proc_start_time(pid) is None:
            return None
        raise ShutdownProfileError(f"could not verify viewer PID {pid}: {error}") from error
    expected_uri = f"spice+unix://{vm_directory / 'run' / 'spice.sock'}"
    if executable.name != QEMU_VIEWER_CLASS or expected_uri not in command:
        raise ShutdownProfileError(
            f"viewer PID file {pid_file} points to an unexpected process"
        )
    return ProcessIdentity(pid, started)


def _qemu_viewer_window(
    vm_directory: Path,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    identity = _live_qemu_viewer(vm_directory)
    if identity is None:
        return None
    matches = []
    for window in (shell or capture_shell()).get("windows", []):
        if not isinstance(window, dict) or window.get("pid") != identity.pid:
            continue
        app_ids_value = window.get("app_ids", [])
        app_ids = app_ids_value if isinstance(app_ids_value, list) else []
        if (
            window.get("app_id") == QEMU_VIEWER_APP_ID
            or QEMU_VIEWER_APP_ID in app_ids
            or window.get("wm_class") == QEMU_VIEWER_CLASS
            or QEMU_VIEWER_CLASS in app_ids
        ):
            matches.append(window)
    if len(matches) > 1:
        raise ShutdownProfileError("the Windows VM has multiple viewer windows")
    return dict(matches[0]) if matches else None


def _capture_qemu_restore_placement(
    vm_directory: Path,
    *,
    timeout: float = QEMU_VIEWER_CAPTURE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Wait through the brief Shell transition after the native power dialog."""
    deadline = time.monotonic() + max(0.0, timeout)
    viewer_identity = _live_qemu_viewer(vm_directory)
    if viewer_identity is None:
        raise ShutdownProfileError(
            "Windows VM is running without its verified remote-viewer process"
        )
    last_error = (
        f"GNOME has not exposed remote-viewer PID {viewer_identity.pid} yet"
    )
    while True:
        shell = capture_shell()
        if shell.get("available") is False:
            last_error = "the GNOME placement service is temporarily unavailable"
        else:
            viewer = _qemu_viewer_window(vm_directory, shell)
            if viewer is None:
                last_error = (
                    f"GNOME did not expose remote-viewer PID {viewer_identity.pid}"
                )
            else:
                workspace = viewer.get("workspace")
                names = {
                    int(item["index"]): str(item["name"])
                    for item in shell.get("workspaces", [])
                    if isinstance(item, dict)
                    and isinstance(item.get("index"), int)
                    and isinstance(item.get("name"), str)
                }
                if not isinstance(workspace, int) or workspace not in names:
                    last_error = (
                        "Windows VM viewer workspace has no stable GNOME name"
                    )
                elif list(names.values()).count(names[workspace]) != 1:
                    raise ShutdownProfileError(
                        "Windows VM viewer workspace name is not unique in GNOME"
                    )
                else:
                    physical_identity = viewer.get("monitor_identity")
                    placement = {
                        "workspace": workspace,
                        "workspace_name": names[workspace],
                        "monitor": viewer.get("monitor"),
                        "monitor_identity": physical_identity,
                        "monitor_intent": physical_identity,
                        "monitor_geometry": viewer.get("monitor_geometry"),
                        "geometry": viewer.get("geometry"),
                        "geometry_relative": viewer.get("geometry_relative"),
                        "state": viewer.get("state"),
                    }
                    try:
                        return _validate_qemu_placement(placement)
                    except ShutdownProfileError as error:
                        last_error = str(error)
        if time.monotonic() >= deadline:
            raise ShutdownProfileError(
                "could not capture the Windows VM viewer placement after "
                f"{max(0.0, timeout):g} seconds: {last_error}"
            )
        time.sleep(0.25)


def _process_identity_mapping(identity: ProcessIdentity) -> dict[str, int]:
    return {"pid": identity.pid, "start_time": identity.start_time}


def _process_identity_from_mapping(value: Any, label: str) -> ProcessIdentity:
    if (
        not isinstance(value, dict)
        or set(value) != {"pid", "start_time"}
        or isinstance(value.get("pid"), bool)
        or not isinstance(value.get("pid"), int)
        or value["pid"] <= 0
        or isinstance(value.get("start_time"), bool)
        or not isinstance(value.get("start_time"), int)
        or value["start_time"] <= 0
    ):
        raise ShutdownProfileError(f"invalid {label} process identity")
    return ProcessIdentity(int(value["pid"]), int(value["start_time"]))


def capture_shutdown_profile_preflight(
    profiles: list[ShutdownProfile],
    *,
    operation_id: str,
    session_id: str,
    action: str,
) -> None:
    """Capture graphical VM state before publishing the modal shutdown HUD."""
    entries: list[dict[str, Any]] = []
    for profile in profiles:
        if action not in profile.actions or profile.adapter != "qemu-windows-hibernate":
            continue
        vm_directory = Path(profile.adapter_config["vm_directory"])
        qemu_identity = _live_qemu(vm_directory)
        entry: dict[str, Any] = {
            "profile_id": profile.identifier,
            "profile_fingerprint": profile_fingerprint(profile),
            "active": qemu_identity is not None,
        }
        if qemu_identity is not None:
            viewer_identity = _live_qemu_viewer(vm_directory)
            if viewer_identity is None:
                raise ShutdownProfileError(
                    f"{profile.label} is active without its verified remote-viewer process"
                )
            placement = _capture_qemu_restore_placement(
                vm_directory,
                timeout=min(5.0, profile.timeout_seconds),
            )
            if not _same_process(qemu_identity):
                raise ShutdownProfileError(
                    f"{profile.label} changed during pre-HUD placement capture"
                )
            entry.update({
                "qemu_identity": _process_identity_mapping(qemu_identity),
                "viewer_identity": _process_identity_mapping(viewer_identity),
                "placement": placement,
            })
        entries.append(entry)
    atomic_json(profile_preflight_path(), {
        "schema_version": 1,
        "operation_id": operation_id,
        "session_id": session_id,
        "action": action,
        "created_at": time.time(),
        "entries": entries,
    })


def load_shutdown_profile_preflight(
    profiles: list[ShutdownProfile],
    *,
    operation_id: str,
    session_id: str,
    action: str,
) -> dict[str, dict[str, Any]]:
    """Load the operation-bound state captured before Shell acquired modal input."""
    expected = {
        profile.identifier: profile
        for profile in profiles
        if action in profile.actions and profile.adapter == "qemu-windows-hibernate"
    }
    if not expected:
        return {}
    path = profile_preflight_path()
    data = _read_private_regular_file(path, max_bytes=512 * 1024)
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShutdownProfileError(f"invalid {path.name}: {error}") from error
    created_at = document.get("created_at") if isinstance(document, dict) else None
    entries = document.get("entries") if isinstance(document, dict) else None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != 1
        or document.get("operation_id") != operation_id
        or document.get("session_id") != session_id
        or document.get("action") != action
        or isinstance(created_at, bool)
        or not isinstance(created_at, (int, float))
        or not 0 <= time.time() - created_at <= 60
        or not isinstance(entries, list)
        or len(entries) != len(expected)
    ):
        raise ShutdownProfileError(
            f"{path.name} is stale, malformed, or belongs to another shutdown"
        )
    states: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ShutdownProfileError(f"{path.name} has a malformed profile entry")
        identifier = entry.get("profile_id")
        profile = expected.get(identifier) if isinstance(identifier, str) else None
        active = entry.get("active")
        allowed = {"profile_id", "profile_fingerprint", "active"}
        if active is True:
            allowed |= {"qemu_identity", "viewer_identity", "placement"}
        if (
            profile is None
            or identifier in states
            or not isinstance(active, bool)
            or set(entry) != allowed
            or entry.get("profile_fingerprint") != profile_fingerprint(profile)
        ):
            raise ShutdownProfileError(f"{path.name} has an invalid profile entry")
        state: dict[str, Any] = {"preflight_active": active}
        if active:
            state.update({
                "preflight_qemu_identity": _process_identity_from_mapping(
                    entry.get("qemu_identity"), "QEMU",
                ),
                "preflight_viewer_identity": _process_identity_from_mapping(
                    entry.get("viewer_identity"), "remote-viewer",
                ),
                "restore_placement": _validate_qemu_placement(
                    entry.get("placement")
                ),
            })
        states[identifier] = state
    if set(states) != set(expected):
        raise ShutdownProfileError(f"{path.name} is missing a configured VM profile")
    return states


def _resolved_qemu_placement(
    vm_directory: Path,
    placement: dict[str, Any],
) -> dict[str, Any]:
    validated = _validate_qemu_placement(placement)
    workspace_name = validated["workspace_name"]
    shell = capture_shell()
    workspace_items = [
        (str(item["name"]), int(item["index"]))
        for item in shell.get("workspaces", [])
        if isinstance(item, dict)
        and isinstance(item.get("index"), int)
        and isinstance(item.get("name"), str)
    ]
    matching_workspaces = [
        index for name, index in workspace_items if name == workspace_name
    ]
    if not matching_workspaces:
        raise ShutdownProfileError(
            f"saved Windows VM workspace is unavailable: {workspace_name}"
        )
    if len(matching_workspaces) != 1:
        raise ShutdownProfileError(
            f"saved Windows VM workspace name is ambiguous: {workspace_name}"
        )
    try:
        target = remap_monitor(
            remap_workspace(validated),
            require_identity=True,
        )
    except CommandError as error:
        raise ShutdownProfileError(str(error)) from error
    target["workspace"] = matching_workspaces[0]
    target["workspace_name"] = workspace_name
    monitor_geometry = target.get("monitor_geometry") or {}
    connector = monitor_geometry.get("connector")
    if not isinstance(connector, str) or not connector:
        identity = target.get("monitor_identity") or {}
        connector = identity.get("connector")
    if not isinstance(connector, str) or not connector:
        raise ShutdownProfileError("resolved Windows VM display has no connector")
    geometry = target["geometry"]
    viewer_geometry = [
        int(geometry["x"]) - int(monitor_geometry["x"]),
        int(geometry["y"]) - int(monitor_geometry["y"]),
        int(geometry["width"]),
        int(geometry["height"]),
    ]
    atomic_json(vm_directory / "viewer-placement.json", {
        "workspace": int(target["workspace"]),
        "monitor": connector,
        "geometry": viewer_geometry,
        "state": target["state"],
    })
    return target


def _place_qemu_viewer(
    vm_directory: Path,
    target: dict[str, Any],
    *,
    timeout: float = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(1.0, timeout)
    last_error = "viewer window has not appeared"
    while time.monotonic() < deadline:
        viewer = _qemu_viewer_window(vm_directory)
        if viewer is None:
            time.sleep(0.25)
            continue
        window_id = viewer.get("id")
        if not isinstance(window_id, int):
            last_error = "viewer window has no stable GNOME ID"
            time.sleep(0.25)
            continue
        shell = capture_shell()
        try:
            active_workspace = int(shell.get("active_workspace"))
            target_workspace = int(target["workspace"])
        except (TypeError, ValueError):
            active_workspace = target_workspace = -1
        if active_workspace >= 0 and active_workspace != target_workspace:
            staging = dict(target)
            staging["workspace"] = active_workspace
            staging.pop("workspace_name", None)
            if not move_window_result(window_id, staging).get("placed"):
                last_error = "viewer window staging failed"
                time.sleep(0.25)
                continue
        result = move_window_result(window_id, target)
        if not result.get("placed"):
            last_error = "GNOME rejected the viewer placement"
            time.sleep(0.25)
            continue
        verification_deadline = min(deadline, time.monotonic() + 5)
        while time.monotonic() < verification_deadline:
            current = _qemu_viewer_window(vm_directory)
            if current is not None and all((
                current.get("workspace") == target.get("workspace"),
                current.get("monitor") == target.get("monitor"),
                current.get("state") == target.get("state"),
            )):
                return current
            time.sleep(0.25)
        last_error = "viewer did not reach the saved workspace, display, and state"
    raise ShutdownProfileError(last_error)


class _JsonSocket:
    def __init__(self, path: Path, *, qmp: bool) -> None:
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(8)
        self.socket.connect(str(path))
        self.buffer = bytearray()
        self.qmp = qmp
        if qmp:
            greeting = self._read()
            if "QMP" not in greeting:
                raise ShutdownProfileError("unexpected QMP greeting")
            self.request("qmp_capabilities")

    def close(self) -> None:
        self.socket.close()

    def _read(self) -> dict[str, Any]:
        deadline = time.monotonic() + 8
        while True:
            if b"\n" in self.buffer:
                raw, _, remainder = self.buffer.partition(b"\n")
                self.buffer = bytearray(remainder)
                raw = raw.lstrip(b"\xff\x00\r\n")
                if not raw:
                    continue
                response = json.loads(raw)
                if self.qmp and "event" in response:
                    continue
                return response
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("guest control socket did not respond")
            readable, _, _ = select.select([self.socket], [], [], remaining)
            if not readable:
                raise TimeoutError("guest control socket did not respond")
            chunk = self.socket.recv(65536)
            if not chunk:
                raise ConnectionError("guest control socket closed")
            self.buffer.extend(chunk)

    def request(self, command: str, arguments: dict[str, Any] | None = None) -> Any:
        payload: dict[str, Any] = {"execute": command}
        if arguments is not None:
            payload["arguments"] = arguments
        self.socket.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        response = self._read()
        if "error" in response:
            raise ShutdownProfileError(
                f"{command} failed: {json.dumps(response['error'], sort_keys=True)}"
            )
        return response.get("return")


def _socket_request(
    path: Path,
    command: str,
    arguments: dict[str, Any] | None = None,
    *,
    qmp: bool = False,
) -> Any:
    try:
        client = _JsonSocket(path, qmp=qmp)
        try:
            return client.request(command, arguments)
        finally:
            client.close()
    except (ConnectionError, OSError, TimeoutError, ValueError, json.JSONDecodeError) as error:
        raise ShutdownProfileError(f"{path.name} command {command} failed: {error}") from error


def _qmp_status(vm_directory: Path) -> str:
    result = _socket_request(
        vm_directory / "run" / "qmp.sock", "query-status", qmp=True
    )
    if not isinstance(result, dict) or not isinstance(result.get("status"), str):
        raise ShutdownProfileError("QMP query-status returned malformed data")
    return str(result["status"])


def _qga_ping(vm_directory: Path) -> None:
    _socket_request(vm_directory / "run" / "qga.sock", "guest-ping")


def _qga_hibernate(vm_directory: Path) -> int:
    result = _socket_request(
        vm_directory / "run" / "qga.sock",
        "guest-exec",
        {
            "path": "shutdown.exe",
            "arg": ["/h"],
            "capture-output": True,
        },
    )
    if not isinstance(result, dict) or not isinstance(result.get("pid"), int):
        raise ShutdownProfileError("QGA did not acknowledge the Windows hibernate command")
    return int(result["pid"])


def _qga_exec_status(vm_directory: Path, guest_pid: int) -> dict[str, Any]:
    result = _socket_request(
        vm_directory / "run" / "qga.sock",
        "guest-exec-status",
        {"pid": guest_pid},
    )
    if not isinstance(result, dict):
        raise ShutdownProfileError("QGA guest-exec-status returned malformed data")
    return result


class _Adapter(Protocol):
    def probe(self, runtime: ProfileRuntime) -> tuple[bool, str]: ...
    def prepare(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str: ...
    def verify(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str: ...
    def rollback(self, runtime: ProfileRuntime) -> str: ...


class CommandAdapter:
    def _phase(
        self,
        runtime: ProfileRuntime,
        phase: str,
        *,
        timeout: float,
        cancel: CancellationProbe | None = None,
        defer_cancel: bool = False,
    ) -> tuple[int, str]:
        command = getattr(runtime.profile, phase)
        if command is None:
            raise ShutdownProfileError(f"{runtime.profile.identifier} has no {phase} command")
        return _run_external(
            command,
            label=f"{runtime.profile.label} {phase}",
            timeout=timeout,
            cancel=cancel,
            defer_cancel=defer_cancel,
        )

    def probe(self, runtime: ProfileRuntime) -> tuple[bool, str]:
        status, output = self._phase(
            runtime, "probe", timeout=runtime.profile.timeout_seconds
        )
        if output:
            append_diagnostic(f"{runtime.profile.label} probe", output)
        if status == 3:
            return False, output or "Profile is not active"
        if status:
            raise ShutdownProfileError(f"probe exited with status {status}")
        return True, output or "Profile is active"

    def prepare(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str:
        status, output = self._phase(
            runtime,
            "prepare",
            timeout=runtime.profile.timeout_seconds,
            cancel=cancel,
            defer_cancel=runtime.profile.cancel_policy == "finish-then-rollback",
        )
        if output:
            append_diagnostic(f"{runtime.profile.label} prepare", output)
        if status:
            raise ShutdownProfileError(f"prepare exited with status {status}")
        return output or "Prepare command completed"

    def verify(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str:
        status, output = self._phase(
            runtime,
            "verify",
            timeout=runtime.profile.timeout_seconds,
            cancel=cancel,
        )
        if output:
            append_diagnostic(f"{runtime.profile.label} verify", output)
        if status:
            raise ShutdownProfileError(f"verification exited with status {status}")
        return output or "Verification command completed"

    def rollback(self, runtime: ProfileRuntime) -> str:
        status, output = self._phase(
            runtime,
            "rollback",
            timeout=runtime.profile.rollback_timeout_seconds,
        )
        if output:
            append_diagnostic(f"{runtime.profile.label} rollback", output)
        if status:
            raise ShutdownProfileError(f"rollback exited with status {status}")
        return output or "Rollback command completed"


class QemuWindowsHibernateAdapter:
    @staticmethod
    def _vm_directory(runtime: ProfileRuntime) -> Path:
        return Path(runtime.profile.adapter_config["vm_directory"])

    def probe(self, runtime: ProfileRuntime) -> tuple[bool, str]:
        vm_directory = self._vm_directory(runtime)
        identity = _live_qemu(vm_directory)
        preflight_active = runtime.state.get("preflight_active")
        if preflight_active is False:
            if identity is not None:
                raise ShutdownProfileError(
                    "Windows VM became active after the confirmed shutdown request"
                )
            return False, "Windows VM was not running when shutdown was confirmed"
        if preflight_active is True:
            expected_identity = runtime.state.get("preflight_qemu_identity")
            expected_viewer = runtime.state.get("preflight_viewer_identity")
            if not isinstance(expected_identity, ProcessIdentity):
                raise ShutdownProfileError("Windows VM pre-HUD QEMU identity is missing")
            if not isinstance(expected_viewer, ProcessIdentity):
                raise ShutdownProfileError(
                    "Windows VM pre-HUD remote-viewer identity is missing"
                )
            if identity != expected_identity or not _same_process(expected_identity):
                raise ShutdownProfileError(
                    "Windows VM changed after its pre-HUD placement capture"
                )
            if _live_qemu_viewer(vm_directory) != expected_viewer:
                raise ShutdownProfileError(
                    "Windows VM viewer changed after its pre-HUD placement capture"
                )
        if identity is None:
            return False, "Windows VM is not running"
        status = _qmp_status(vm_directory)
        if status != "running":
            raise ShutdownProfileError(
                f"Windows VM is in QEMU state {status!r}, not running"
            )
        _qga_ping(vm_directory)
        runtime.state["original_identity"] = identity
        if preflight_active is not True:
            runtime.state["restore_placement"] = _capture_qemu_restore_placement(
                vm_directory
            )
        else:
            _validate_qemu_placement(runtime.state.get("restore_placement"))
        return True, f"Windows VM PID {identity.pid} is running and QGA is ready"

    def prepare(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str:
        vm_directory = self._vm_directory(runtime)
        identity = runtime.state.get("original_identity")
        if not isinstance(identity, ProcessIdentity) or not _same_process(identity):
            raise ShutdownProfileError("Windows VM changed after the profile probe")
        guest_pid = _qga_hibernate(vm_directory)
        runtime.state["hibernate_guest_pid"] = guest_pid
        deadline = time.monotonic() + runtime.profile.timeout_seconds
        command_finished = False
        while _same_process(identity):
            cancel.requested()  # Latch cancellation; hibernation must not be interrupted.
            if not command_finished:
                try:
                    command_status = _qga_exec_status(vm_directory, guest_pid)
                except ShutdownProfileError:
                    # QGA normally disconnects while Windows enters
                    # hibernation. The authoritative verification is the
                    # original QEMU process exiting before the deadline.
                    pass
                else:
                    command_finished = bool(command_status.get("exited"))
                    if command_finished and int(command_status.get("exitcode", 0)) != 0:
                        raise ShutdownProfileError(
                            "Windows hibernate command exited with status "
                            f"{int(command_status['exitcode'])}"
                        )
            if time.monotonic() >= deadline:
                raise ShutdownProfileError(
                    "Windows accepted hibernation but QEMU did not exit before timeout"
                )
            time.sleep(0.2)
        return "Windows completed hibernation and QEMU exited"

    def verify(self, runtime: ProfileRuntime, cancel: CancellationProbe) -> str:
        _ = cancel
        identity = runtime.state.get("original_identity")
        if not isinstance(identity, ProcessIdentity) or _same_process(identity):
            raise ShutdownProfileError("the original QEMU process is still running")
        if _live_qemu(self._vm_directory(runtime)) is not None:
            raise ShutdownProfileError("an unexpected QEMU process replaced the hibernated VM")
        return "Windows hibernation is durable and the QEMU process is inactive"

    def rollback(self, runtime: ProfileRuntime) -> str:
        vm_directory = self._vm_directory(runtime)
        deadline = time.monotonic() + runtime.profile.rollback_timeout_seconds
        placement = runtime.state.get("restore_placement")
        target = (
            _resolved_qemu_placement(vm_directory, placement)
            if placement is not None
            else None
        )
        identity = _live_qemu(vm_directory)
        if identity is None:
            remaining = max(1.0, deadline - time.monotonic())
            status, output = _run_external(
                [str(vm_directory / "launch.sh")],
                label=f"{runtime.profile.label} rollback launch",
                timeout=remaining,
            )
            if output:
                append_diagnostic(f"{runtime.profile.label} rollback launch", output)
            if status:
                raise ShutdownProfileError(
                    f"VM rollback launch exited with status {status}"
                )
        last_error = "QEMU is not running"
        while time.monotonic() < deadline:
            identity = _live_qemu(vm_directory)
            if identity is not None:
                try:
                    if _qmp_status(vm_directory) == "running":
                        _qga_ping(vm_directory)
                        if target is not None:
                            _place_qemu_viewer(
                                vm_directory,
                                target,
                                timeout=max(1.0, deadline - time.monotonic()),
                            )
                        return (
                            f"Windows VM restored as PID {identity.pid}; "
                            "QEMU and QGA are ready on its saved GNOME workspace and display"
                        )
                    last_error = "QEMU did not enter running state"
                except ShutdownProfileError as error:
                    last_error = str(error)
            time.sleep(0.5)
        raise ShutdownProfileError(
            "Windows VM rollback did not become ready: " + last_error
        )


def _adapter_for(profile: ShutdownProfile) -> _Adapter:
    if profile.adapter == "command":
        return CommandAdapter()
    if profile.adapter == "qemu-windows-hibernate":
        return QemuWindowsHibernateAdapter()
    raise ShutdownProfileError(f"unsupported adapter: {profile.adapter}")


def probe_profile(profile: ShutdownProfile) -> tuple[bool, str]:
    return _adapter_for(profile).probe(ProfileRuntime(profile))


def _read_startup_restore() -> tuple[dict[str, Any], list[ProfileRuntime]] | None:
    path = startup_restore_path()
    if not path.exists():
        return None
    data = _read_private_regular_file(path, max_bytes=512 * 1024)
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ShutdownProfileError(f"invalid {path.name}: {error}") from error
    entries = document.get("entries") if isinstance(document, dict) else None
    operation_id = document.get("operation_id") if isinstance(document, dict) else None
    restored_boot_id = (
        document.get("restored_boot_id") if isinstance(document, dict) else None
    )
    committed_at = (
        document.get("committed_at") if isinstance(document, dict) else None
    )
    restored_at = (
        document.get("restored_at") if isinstance(document, dict) else None
    )
    valid_committed_at = (
        not isinstance(committed_at, bool)
        and isinstance(committed_at, (int, float))
        and 0 < committed_at < float("inf")
    )
    valid_restored_at = (
        restored_boot_id is None and restored_at is None
    ) or (
        restored_boot_id is not None
        and not isinstance(restored_at, bool)
        and isinstance(restored_at, (int, float))
        and 0 < restored_at < float("inf")
    )
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != STARTUP_RESTORE_SCHEMA_VERSION
        or not isinstance(operation_id, str)
        or len(operation_id) != 32
        or any(character not in "0123456789abcdef" for character in operation_id)
        or not isinstance(document.get("session_id"), str)
        or not document["session_id"]
        or document.get("action") not in SUPPORTED_ACTIONS
        or not isinstance(document.get("source_boot_id"), str)
        or not document["source_boot_id"]
        or not valid_committed_at
        or (
            restored_boot_id is not None
            and (not isinstance(restored_boot_id, str) or not restored_boot_id)
        )
        or not valid_restored_at
        or not isinstance(entries, list)
        or len(entries) > MAX_PROFILES
    ):
        raise ShutdownProfileError(
            f"{path.name} is insecure, malformed, or incomplete"
        )
    runtimes: list[ProfileRuntime] = []
    identifiers: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"profile", "placement"}:
            raise ShutdownProfileError(f"{path.name} contains a malformed restore job")
        profile = _profile_from_mapping(entry["profile"])
        if (
            profile.adapter != "qemu-windows-hibernate"
            or document["action"] not in profile.actions
            or profile.identifier in identifiers
        ):
            raise ShutdownProfileError(f"{path.name} contains an invalid restore job")
        runtimes.append(ProfileRuntime(
            profile,
            {"restore_placement": _validate_qemu_placement(entry["placement"])},
        ))
        identifiers.add(profile.identifier)
    return document, runtimes


def _wait_for_qemu_ready(
    runtime: ProfileRuntime,
    deadline: float,
) -> ProcessIdentity:
    vm_directory = Path(runtime.profile.adapter_config["vm_directory"])
    last_error = "QEMU is not running"
    while time.monotonic() < deadline:
        identity = _live_qemu(vm_directory)
        if identity is not None:
            try:
                if _qmp_status(vm_directory) == "running":
                    _qga_ping(vm_directory)
                    return identity
                last_error = "QEMU did not enter running state"
            except ShutdownProfileError as error:
                last_error = str(error)
        time.sleep(0.5)
    raise ShutdownProfileError(
        f"{runtime.profile.label} did not resume before timeout: {last_error}"
    )


def restore_startup_profiles(*, dry_run: bool = False) -> StartupProfileRestoreOutcome:
    boot_id = _boot_id()
    restore = _read_startup_restore()
    if restore is None:
        return StartupProfileRestoreOutcome(0, 0, "No committed VM restore jobs")
    document, runtimes = restore
    if document["source_boot_id"] == boot_id:
        return StartupProfileRestoreOutcome(
            0, 0, "VM restore is deferred until the next OS boot"
        )
    if document["restored_boot_id"] is not None:
        return StartupProfileRestoreOutcome(
            0, 0, "VM restore was already completed after the committed shutdown"
        )
    if not runtimes:
        document["restored_boot_id"] = boot_id
        document["restored_at"] = time.time()
        if not dry_run:
            atomic_json(startup_restore_path(), document)
        return StartupProfileRestoreOutcome(0, 0, "No Windows VM was active at shutdown")
    if dry_run:
        labels = ", ".join(runtime.profile.label for runtime in runtimes)
        return StartupProfileRestoreOutcome(
            len(runtimes), len(runtimes), f"Would restore: {labels}"
        )

    restored = 0
    for runtime in runtimes:
        profile = runtime.profile
        vm_directory = Path(profile.adapter_config["vm_directory"])
        target = _resolved_qemu_placement(
            vm_directory,
            runtime.state["restore_placement"],
        )
        deadline = time.monotonic() + profile.rollback_timeout_seconds
        identity = _live_qemu(vm_directory)
        if identity is None:
            status, output = _run_external(
                [str(vm_directory / "launch.sh")],
                label=f"{profile.label} startup launch",
                timeout=max(1.0, deadline - time.monotonic()),
            )
            if output:
                append_diagnostic(f"{profile.label} startup launch", output)
            if status:
                raise ShutdownProfileError(
                    f"{profile.label} launch exited with status {status}"
                )
        identity = _wait_for_qemu_ready(runtime, deadline)
        _place_qemu_viewer(
            vm_directory,
            target,
            timeout=max(1.0, deadline - time.monotonic()),
        )
        restored += 1
        update_stage(
            "virtual-machines",
            "running",
            f"Restored {profile.label} as QEMU PID {identity.pid}",
            current=restored,
            total=len(runtimes),
        )
    document["restored_boot_id"] = boot_id
    document["restored_at"] = time.time()
    atomic_json(startup_restore_path(), document)
    return StartupProfileRestoreOutcome(
        restored,
        len(runtimes),
        f"Restored {restored} Windows VM(s) on their saved GNOME workspace and display",
    )


class ShutdownProfileSession:
    """Run profiles and retain exact rollback instructions until GNOME commits."""

    def __init__(
        self,
        profiles: list[ShutdownProfile],
        *,
        operation_id: str,
        session_id: str,
        action: str,
        cancel: CancellationProbe,
        reporter: Reporter | None = None,
        initial_states: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.profiles = profiles
        self.operation_id = operation_id
        self.session_id = session_id
        self.action = action
        self.cancel = cancel
        self.reporter = reporter or self._default_reporter
        self.initial_states = initial_states or {}
        self.runtimes: list[ProfileRuntime] = []

    @staticmethod
    def _default_reporter(stage: str, state: str, message: str) -> None:
        update_stage(stage, state, message)

    def _report(self, profile: ShutdownProfile, state: str, message: str) -> None:
        self.reporter(profile.stage_id, state, message)

    def _persist(self) -> None:
        _write_transaction(
            self.operation_id,
            self.session_id,
            self.action,
            self.runtimes,
        )

    def run(self) -> None:
        for profile in self.profiles:
            if self.action not in profile.actions:
                self._report(profile, "skipped", f"Not enabled for {self.action}")
                continue
            if self.cancel.requested():
                raise ShutdownProfilesCancelled
            adapter = _adapter_for(profile)
            runtime = ProfileRuntime(
                profile,
                dict(self.initial_states.get(profile.identifier, {})),
            )
            self._report(profile, "running", "Probing whether this job is active")
            try:
                applicable, message = adapter.probe(runtime)
            except ShutdownProfileError as error:
                self._handle_failure(profile, None, error)
                continue
            if not applicable:
                self._report(profile, "skipped", message)
                continue
            if self.cancel.requested():
                raise ShutdownProfilesCancelled
            self._report(profile, "running", message)
            self.runtimes.append(runtime)
            self._persist()  # Write-ahead rollback record before mutation.
            try:
                self._report(profile, "running", "Preparing shutdown job")
                prepared_message = adapter.prepare(runtime, self.cancel)
                self._report(profile, "running", prepared_message)
                if self.cancel.requested():
                    raise ShutdownProfilesCancelled
                self._report(profile, "running", "Verifying prepared state")
                verified_message = adapter.verify(runtime, self.cancel)
                if self.cancel.requested():
                    raise ShutdownProfilesCancelled
                self._report(profile, "ready", verified_message)
            except ShutdownProfilesCancelled:
                raise
            except ShutdownProfileError as error:
                self._handle_failure(profile, runtime, error)

    def _handle_failure(
        self,
        profile: ShutdownProfile,
        runtime: ProfileRuntime | None,
        error: ShutdownProfileError,
    ) -> None:
        append_diagnostic(f"shutdown profile {profile.identifier}", str(error))
        if profile.critical:
            self._report(profile, "failed", str(error))
            raise error
        if runtime is not None:
            try:
                self._rollback_runtime(runtime, "Recovering non-critical failed job")
            except ShutdownProfileError as rollback_error:
                self._report(profile, "failed", str(rollback_error))
                raise rollback_error
        self._report(profile, "degraded", f"Non-critical job skipped: {error}")

    def _rollback_runtime(self, runtime: ProfileRuntime, reason: str) -> None:
        profile = runtime.profile
        self._report(profile, "running", reason)
        message = _adapter_for(profile).rollback(runtime)
        if runtime in self.runtimes:
            self.runtimes.remove(runtime)
            self._persist()
        self._report(profile, "skipped", message)

    def rollback_all(self, reason: str) -> None:
        errors: list[str] = []
        for runtime in list(reversed(self.runtimes)):
            try:
                self._rollback_runtime(runtime, reason)
            except ShutdownProfileError as error:
                message = f"{runtime.profile.label}: {error}"
                errors.append(message)
                self._report(runtime.profile, "failed", str(error))
                append_diagnostic("shutdown profile rollback", message)
        if errors:
            raise ShutdownProfileError(
                "shutdown rollback did not restore every job: " + "; ".join(errors)
            )
        _discard_startup_restore(self.operation_id)


def recover_transaction(operation_id: str) -> None:
    transaction = _read_transaction(operation_id)
    if transaction is None:
        return
    document, runtimes = transaction
    session = ShutdownProfileSession(
        [],
        operation_id=operation_id,
        session_id=str(document["session_id"]),
        action=str(document["action"]),
        cancel=_NeverCancelled(),
    )
    session.runtimes = runtimes
    session.rollback_all("Restoring original state after shutdown cancellation")


class _NeverCancelled:
    def requested(self) -> bool:
        return False


def install_qemu_windows_profile(
    vm_directory: Path,
    *,
    identifier: str = "windows-word-vm",
    label: str = "Windows VM hibernation",
    timeout_seconds: float = 180,
    force: bool = False,
) -> Path:
    vm_directory = vm_directory.expanduser().resolve(strict=True)
    raw = {
        "schema_version": SCHEMA_VERSION,
        "id": identifier,
        "label": label,
        "adapter": "qemu-windows-hibernate",
        "actions": ["poweroff", "restart"],
        "critical": True,
        "timeout_seconds": timeout_seconds,
        "rollback_timeout_seconds": timeout_seconds,
        "cancel_policy": "finish-then-rollback",
        "adapter_config": {"vm_directory": str(vm_directory)},
    }
    profile = _profile_from_mapping(raw)
    for required in (
        vm_directory / "launch.sh",
        vm_directory / "viewer_supervisor.py",
        vm_directory / "tools" / "qemu-system-x86_64-smb",
        vm_directory / "disk" / "windows10-22h2.qcow2",
    ):
        if not required.is_file() or required.is_symlink():
            raise ShutdownProfileError(
                f"required protected VM file is missing or unsafe: {required}"
            )
    config_root = _config_home() / "workspace-state"
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_root.chmod(0o700)
    directory = config_root / "shutdown-profiles.d"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    destination = directory / f"{profile.identifier}.toml"
    if (destination.exists() or destination.is_symlink()) and not force:
        raise ShutdownProfileError(
            f"profile already exists: {destination}; pass --force to replace it"
        )
    lines = [
        f"schema_version = {SCHEMA_VERSION}",
        f"id = {json.dumps(profile.identifier)}",
        f"label = {json.dumps(profile.label)}",
        f"adapter = {json.dumps(profile.adapter)}",
        'actions = ["poweroff", "restart"]',
        "critical = true",
        f"timeout_seconds = {profile.timeout_seconds:g}",
        f"rollback_timeout_seconds = {profile.rollback_timeout_seconds:g}",
        f"cancel_policy = {json.dumps(profile.cancel_policy)}",
        "",
        "[adapter_config]",
        f"vm_directory = {json.dumps(str(vm_directory))}",
        "",
    ]
    temporary = directory / f".{destination.name}.{os.getpid()}"
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            stream.write("\n".join(lines))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        destination.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
    return destination


def profile_fingerprint(profile: ShutdownProfile) -> str:
    payload = json.dumps(
        _profile_mapping(profile), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(payload).hexdigest()
