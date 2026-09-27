"""Best-effort, per-login status publication for the GNOME login HUD."""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .util import atomic_json
from . import operations


SCHEMA_VERSION = 1
CANCEL_FILENAME = "shutdown-cancel.json"
SHUTDOWN_REQUEST_FILENAME = "shutdown-request.json"
SHUTDOWN_RENDERED_FILENAME = "shutdown-hud-rendered.json"
SHUTDOWN_COMMIT_FILENAME = "shutdown-commit.json"
SHUTDOWN_WORKER_COMPLETE_FILENAME = "shutdown-worker-complete.json"
BOOT_CLAIM_FILENAME = "startup-hud-boot.json"
STARTUP_POLICY_FILENAME = "startup-hud-policy.json"
MAX_STAGE_EVENTS = 32
TERMINAL_STATES = {"ready", "degraded", "failed", "skipped"}
VALID_STATES = TERMINAL_STATES | {"pending", "waiting", "running"}
DEFAULT_STAGES = (
    ("gnome", "GNOME Wayland session"),
    ("displays", "Display and workspace readiness"),
    ("tmux", "tmux-resurrect"),
    ("terminals", "Alacritty and tmux sessions"),
    ("codex", "Codex conversations"),
    ("browsers", "Chrome workspaces"),
    ("social-apps", "Social apps — Slack, Discord, Telegram, Viber"),
    ("file-manager", "Default file manager"),
    ("vscode", "VS Code workspaces"),
    ("virtual-machines", "Windows VM restoration"),
    ("workspace", "Workspace restoration"),
    ("gdrive", "Google Drive"),
    ("nextcloud", "Nextcloud drive"),
    ("pdrive", "Proton Drive"),
    ("warmup", "Cloud metadata warm-up"),
)
SHUTDOWN_STAGES = (
    ("tmux-save", "tmux-resurrect checkpoint"),
    ("workspace-save", "Desktop and browser checkpoint"),
    ("social-apps-save", "Social app visibility and placement"),
    ("file-manager-save", "Default file manager"),
    ("vscode-save", "VS Code workspaces"),
    ("checkpoint-proof", "Checkpoint integrity"),
)
STAGE_GROUPS = {
    "startup": {
        **{
            identifier: ("desktop-readiness", "GNOME Wayland and workspace readiness")
            for identifier in ("gnome", "displays")
        },
        **{
            identifier: ("terminal-restore", "Alacritty and tmux restoration")
            for identifier in ("tmux", "terminals")
        },
        **{
            identifier: ("cloud-drives", "Cloud drives and metadata")
            for identifier in ("gdrive", "nextcloud", "pdrive", "warmup")
        },
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def runtime_root() -> Path:
    return Path(
        os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    ) / "workspace-state"


def state_root() -> Path:
    return Path(
        os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")
    ) / "workspace-state"


def status_path() -> Path:
    return runtime_root() / "login-hud-status.json"


def operation_path() -> Path:
    return runtime_root() / "current-operation.json"


def _record_operation(document: dict[str, Any]) -> None:
    atomic_json(operation_path(), {"schema_version": 1,
                                  "operation_context": document["operation_context"]})


def _recover_startup_status() -> dict[str, Any]:
    """Presentation damage cannot change the lifecycle owner's identity/mode."""
    try:
        record = json.loads(operation_path().read_text())
        owner = operations.OperationContext.from_dict(record.get("operation_context"))
    except FileNotFoundError:
        if operations.current() is not None:
            raise ValueError("operation ownership record is missing")
        document = _initial_status()
        _record_operation(document)
        return document
    if owner.mode != "startup" or owner.login_generation != _session_id():
        raise ValueError("cannot reconstruct shutdown or another login from telemetry")
    document = _initial_status(owner.login_generation)
    document.update(operation_context=owner.to_dict(), operation_id=owner.operation_id)
    return document


def log_path() -> Path:
    return runtime_root() / "login-hud.log"


def cancel_path() -> Path:
    return runtime_root() / CANCEL_FILENAME


def shutdown_request_path() -> Path:
    return runtime_root() / SHUTDOWN_REQUEST_FILENAME


def shutdown_rendered_path() -> Path:
    return runtime_root() / SHUTDOWN_RENDERED_FILENAME


def shutdown_commit_path() -> Path:
    return runtime_root() / SHUTDOWN_COMMIT_FILENAME


def shutdown_worker_complete_path() -> Path:
    return runtime_root() / SHUTDOWN_WORKER_COMPLETE_FILENAME


def startup_policy_path() -> Path:
    return runtime_root() / STARTUP_POLICY_FILENAME


def boot_claim_path() -> Path:
    return state_root() / BOOT_CLAIM_FILENAME


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def claim_startup_hud(session_id: str) -> bool:
    """Claim startup HUD visibility once per OS boot.

    The session ID is retained with the boot claim so a coordinator restart in
    the first GNOME session keeps publishing the same visible startup HUD. A
    later GNOME login during the same boot receives telemetry, but its startup
    document is explicitly hidden. Shutdown documents are unaffected.
    """
    boot_id = _boot_id()
    if not boot_id:
        # A missing kernel boot ID should not make the first real boot HUD
        # disappear. Session validation in the Shell extension still rejects
        # stale status from a different GNOME login.
        return True
    try:
        root = state_root()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        lock_path = root / f"{BOOT_CLAIM_FILENAME}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                claim = json.loads(boot_claim_path().read_text())
            except (OSError, json.JSONDecodeError, TypeError):
                claim = None
            same_boot = isinstance(claim, dict) and claim.get("boot_id") == boot_id
            same_session = same_boot and claim.get("session_id") == session_id
            visible = not same_boot or (
                same_session and _startup_claim_is_still_active(session_id)
            )
            if not same_boot:
                atomic_json(boot_claim_path(), {
                    "schema_version": SCHEMA_VERSION,
                    "boot_id": boot_id,
                    "session_id": session_id,
                    "claimed_at": _now(),
                })
            return visible
    except OSError:
        # HUD accounting must never make GNOME restoration fail. If the
        # persistent claim cannot be secured, keep startup presentation hidden
        # while shutdown safety remains fully available.
        return False


def _startup_claim_is_still_active(session_id: str) -> bool:
    """Retain a first-login HUD only across a genuinely mid-startup restart."""
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, json.JSONDecodeError, TypeError):
        # The first coordinator may have claimed the boot immediately before
        # it could create status. Preserve that narrow crash-recovery case.
        return True
    return bool(
        isinstance(status, dict)
        and status.get("schema_version") == SCHEMA_VERSION
        and status.get("mode") == "startup"
        and status.get("session_id") == session_id
        and status.get("show_startup_hud") is True
        and status.get("overall_state") not in TERMINAL_STATES
    )


def _session_id() -> str:
    try:
        value = (runtime_root() / "login-generation").read_text().strip()
    except OSError:
        value = ""
    return value or f"pid-{os.getpid()}"


def _stage_document(identifier: str, label: str, mode: str) -> dict[str, Any]:
    stage: dict[str, Any] = {
        "id": identifier,
        "label": label,
        "state": "pending",
        "message": "Pending",
        "events": [],
    }
    group = STAGE_GROUPS.get(mode, {}).get(identifier)
    if group:
        stage["group_id"], stage["group_label"] = group
    return stage


def _ensure_stage_metadata(status: dict[str, Any]) -> bool:
    changed = False
    mode = str(status.get("mode") or "startup")
    groups = STAGE_GROUPS.get(mode, {})
    stages = status.get("stages", [])
    if not isinstance(stages, list):
        stages = []
        status["stages"] = stages
        changed = True
    if mode == "startup":
        by_identifier = {
            str(stage.get("id")): stage
            for stage in stages
            if isinstance(stage, dict) and isinstance(stage.get("id"), str)
        }
        ordered = []
        for identifier, label in DEFAULT_STAGES:
            stage = by_identifier.pop(identifier, None)
            if stage is None:
                stage = _stage_document(identifier, label, mode)
                changed = True
            ordered.append(stage)
        default_identifiers = {identifier for identifier, _label in DEFAULT_STAGES}
        ordered.extend(
            stage for stage in stages
            if isinstance(stage, dict)
            and str(stage.get("id")) not in default_identifiers
        )
        if ordered != stages:
            status["stages"] = stages = ordered
            changed = True
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        if not isinstance(stage.get("events"), list):
            stage["events"] = []
            changed = True
        group = groups.get(str(stage.get("id", "")))
        if group and (
            stage.get("group_id") != group[0]
            or stage.get("group_label") != group[1]
        ):
            stage["group_id"], stage["group_label"] = group
            changed = True
    return changed


def _record_stage_event(stage: dict[str, Any], state: str, message: str) -> None:
    events = stage.setdefault("events", [])
    if not isinstance(events, list):
        events = []
        stage["events"] = events
    event = {"at": _now(), "state": state, "message": str(message)}
    if events and isinstance(events[-1], dict) and all(
        events[-1].get(key) == event[key] for key in ("state", "message")
    ):
        return
    events.append(event)
    del events[:-MAX_STAGE_EVENTS]


def _startup_visibility(session_id: str) -> bool:
    try:
        policy = json.loads(startup_policy_path().read_text())
    except (OSError, json.JSONDecodeError, TypeError):
        return True
    return not (
        isinstance(policy, dict)
        and policy.get("session_id") == session_id
        and policy.get("show_startup_hud") is False
    )


def _write_startup_policy(session_id: str, show_startup_hud: bool) -> None:
    atomic_json(startup_policy_path(), {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "show_startup_hud": bool(show_startup_hud),
    })


def _initial_status(
    session_id: str | None = None,
    show_startup_hud: bool | None = None,
) -> dict[str, Any]:
    now = _now()
    effective_session_id = session_id or _session_id()
    if show_startup_hud is None:
        show_startup_hud = _startup_visibility(effective_session_id)
    context = operations.OperationContext.create(effective_session_id, "startup")
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "startup",
        "session_id": effective_session_id,
        "operation_id": context.operation_id,
        "operation_context": context.to_dict(),
        "operation_state": "running",
        "commit_authorized": False,
        "recovery_pending": False,
        "show_startup_hud": bool(show_startup_hud),
        "started_at": now,
        "updated_at": now,
        "overall_state": "running",
        "overall_message": "GNOME session initialization started",
        "stages": [
            _stage_document(identifier, label, "startup")
            for identifier, label in DEFAULT_STAGES
        ],
        "error_log_path": str(log_path()),
    }


def _initial_shutdown_status(
    session_id: str,
    operation_id: str,
    *,
    action: str = "poweroff",
    origin: str = "preflight",
) -> dict[str, Any]:
    now = _now()
    context = operations.OperationContext.create(session_id, "shutdown", operation_id=operation_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "shutdown",
        "session_id": session_id,
        "operation_id": operation_id,
        "operation_context": context.to_dict(),
        "operation_state": "preparing",
        "commit_authorized": False,
        "recovery_pending": False,
        "shutdown_action": action,
        "shutdown_origin": origin,
        "cancelled": False,
        "started_at": now,
        "updated_at": now,
        "overall_state": "running",
        "overall_message": "Saving the workspace before shutdown",
        "stages": [
            _stage_document(identifier, label, "shutdown")
            for identifier, label in SHUTDOWN_STAGES
        ],
        "error_log_path": str(log_path()),
    }


def _append_log_unlocked(message: str) -> None:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    with path.open("a", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(f"{_now()}  {message.rstrip()}\n")


def _recompute(status: dict[str, Any]) -> None:
    stages = status.get("stages", [])
    states = [stage.get("state") for stage in stages if isinstance(stage, dict)]
    if any(state == "failed" for state in states):
        status["overall_state"] = "failed"
        return
    if states and all(state in TERMINAL_STATES for state in states):
        status["overall_state"] = (
            "degraded" if any(state == "degraded" for state in states) else "ready"
        )
        return
    status["overall_state"] = "running"


def _locked_update(mutator: Any, *, event: str | None = None,
                   context: operations.OperationContext | None = None,
                   mode: str | None = None, allow_expired: bool = False,
                   lock_timeout: float | None = None) -> bool:
    """Apply an update without ever making login restoration depend on the HUD."""
    try:
        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        lock_path = root / "login-hud-status.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            if lock_timeout is None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            else:
                lock_deadline = time.monotonic() + max(0.0, lock_timeout)
                while True:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = lock_deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        time.sleep(min(0.01, remaining))
            try:
                with status_path().open(encoding="utf-8") as stream:
                    status = json.load(stream)
                if not isinstance(status, dict) or status.get("schema_version") != SCHEMA_VERSION:
                    status = _recover_startup_status()
            except (OSError, json.JSONDecodeError):
                status = _recover_startup_status()
            authority = context or operations.current()
            if authority is None:
                # Compatibility for initial startup telemetry only. Workers
                # must never adopt a shutdown operation merely by seeing it.
                if status.get("mode") != "startup":
                    return False
                authority = operations.OperationContext.from_dict(status.get("operation_context"))
                operations.bind(authority)
            if not authority.matches(status) or (mode and authority.mode != mode):
                return False
            owner = json.loads(operation_path().read_text()).get("operation_context")
            if owner != authority.to_dict():
                return False
            if not allow_expired:
                authority.check()
            elif authority.boot_id != operations.boot_id():
                return False
            _ensure_stage_metadata(status)
            mutator(status)
            status["updated_at"] = _now()
            _recompute(status)
            atomic_json(status_path(), status)
            if event:
                _append_log_unlocked(event)
        return True
    except (OSError, TypeError, ValueError):
        return False


def initialize(session_id: str, *, show_startup_hud: bool = True) -> bool:
    """Start a clean status document and log for a newly registered login."""
    try:
        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        _write_startup_policy(session_id, show_startup_hud)
        lock_path = root / "login-hud-status.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with status_path().open(encoding="utf-8") as stream:
                    existing = json.load(stream)
            except (OSError, json.JSONDecodeError):
                existing = None
            if (
                isinstance(existing, dict)
                and existing.get("schema_version") == SCHEMA_VERSION
                and existing.get("session_id") == session_id
                and existing.get("mode") == "startup"
            ):
                visibility_changed = (
                    existing.get("show_startup_hud") != bool(show_startup_hud)
                )
                existing["show_startup_hud"] = bool(show_startup_hud)
                if "operation_context" not in existing:
                    context = operations.OperationContext.create(session_id, "startup")
                    existing.update(operation_context=context.to_dict(), operation_id=context.operation_id,
                                    operation_state="running", commit_authorized=False, recovery_pending=False)
                    visibility_changed = True
                operations.bind(operations.OperationContext.from_dict(existing["operation_context"]))
                _record_operation(existing)
                metadata_changed = _ensure_stage_metadata(existing)
                if metadata_changed:
                    _recompute(existing)
                if metadata_changed or visibility_changed:
                    existing["updated_at"] = _now()
                    atomic_json(status_path(), existing)
                _append_log_unlocked("login status publisher reattached")
                return True
            log_path().write_text("", encoding="utf-8")
            log_path().chmod(0o600)
            initial = _initial_status(session_id, show_startup_hud)
            _record_operation(initial)
            atomic_json(status_path(), initial)
            operations.bind(operations.OperationContext.from_dict(initial["operation_context"]))
            _append_log_unlocked("login status initialized")
        return True
    except OSError:
        return False


def initialize_shutdown(
    session_id: str,
    operation_id: str,
    *,
    action: str = "poweroff",
    origin: str = "preflight",
) -> bool:
    """Replace startup telemetry with one complete shutdown transaction."""
    if action not in {"poweroff", "restart"}:
        raise ValueError(f"invalid shutdown action: {action}")
    if origin != "preflight":
        raise ValueError(f"invalid shutdown origin: {origin}")
    try:
        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        lock_path = root / "login-hud-status.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            cancel_path().unlink(missing_ok=True)
            shutdown_rendered_path().unlink(missing_ok=True)
            shutdown_commit_path().unlink(missing_ok=True)
            shutdown_worker_complete_path().unlink(missing_ok=True)
            log_path().write_text("", encoding="utf-8")
            log_path().chmod(0o600)
            initial = _initial_shutdown_status(session_id, operation_id, action=action, origin=origin)
            _record_operation(initial)
            atomic_json(status_path(), initial)
            operations.bind(operations.OperationContext.from_dict(initial["operation_context"]))
            _append_log_unlocked("shutdown checkpoint initialized")
        return True
    except OSError:
        return False


def register_shutdown_stages(stages: list[tuple[str, str]]) -> bool:
    """Insert validated dynamic shutdown jobs before the integrity proof."""
    normalized: list[tuple[str, str]] = []
    seen: set[str] = set()
    for identifier, label in stages:
        if (
            not isinstance(identifier, str)
            or not identifier.startswith("profile-")
            or len(identifier) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-" for character in identifier)
            or not isinstance(label, str)
            or not label
            or len(label) > 160
            or identifier in seen
        ):
            raise ValueError("invalid dynamic shutdown stage")
        seen.add(identifier)
        normalized.append((identifier, label))

    def mutate(status: dict[str, Any]) -> None:
        if status.get("mode") != "shutdown":
            raise ValueError("dynamic shutdown stages require shutdown status")
        existing = {
            str(stage.get("id"))
            for stage in status.get("stages", [])
            if isinstance(stage, dict)
        }
        additions = [
            _stage_document(identifier, label, "shutdown")
            for identifier, label in normalized
            if identifier not in existing
        ]
        stages_list = status.setdefault("stages", [])
        proof_index = next(
            (
                index for index, stage in enumerate(stages_list)
                if isinstance(stage, dict) and stage.get("id") == "checkpoint-proof"
            ),
            len(stages_list),
        )
        stages_list[proof_index:proof_index] = additions

    return _locked_update(
        mutate,
        event=(
            "shutdown profiles registered: "
            + (", ".join(identifier for identifier, _label in normalized) or "none")
        ),
    )


def update_stage(
    identifier: str,
    state: str,
    message: str,
    *,
    current: int | None = None,
    total: int | None = None,
    error: str | None = None,
    context: operations.OperationContext | None = None,
) -> bool:
    if state not in VALID_STATES:
        raise ValueError(f"invalid login stage state: {state}")

    def mutate(status: dict[str, Any]) -> None:
        stages = status.setdefault("stages", [])
        stage = next(
            (item for item in stages if isinstance(item, dict) and item.get("id") == identifier),
            None,
        )
        if stage is None:
            defaults = SHUTDOWN_STAGES if status.get("mode") == "shutdown" else DEFAULT_STAGES
            stage = _stage_document(
                identifier,
                dict(defaults).get(identifier, identifier.replace("-", " ").title()),
                str(status.get("mode") or "startup"),
            )
            if status.get("mode") == "shutdown" and identifier in {"social-apps-save", "file-manager-save"}:
                proof_index = next((i for i, item in enumerate(stages) if item.get("id") == "checkpoint-proof"), len(stages))
                stages.insert(proof_index, stage)
            else:
                stages.append(stage)
        stage.update({"state": state, "message": str(message)})
        _record_stage_event(stage, state, str(message))
        if current is None:
            stage.pop("current", None)
        else:
            stage["current"] = max(0, int(current))
        if total is None:
            stage.pop("total", None)
        else:
            stage["total"] = max(0, int(total))
        if error:
            stage["error"] = str(error)
        elif state not in {"failed", "degraded"}:
            stage.pop("error", None)

    suffix = f": {error}" if error else ""
    return _locked_update(
        mutate,
        event=f"{identifier}: {state} - {message}{suffix}",
        context=context,
        mode="startup" if identifier in dict(DEFAULT_STAGES) else
             "shutdown" if identifier in dict(SHUTDOWN_STAGES) or identifier.startswith("profile-") else None,
        allow_expired=state == "failed" or identifier == "profile-recovery",
    )


def set_overall(state: str, message: str) -> bool:
    if state not in VALID_STATES:
        raise ValueError(f"invalid overall login state: {state}")

    def mutate(status: dict[str, Any]) -> None:
        status["overall_message"] = str(message)
        # _recompute derives failure/readiness from stage truth. An explicit
        # degraded/failed state is reflected by the aggregate stage updates.

    return _locked_update(mutate, event=f"overall: {state} - {message}")


def fail_active(message: str) -> bool:
    """Fail the currently running/waiting stage after an uncaught startup error."""
    def mutate(status: dict[str, Any]) -> None:
        stages = status.setdefault("stages", [])
        if any(
            isinstance(stage, dict) and stage.get("state") == "failed"
            for stage in stages
        ):
            status["overall_message"] = message
            return
        active = next(
            (
                stage for stage in reversed(stages)
                if isinstance(stage, dict) and stage.get("state") in {"running", "waiting"}
            ),
            None,
        )
        if active is None:
            active = next(
                (stage for stage in stages if isinstance(stage, dict) and stage.get("state") == "pending"),
                None,
            )
        if active is not None:
            active.update({"state": "failed", "message": message, "error": message})
            _record_stage_event(active, "failed", message)
        status["overall_message"] = message

    return _locked_update(mutate, event=f"ERROR: {message}", allow_expired=True)


def _refresh_provider_placements(status: dict[str, Any]) -> None:
    from .provider_progress import refresh_stage_evidence
    refresh_stage_evidence(status)


def finish(message: str = "All login systems are ready") -> bool:
    def mutate(status: dict[str, Any]) -> None:
        _refresh_provider_placements(status)
        states = [stage.get("state") for stage in status.get("stages", []) if isinstance(stage, dict)]
        if any(state not in TERMINAL_STATES for state in states):
            from .provider_progress import has_pending
            status["overall_message"] = ("Waiting for application placement verification" if has_pending(status)
                                         else "Login initialization is still in progress")
        elif any(state in {"failed", "degraded"} for state in states):
            status["overall_message"] = "Login completed with items needing attention"
        else:
            status["overall_message"] = message

    return _locked_update(mutate, event="login finalization evidence refreshed", allow_expired=True)


def set_operation_state(state: str) -> bool:
    return _locked_update(lambda document: operations.transition(document, state),
                          event=f"operation: {state}", allow_expired=state != "authorized")


def publish_provider_results(identifier: str, results: Any, *, error: str | None = None) -> bool:
    """Persist phase evidence and derive readiness instead of trusting counts."""
    values = [result.to_dict() for result in results]
    if not values:
        return True
    if len(values) > 128:
        values = values[:128]
        error = "Provider result limit exceeded; remaining items are unverified"
    def mutate(status: dict[str, Any]) -> None:
        stage = next((item for item in status.get("stages", []) if item.get("id") == identifier), None)
        if stage is None:
            raise ValueError("provider results require a registered stage")
        stage["provider_results"] = values
        if error:
            stage["provider_error"] = error
        else:
            stage.pop("provider_error", None)
        for item in values:
            details = "; ".join(f"{phase}: {item[phase]['state']} ({item[phase]['detail']})"
                                for phase in ("identity", "content", "placement"))
            _record_stage_event(stage, stage["state"], f"{item['item_id']}: {details}")
        _refresh_provider_placements(status)
    return _locked_update(mutate, mode="startup", allow_expired=True)


def cancel_shutdown(
    message: str = "Shutdown was cancelled",
    *,
    recovery_pending: bool = False,
) -> bool:
    """Make a cancelled shutdown terminal so the Shell HUD can close."""
    def mutate(status: dict[str, Any]) -> None:
        if status.get("operation_state") not in operations.RECOVERY_STATES | {"cancelled"}:
            operations.transition(status, "cancelling")
        operations.transition(status, "recovering" if recovery_pending else "cancelled")
        for stage in status.get("stages", []):
            if not isinstance(stage, dict):
                continue
            if stage.get("state") not in TERMINAL_STATES:
                stage.update({"state": "skipped", "message": message})
                _record_stage_event(stage, "skipped", message)
                stage.pop("current", None)
                stage.pop("total", None)
                stage.pop("error", None)
        if recovery_pending:
            stages = status.setdefault("stages", [])
            recovery = next(
                (
                    stage for stage in stages
                    if isinstance(stage, dict) and stage.get("id") == "profile-recovery"
                ),
                None,
            )
            if recovery is None:
                recovery = _stage_document(
                    "profile-recovery", "Shutdown cancellation recovery", "shutdown"
                )
                stages.append(recovery)
            recovery.update({
                "state": "running",
                "message": "Restoring jobs changed during shutdown preparation",
            })
            _record_stage_event(
                recovery,
                "running",
                "Restoring jobs changed during shutdown preparation",
            )
        status["cancelled"] = True
        status["overall_message"] = message

    return _locked_update(mutate, event=f"shutdown: cancelled - {message}",
                          mode="shutdown", allow_expired=True)


def consume_shutdown_cancel(operation_id: str) -> bool:
    """Consume one private, operation-bound Shell cancellation request."""
    try:
        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        root.chmod(0o700)
        lock_path = root / "login-hud-status.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            path = cancel_path()
            try:
                with path.open(encoding="utf-8") as stream:
                    request = json.load(stream)
            except OSError:
                return False
            except json.JSONDecodeError:
                path.unlink(missing_ok=True)
                return False
            path.unlink(missing_ok=True)
            try:
                status = json.loads(status_path().read_text())
            except (OSError, ValueError):
                return False
            return (
                isinstance(request, dict)
                and request.get("schema_version") == SCHEMA_VERSION
                and request.get("operation_id") == operation_id
                and request.get("session_id") == status.get("session_id")
                and operations.receipt_matches(status, request, allow_expired=True)
            )
    except OSError:
        return False


def append_diagnostic(title: str, text: str) -> bool:
    """Append bounded command diagnostics to the private full-login log."""
    if not text.strip():
        return True
    bounded = text.rstrip()[-32_000:]
    try:
        root = runtime_root()
        root.mkdir(parents=True, exist_ok=True)
        lock_path = root / "login-hud-status.lock"
        with lock_path.open("a+", encoding="utf-8") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            _append_log_unlocked(f"--- {title} ---\n{bounded}\n--- end {title} ---")
        return True
    except OSError:
        return False
