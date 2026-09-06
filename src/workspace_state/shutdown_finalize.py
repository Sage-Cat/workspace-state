"""Managed, cancellable workspace checkpoint after GNOME confirmation."""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

from .login_status import (
    append_diagnostic,
    cancel_shutdown,
    consume_shutdown_cancel,
    fail_active,
    finish,
    register_shutdown_stages,
    set_overall,
    shutdown_worker_complete_path,
    status_path,
    update_stage,
)
from .shutdown_profiles import (
    ShutdownProfileError,
    ShutdownProfileSession,
    ShutdownProfilesCancelled,
    load_profiles,
    recover_transaction,
)


CHECKPOINT_TIMEOUT_SECONDS = 120.0
OPERATION_ENV = "WSCTL_SHUTDOWN_OPERATION_ID"


class ShutdownCancelled(Exception):
    """The HUD or systemd asked the managed checkpoint to stop."""


class Cancellation:
    def __init__(self, operation_id: str) -> None:
        self.operation_id = operation_id
        self.signalled = False
        self.cancelled = False

    def signal(self, _signum: int, _frame: object) -> None:
        self.signalled = True

    def requested(self) -> bool:
        if not self.cancelled:
            self.cancelled = self.signalled or consume_shutdown_cancel(
                self.operation_id
            )
        return self.cancelled

    def check(self) -> None:
        if self.requested():
            raise ShutdownCancelled


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


def _run_checkpoint(
    command: list[str],
    label: str,
    stage: str,
    cancel: Cancellation,
) -> None:
    update_stage(stage, "running", f"Running {label}")
    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as error:
        raise RuntimeError(f"could not start {label}: {error}") from error
    deadline = time.monotonic() + CHECKPOINT_TIMEOUT_SECONDS
    while process.poll() is None:
        if cancel.requested():
            _terminate_process_group(process)
            raise ShutdownCancelled
        if time.monotonic() >= deadline:
            _terminate_process_group(process)
            raise RuntimeError(
                f"{label} exceeded {CHECKPOINT_TIMEOUT_SECONDS:g} seconds"
            )
        time.sleep(0.1)
    if process.returncode:
        raise RuntimeError(f"{label} failed with exit status {process.returncode}")
    update_stage(stage, "ready", f"Completed {label}", current=1, total=1)


def _runtime_root() -> Path:
    return Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    ) / "workspace-state"


def _login_generation() -> str | None:
    try:
        value = (_runtime_root() / "login-generation").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None
    if len(value) != 16 or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def clear_worker_complete_marker() -> None:
    try:
        shutdown_worker_complete_path().unlink(missing_ok=True)
    except OSError as error:
        append_diagnostic("could not clear shutdown worker completion", str(error))


def write_worker_complete_marker(
    operation_id: str,
    *,
    action: str = "poweroff",
    origin: str = "preflight",
) -> bool:
    """Publish success for coordinator promotion after this unit has exited."""
    login_generation = _login_generation()
    invocation_id = os.environ.get("INVOCATION_ID", "")
    if (
        login_generation is None
        or origin != "preflight"
        or action not in {"poweroff", "restart"}
        or len(invocation_id) != 32
        or any(character not in "0123456789abcdef" for character in invocation_id)
    ):
        return False
    root = _runtime_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        target = shutdown_worker_complete_path()
        temporary = root / f".{target.name}.{os.getpid()}"
        payload = {
            "schema_version": 1,
            "operation_id": operation_id,
            "login_generation": login_generation,
            "action": action,
            "origin": origin,
            "invocation_id": invocation_id,
            "created_at": time.time(),
        }
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        return True
    except OSError as error:
        append_diagnostic("could not publish shutdown preparation", str(error))
        return False


def _shutdown_context(operation_id: str) -> tuple[str, str, str]:
    """Read the action and session bound to this confirmed HUD operation."""
    try:
        descriptor = os.open(status_path(), os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            metadata = os.fstat(stream.fileno())
            status = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"could not read shutdown transaction status: {error}") from error
    login_generation = _login_generation()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or not isinstance(status, dict)
        or status.get("schema_version") != 1
        or status.get("mode") != "shutdown"
        or status.get("operation_id") != operation_id
        or status.get("shutdown_action") not in {"poweroff", "restart"}
        or status.get("shutdown_origin") != "preflight"
        or login_generation is None
        or status.get("session_id") != login_generation
    ):
        raise RuntimeError(
            "shutdown transaction status is insecure, stale, malformed, "
            "or was not created after GNOME confirmation"
        )
    return str(status["shutdown_action"]), "preflight", login_generation


def run_transaction(operation_id: str) -> int:
    """Save workspace state and prepare configured recoverable shutdown jobs."""
    cancel = Cancellation(operation_id)
    profile_session: ShutdownProfileSession | None = None
    previous_term = signal.signal(signal.SIGTERM, cancel.signal)
    previous_int = signal.signal(signal.SIGINT, cancel.signal)
    try:
        action, origin, session_id = _shutdown_context(operation_id)
        try:
            profiles = load_profiles()
        except ShutdownProfileError as error:
            update_stage(
                "shutdown-profiles", "failed", str(error), error=str(error)
            )
            raise RuntimeError(str(error)) from error
        if not register_shutdown_stages(
            [(profile.stage_id, profile.label) for profile in profiles]
        ):
            raise RuntimeError("could not publish configured shutdown profile stages")
        profile_session = ShutdownProfileSession(
            profiles,
            operation_id=operation_id,
            session_id=session_id,
            action=action,
            cancel=cancel,
        )
        bin_dir = Path(
            os.environ.get("WSCTL_BIN_DIR", Path.home() / ".local/bin")
        )
        _run_checkpoint(
            [str(bin_dir / "wsctl-continuum-save"), "quiet"],
            "tmux-resurrect save",
            "tmux-save",
            cancel,
        )
        _run_checkpoint(
            [str(bin_dir / "wsctl"), "save"],
            "workspace save",
            "workspace-save",
            cancel,
        )
        try:
            profile_session.run()
        except ShutdownProfilesCancelled as error:
            raise ShutdownCancelled from error
        except ShutdownProfileError as error:
            raise RuntimeError(str(error)) from error
        cancel.check()
        update_stage(
            "checkpoint-proof",
            "running",
            "Checkpoint saved; verifying the managed worker exit",
        )
        set_overall(
            "running",
            "Checkpoint saved; verifying integrity before the HUD countdown",
        )
        cancel.check()
        # This is not the authoritative prepared marker. The coordinator
        # promotes it only after systemd proves this exact invocation exited
        # successfully. Cloud drives and GNOME are left to Ubuntu shutdown.
        if not write_worker_complete_marker(
            operation_id,
            action=action,
            origin=origin,
        ):
            raise RuntimeError("could not persist shutdown worker completion")
        return 0
    except ShutdownCancelled:
        clear_worker_complete_marker()
        if profile_session is not None:
            try:
                profile_session.rollback_all(
                    "Cancellation requested; restoring original state"
                )
            except ShutdownProfileError as error:
                fail_active(str(error))
                append_diagnostic("shutdown cancellation rollback", str(error))
                return 1
        cancel_shutdown("Shutdown cancelled from the HUD")
        finish("Shutdown cancelled; prepared jobs were restored")
        return 0
    except RuntimeError as error:
        clear_worker_complete_marker()
        rollback_error: ShutdownProfileError | None = None
        if profile_session is not None:
            try:
                profile_session.rollback_all(
                    "Shutdown preparation failed; restoring original state"
                )
            except ShutdownProfileError as recovery_error:
                rollback_error = recovery_error
        message = str(error)
        if rollback_error is not None:
            message += f"; {rollback_error}"
        fail_active(message)
        append_diagnostic("shutdown checkpoint", message)
        return 1
    finally:
        signal.signal(signal.SIGTERM, previous_term)
        signal.signal(signal.SIGINT, previous_int)


def main() -> int:
    rollback = sys.argv[1:] == ["--rollback"]
    if len(sys.argv) != 1 and not rollback:
        append_diagnostic("shutdown service invocation", "invalid arguments")
        return 2
    operation_id = os.environ.get(OPERATION_ENV, "")
    if len(operation_id) != 32 or any(
        char not in "0123456789abcdef" for char in operation_id
    ):
        append_diagnostic(
            "shutdown service invocation",
            "missing or invalid operation id",
        )
        return 2
    if rollback:
        try:
            recover_transaction(operation_id)
            return 0
        except ShutdownProfileError as error:
            fail_active(str(error))
            append_diagnostic("shutdown profile recovery", str(error))
            return 1
    return run_transaction(operation_id)


if __name__ == "__main__":
    raise SystemExit(main())
