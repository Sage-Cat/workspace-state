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

from .login_status import append_diagnostic, runtime_root, update_stage
from .util import atomic_json


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
    return document, runtimes


def transaction_exists(operation_id: str) -> bool:
    try:
        return _read_transaction(operation_id) is not None
    except ShutdownProfileError:
        return True


def disarm_transaction(operation_id: str) -> bool:
    """Remove rollback state only when GNOME has entered final EndSession."""
    try:
        transaction = _read_transaction(operation_id)
        if transaction is None:
            return True
        transaction_path().unlink()
        return True
    except (OSError, ShutdownProfileError) as error:
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
        if identity is None:
            return False, "Windows VM is not running"
        status = _qmp_status(vm_directory)
        if status != "running":
            raise ShutdownProfileError(
                f"Windows VM is in QEMU state {status!r}, not running"
            )
        _qga_ping(vm_directory)
        runtime.state["original_identity"] = identity
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
                        return (
                            f"Windows VM restored as PID {identity.pid}; "
                            "QEMU and QGA are ready"
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
    ) -> None:
        self.profiles = profiles
        self.operation_id = operation_id
        self.session_id = session_id
        self.action = action
        self.cancel = cancel
        self.reporter = reporter or self._default_reporter
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
            runtime = ProfileRuntime(profile)
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
