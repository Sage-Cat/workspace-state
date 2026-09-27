"""Managed, cancellable workspace checkpoint after GNOME confirmation."""

from __future__ import annotations
from . import operations

import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from functools import partial
from pathlib import Path

from .concurrency import completed_jobs

from .login_status import (
    append_diagnostic,
    cancel_shutdown,
    consume_shutdown_cancel,
    fail_active,
    finish_shutdown,
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
    load_shutdown_profile_preflight,
    recover_transaction,
    transaction_exists,
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
        self._lock = threading.Lock()
        self._abort = False

    def signal(self, _signum: int, _frame: object) -> None:
        self.signalled = True

    def requested(self) -> bool:
        # consume_shutdown_cancel removes the marker. Serialize consumption and
        # latching so a second thread cannot overwrite the first thread's True.
        with self._lock:
            if not self.cancelled:
                self.cancelled = self.signalled or self._abort or consume_shutdown_cancel(
                    self.operation_id
                )
            return self.cancelled

    def abort_peers(self) -> None:
        with self._lock:
            self._abort = True

    def check(self) -> None:
        context = operations.current()
        if context is not None:
            context.check()
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
    *,
    degraded_returncodes: frozenset[int] = frozenset(),
) -> bool:
    cancel.check()
    update_stage(stage, "running", f"Running {label}")
    try:
        process = subprocess.Popen(command, start_new_session=True,
                                   env=operations.child_environment())
    except OSError as error:
        raise RuntimeError(f"could not start {label}: {error}") from error
    context = operations.current()
    deadline = time.monotonic() + (context.remaining(CHECKPOINT_TIMEOUT_SECONDS)
                                  if context else CHECKPOINT_TIMEOUT_SECONDS)
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
    if process.returncode in degraded_returncodes:
        update_stage(
            stage,
            "degraded",
            f"Completed {label} using safe fallback state",
            current=1,
            total=1,
            error="One or more live items could not be captured exactly; safe fallback state was retained",
        )
        return True
    if process.returncode:
        raise RuntimeError(f"{label} failed with exit status {process.returncode}")
    update_stage(stage, "ready", f"Completed {label}", current=1, total=1)
    return False


def _save_checkpoints(bin_dir: Path, operation_id: str, cancel: Cancellation) -> bool:
    """Join both read-only saves before profiles, rollback or authorization.

    Their canonical-recipe commits still use the existing cross-process state
    lock: a tmux hook merges the latest browser/app categories, never stale ones.
    """
    jobs = {
        "tmux-save": partial(_run_checkpoint, [
            str(bin_dir / "wsctl-continuum-save"), "--shutdown-operation", operation_id, "quiet",
        ], "tmux-resurrect save", "tmux-save", cancel),
        "workspace-save": partial(_run_checkpoint, [
            str(bin_dir / "wsctl"), "save", "--allow-partial", "--shutdown-safe",
        ], "workspace save", "workspace-save", cancel, degraded_returncodes=frozenset({3})),
    }
    errors = []
    degraded = False
    for stage, result, error in completed_jobs(jobs, workers=2):
        if error is not None:
            errors.append(error)
            cancel.abort_peers()
            if not isinstance(error, ShutdownCancelled):
                update_stage(stage, "failed", str(error), error=str(error))
        else:
            degraded = degraded or bool(result)
    # Preserve the actual failure, not its secondary peer-cancellation error.
    failure = next((error for error in errors if not isinstance(error, ShutdownCancelled)), None)
    if failure is not None:
        raise RuntimeError(str(failure)) from failure
    if errors:
        raise ShutdownCancelled
    cancel.check()
    return degraded


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
        context = operations.current()
        if context is not None:
            context.check()
            payload["operation_context"] = context.to_dict()
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
    try:
        operations.context_from_status(status_path(), "shutdown", operation_id)
    except (OSError, ValueError) as error:
        # A legacy source-linked coordinator can still invoke this new worker
        # before the first coordinated login. Refuse before any checkpoint or
        # profile mutation; never synthesize authority from legacy HUD receipts.
        if "operation_context" not in status:
            raise RuntimeError(
                "Shutdown preparation requires the coordinated desktop operation protocol; "
                "log out and back in to activate the scheduled desktop release. "
                "No preparation was started."
            ) from error
        raise RuntimeError(f"Shutdown operation authority is no longer valid: {error}") from error
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
        try:
            initial_states = load_shutdown_profile_preflight(
                profiles,
                operation_id=operation_id,
                session_id=session_id,
                action=action,
            )
        except ShutdownProfileError as error:
            update_stage(
                "shutdown-profiles", "failed", str(error), error=str(error)
            )
            raise RuntimeError(str(error)) from error
        profile_session = ShutdownProfileSession(
            profiles,
            operation_id=operation_id,
            session_id=session_id,
            action=action,
            cancel=cancel,
            initial_states=initial_states,
        )
        bin_dir = Path(
            os.environ.get("WSCTL_BIN_DIR", Path.home() / ".local/bin")
        )
        degraded = _save_checkpoints(bin_dir, operation_id, cancel)
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
            "degraded" if degraded else "running",
            (
                "Checkpoint saved with safe fallbacks; verifying integrity before the HUD countdown"
                if degraded
                else "Checkpoint saved; verifying integrity before the HUD countdown"
            ),
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
        finish_shutdown("Shutdown cancelled; prepared jobs were restored")
        return 0
    except (RuntimeError, OSError, ValueError) as error:
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
        print(f"wsctl: {message}", file=sys.stderr, flush=True)
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
        # Refused preparation has no journal. A no-op cleanup needs no adoption
        # of the current (possibly newer) status document and grants no commit.
        if not transaction_exists(operation_id):
            return 0
        try:
            operations.context_from_status(status_path(), "shutdown", operation_id, allow_expired=True)
            recover_transaction(operation_id)
            return 0
        except (ShutdownProfileError, OSError, ValueError) as error:
            fail_active(str(error))
            append_diagnostic("shutdown profile recovery", str(error))
            return 1
    return run_transaction(operation_id)


if __name__ == "__main__":
    raise SystemExit(main())
