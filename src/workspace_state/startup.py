"""Shared login ownership for CLI and shell startup launchers."""
from __future__ import annotations

import fcntl
import json
import os
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


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "directory"
    if command == "worker-exit":
        result = os.environ.get("SERVICE_RESULT", "unknown")
        if result != "success":
            report_worker_failure(f"Startup worker ended without completion: {result}")
        return 0
    if command == "report-failure":
        report_worker_failure("Startup worker failed: " + " ".join(sys.argv[2:]))
        return 0
    if command in {"context", "worker-budget"}:
        from . import operations
        from .login_status import status_path
        try:
            context = operations.context_from_status(status_path(), "startup")
        except (OSError, ValueError, TimeoutError):
            return 1
        if command == "worker-budget":
            print(f"{context.remaining(7 * 60):.3f}s")
        else:
            print(json.dumps(context.to_dict(), separators=(",", ":")))
        return 0
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
