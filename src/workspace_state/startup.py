"""Shared login ownership for the CLI, the session agent and shell wrappers."""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
from dataclasses import dataclass, field
from typing import Any
from pathlib import Path
import sys

from .provider_results import blocking_attention
from .util import atomic_json, checked_component, checked_path, environment_path



@dataclass(frozen=True)
class StageMarker:
    """A restore attempt is not evidence that every phase completed."""
    category: str
    state: str = "attempted"
    snapshot: str = ""
    message: str = ""
    operation_context: dict[str, Any] | None = None
    provider_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    legacy: bool = False

    @property
    def verified(self) -> bool:
        if self.legacy or self.state not in {"ready", "skipped"}:
            return False
        return all(not blocking_attention(item.get("attention") or ()) and all(
            isinstance(item.get(phase), dict)
            and item[phase].get("state") in {"verified", "skipped"}
            for phase in ("identity", "content", "placement"))
            for item in self.provider_results)

    def verified_for(self, context) -> bool:
        return bool(context is not None and self.verified
                    and self.operation_context == context.to_dict())

    def verified_for_login(self, context) -> bool:
        if context is None or not self.verified:
            return False
        from .operations import OperationContext
        try:
            owner = OperationContext.from_dict(self.operation_context)
        except (TypeError, ValueError):
            return False
        return (owner.mode == "startup" and owner.boot_id == context.boot_id
                and owner.login_generation == context.login_generation)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": 1, "category": self.category, "state": self.state,
                "snapshot": self.snapshot, "message": self.message,
                "operation_context": self.operation_context,
                "provider_results": list(self.provider_results)}


def read_stage_marker(path: Path, category: str = "") -> StageMarker | None:
    """Read historical plain-text attempts without inventing verification."""
    try:
        raw = path.read_text().strip()
    except FileNotFoundError:
        return None
    except OSError:
        return StageMarker(category, message="Restore marker is unreadable")
    if not raw.startswith(("{", "[")):
        failed = raw.startswith("failed:")
        return StageMarker(category, "failed" if failed else "attempted",
                           snapshot="" if failed else raw, message=raw, legacy=True)
    try:
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("schema_version")) is not int
                or value["schema_version"] != 1
                or value.get("state") not in {"ready", "skipped", "waiting", "failed", "attempted"}
                or (category and value.get("category") != category)
                or not isinstance(value.get("provider_results", []), list)
                or any(not isinstance(item, dict) for item in value.get("provider_results", []))):
            raise ValueError("invalid marker")
        context = value.get("operation_context")
        if context is not None:
            from .operations import OperationContext
            OperationContext.from_dict(context)
        return StageMarker(str(value.get("category", category)), value["state"],
                           str(value.get("snapshot", "")), str(value.get("message", "")),
                           context, tuple(value.get("provider_results", [])))
    except (ValueError, TypeError):
        return StageMarker(category, message="Restore marker is invalid; prior attempt is preserved")


def write_stage_marker(path: Path, marker: StageMarker) -> None:
    if marker.state not in {"ready", "skipped", "waiting", "failed", "attempted"}:
        raise ValueError("invalid restore attempt state")
    atomic_json(path, marker.to_dict())

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
    """The startup directory of one login, adopting a legacy one for its owner."""
    root = checked_path(root, "runtime root")
    boot = checked_component(boot, "boot id")
    generation = None if login_key is None else checked_component(login_key, "login key")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    with (root / "startup-generations.lock").open("a+") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        legacy = root / f"startup-{boot}"
        generated = root / f"startup-{boot}-{generation}" if generation else legacy
        owner_path = legacy / "generation-owner.json"
        try:
            owner = json.loads(owner_path.read_text())
        except FileNotFoundError:
            owner = None
        except (OSError, ValueError):
            owner = {}  # Malformed ownership is never permission to adopt it.
        if generation is None and owner is not None:
            raise RuntimeError("login key is not available; refusing to reuse a prior login's startup markers")
        if legacy.is_dir() and not generated.exists() and generation:
            if owner is None:
                owner = {"schema_version": 1, "boot_id": boot, "login_key": generation}
                atomic_json(owner_path, owner)
            if owner == {"schema_version": 1, "boot_id": boot, "login_key": generation}:
                generated = legacy
        generated.mkdir(parents=True, exist_ok=True, mode=0o700)
        generated.chmod(0o700)
        return generated


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
