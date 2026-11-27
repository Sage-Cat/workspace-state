"""Shared login ownership for the CLI, the session agent and shell wrappers."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
import sys

from .util import checked_component, checked_path, environment_path



def read_hex_file(path: Path) -> str | None:
    try:
        value = path.read_text().strip()
    except OSError:
        return None
    if not value or any(char not in "0123456789abcdef" for char in value):
        return None
    return value


def login_identity(root: Path) -> tuple[str | None, str | None]:
    """Read the session id (`login-generation`) and the login key (`login-key`).

    The session id is the bus-name hash that the HUD also computes; it can
    repeat across logins. The login key identifies the session manager process
    and separates logins. A runtime that predates the key file has the session
    id as its key.
    """
    generation = read_hex_file(root / "login-generation")
    return generation, login_key_file(root) or generation


def login_key_file(root: Path) -> str | None:
    """The published login key, unless a newer `login-generation` outdates it.

    The coordinator writes the session id first and the key after it. A key
    older than the session id was left by an earlier login (for example by a
    newer release that was rolled back), and says nothing about this one.
    """
    try:
        if ((root / "login-key").stat().st_mtime_ns
                < (root / "login-generation").stat().st_mtime_ns):
            return None
    except OSError:
        return None
    return read_hex_file(root / "login-key")


def startup_directory(root: Path, boot: str, login_key: str | None) -> Path:
    """The private startup directory of one login (of the boot without a key)."""
    root = checked_path(root, "runtime root")
    boot = checked_component(boot, "boot id")
    key = None if login_key is None else checked_component(login_key, "login key")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    directory = root / (f"startup-{boot}-{key}" if key else f"startup-{boot}")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def runtime_identity() -> tuple[Path, str, str | None, str | None]:
    """Return the runtime root, boot id, session id and login key."""
    root = environment_path("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}") / "workspace-state"
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return (root, boot, *login_identity(root))


def report_worker_failure(message: str) -> bool:
    from . import operations
    from .login_status import fail_active, status_path, update_stage
    try:
        # Failure reporting needs the inherited authority, never adoption of a
        # potentially newer operation that replaced the worker's startup.
        if operations.current() is None:
            return False
        operations.context_from_status(status_path(), "startup", allow_expired=True)
        update_stage("workspace", "failed", message, error=message)
        return fail_active(message)
    except (OSError, ValueError, TimeoutError):
        return False


AGENT_UNIT = "wsctl-gnome-session.service"
UNIT_CHECK_TIMEOUT = 2.0


def restore_unit_name(login_key: str | None, boot: str) -> str:
    """The fixed name of the transient service that restores one login."""
    return f"wsctl-startup-restore-{login_key or boot}.service"


LIVE_UNIT_STATES = {"active", "activating", "deactivating", "reloading", "refreshing"}
DEAD_UNIT_STATES = {"inactive", "failed"}


def _unit_may_run(unit: str) -> bool:
    """False only when systemd clearly says the unit is dead.

    A timeout, an error or an unknown answer is unclear, and an unclear unit
    may still be running.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=UNIT_CHECK_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return True
    state = result.stdout.strip()
    if result.returncode == 0 and state in DEAD_UNIT_STATES:
        return False
    return True


def unit_is_live(unit: str) -> bool:
    """True only when systemd clearly says the unit is live.

    The opposite of `_unit_may_run`: a timeout, an error or an unknown answer
    is not proof, so it never blocks a launch.
    """
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveState", "--value", unit],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=UNIT_CHECK_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0 and result.stdout.strip() in LIVE_UNIT_STATES


def _restore_can_run(login_key: str | None, boot: str | None,
                     recorded_unit: str | None = None) -> bool:
    """Whether the restore of this login can still make progress.

    Once the agent recorded its restore service, only that service decides: the
    agent lives all session and would mask a service that died early. Before
    the record exists the agent itself is the one that will start the restore.
    """
    if recorded_unit:
        return _unit_may_run(recorded_unit)
    if _unit_may_run(AGENT_UNIT):
        return True
    return boot is not None and _unit_may_run(restore_unit_name(login_key, boot))


def terminals_pending() -> bool:
    """Whether this login's terminal restore can still run and has not ended.

    A terminal opened while it is pending must not start the user's shell,
    which would create `main` before the restore does. Once nothing can finish
    the restore (another login, a finished or expired operation, no agent or
    restore service), `main` belongs to the user. An unreadable status is
    unclear: it counts as pending only while something could still restore.
    """
    from . import operations
    try:
        root, boot, session_id, login_key = runtime_identity()
    except (OSError, RuntimeError, ValueError):
        return _restore_can_run(None, None)
    try:
        document = json.loads((root / "login-hud-status.json").read_text())
        if not isinstance(document, dict):
            raise ValueError("status is not an object")
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return _restore_can_run(login_key, boot)
    if document.get("session_id") is not None and (
            document.get("session_id") != session_id
            or document.get("login_key", session_id) != login_key):
        return False  # Another login's status says nothing about this one.
    try:
        context = operations.OperationContext.from_dict(document.get("operation_context"))
        if (not context.matches(document) or session_id is None
                or not isinstance(document.get("stages"), list)):
            raise ValueError("status is inconsistent")
    except (ValueError, TypeError):
        return _restore_can_run(login_key, boot)
    if (document.get("mode") != "startup" or document.get("operation_state") != "running"
            or context.boot_id != boot or context.remaining() <= 0):
        return False
    for stage in document["stages"]:
        if (isinstance(stage, dict) and stage.get("id") == "terminals"
                and stage.get("state") in {"ready", "degraded", "failed", "skipped", "waiting"}):
            # `waiting` is set only after tmux is restored: a window then waits
            # for its placement check, and the user's shell may start.
            return False
    recorded = None
    if document.get("restore_login_key") == login_key and isinstance(document.get("restore_unit"), str):
        recorded = document["restore_unit"]
    return _restore_can_run(login_key, boot, recorded)


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "directory"
    if command == "worker-exit":
        result = os.environ.get("SERVICE_RESULT", "unknown")
        if result != "success":
            report_worker_failure(f"Startup worker ended without completion: {result}")
        return 0
    if command == "report-failure":
        # Still called by a restore worker that an older release started.
        report_worker_failure("Startup worker failed: " + " ".join(sys.argv[2:]))
        return 0
    if command == "terminals-settled":
        # Only an explicit success lets the user's shell start; a crash exits
        # non-zero and so counts as pending.
        return 1 if terminals_pending() else 0
    if command == "saved-sessions":
        # Read-only: temporary terminal sessions must not take a saved name.
        from . import storage
        try:
            names = storage.saved_session_names(storage.load())
        except FileNotFoundError:
            return 0  # Nothing saved, nothing to avoid.
        except (OSError, ValueError):
            return 1
        for name in names:
            print(name)
        return 0
    root, boot, _session_id, login_key = runtime_identity()
    print(startup_directory(root, boot, login_key))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
