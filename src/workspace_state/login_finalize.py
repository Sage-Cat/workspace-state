"""Load network drives after workspace restoration and report HUD progress."""

from __future__ import annotations

import subprocess
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from . import operations
from .cli import finish_deferred_codex, finish_deferred_file_manager, finish_deferred_vscode
from .login_status import append_diagnostic, fail_active, finish, set_overall, status_path, update_stage
from .util import atomic_json


@dataclass(frozen=True)
class Drive:
    stage: str
    label: str
    unit: str
    mountpoint: Path


DRIVES = (
    Drive("gdrive", "Google Drive", "rclone-gdrive.service", Path("/home/sagecat/Drives/gdrive")),
    Drive(
        "nextcloud",
        "Nextcloud drive",
        "rclone-sagecat-serv-drive.service",
        Path("/home/sagecat/Drives/sagecat-serv-drive"),
    ),
    Drive("pdrive", "Proton Drive", "protondrive-mount-pdrive.service", Path("/home/sagecat/Drives/pdrive")),
)
DRIVE_TIMEOUT_SECONDS = 360.0
WARMUP_TIMEOUT_SECONDS = 13 * 60.0


def _run(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    _check_operation()
    context = operations.current()
    if context is not None:
        timeout = context.remaining(timeout)
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                          check=False, env=operations.child_environment())


def _check_operation(*, allow_expired: bool = False) -> None:
    context = operations.current()
    if context is None:
        return
    if allow_expired:
        if context.boot_id != operations.boot_id():
            raise RuntimeError("startup finalization belongs to a previous boot")
    else:
        context.check()
    if not context.matches(json.loads(status_path().read_text())):
        raise RuntimeError("startup finalizer no longer owns the current operation")
    from .startup import startup_suspended
    if startup_suspended(status_path().parent, context.boot_id, context.login_generation):
        raise RuntimeError("startup finalization is suspended for shutdown")


def _unit_properties(unit: str) -> dict[str, str]:
    result = _run(
        "/usr/bin/systemctl", "--user", "show", unit,
        "--property=ActiveState", "--property=SubState", "--property=Result",
    )
    if result.returncode:
        return {"ActiveState": "unknown", "SubState": "unknown", "Result": "unknown"}
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def _mounted(path: Path) -> bool:
    return _run("/usr/bin/findmnt", "--mountpoint", str(path)).returncode == 0


def _diagnose(unit: str) -> None:
    result = _run(
        "/usr/bin/journalctl", "--user", "--unit", unit,
        "--boot", "--no-pager", "--lines=120", "--output=short-precise",
    )
    append_diagnostic(f"journal for {unit}", result.stdout or result.stderr)


def _start_drives() -> dict[str, bool]:
    results: dict[str, bool] = {}
    pending: dict[str, tuple[Drive, float]] = {}
    for drive in DRIVES:
        if _mounted(drive.mountpoint):
            update_stage(drive.stage, "ready", f"Mounted at {drive.mountpoint}", current=1, total=1)
            results[drive.stage] = True
            continue
        update_stage(drive.stage, "running", f"Starting {drive.label}")
        command = _run(
            "/usr/bin/systemctl", "--user", "start", "--no-block", drive.unit,
        )
        if command.returncode:
            detail = command.stderr.strip() or command.stdout.strip() or "systemd start failed"
            update_stage(drive.stage, "failed", detail, error=detail)
            append_diagnostic(f"could not start {drive.unit}", detail)
            _diagnose(drive.unit)
            results[drive.stage] = False
            continue
        pending[drive.stage] = (drive, time.monotonic() + DRIVE_TIMEOUT_SECONDS)

    while pending:
        for stage, (drive, deadline) in list(pending.items()):
            if _mounted(drive.mountpoint):
                update_stage(stage, "ready", f"Mounted at {drive.mountpoint}", current=1, total=1)
                results[stage] = True
                del pending[stage]
                continue
            properties = _unit_properties(drive.unit)
            active = properties.get("ActiveState", "unknown")
            sub = properties.get("SubState", "unknown")
            result = properties.get("Result", "unknown")
            if active == "failed" or result not in {"success", "unknown", ""}:
                detail = f"{drive.unit}: {active}/{sub}, result {result}"
                update_stage(stage, "failed", detail, error=detail)
                _diagnose(drive.unit)
                results[stage] = False
                del pending[stage]
            elif time.monotonic() >= deadline:
                detail = f"Mount did not appear within {DRIVE_TIMEOUT_SECONDS:g} seconds"
                update_stage(stage, "failed", detail, error=detail)
                _diagnose(drive.unit)
                results[stage] = False
                del pending[stage]
            else:
                update_stage(stage, "running", f"{drive.label}: {active}/{sub}")
        if pending:
            _check_operation()
            context = operations.current()
            time.sleep(context.remaining(1.0) if context else 1.0)
    return results


def _warm_cloud_metadata() -> bool:
    update_stage("warmup", "running", "Reading cloud-drive directory metadata")
    try:
        result = _run(
            "/usr/bin/systemctl", "--user", "start", "cloud-drives-warmup.service",
            timeout=WARMUP_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        detail = f"Cloud metadata warm-up exceeded {WARMUP_TIMEOUT_SECONDS:g} seconds"
        update_stage("warmup", "failed", detail, error=detail)
        _diagnose("cloud-drives-warmup.service")
        return False
    properties = _unit_properties("cloud-drives-warmup.service")
    if result.returncode or properties.get("Result") not in {"success", ""}:
        detail = (
            result.stderr.strip() or result.stdout.strip()
            or f"cloud-drives-warmup.service result {properties.get('Result', 'unknown')}"
        )
        update_stage("warmup", "failed", detail, error=detail)
        _diagnose("cloud-drives-warmup.service")
        return False
    update_stage("warmup", "ready", "Cloud metadata cache is warm", current=1, total=1)
    # Future hourly refreshes begin only after this login's first warm-up.
    timer = _run(
        "/usr/bin/systemctl", "--user", "start", "--no-block",
        "cloud-drives-warmup.timer",
    )
    if timer.returncode:
        append_diagnostic("could not start cloud-drives-warmup.timer", timer.stderr or timer.stdout)
    return True


def _failed_startup_stages() -> list[str]:
    """Include failures published by restoration before drive finalization."""
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        return []
    if not isinstance(status, dict) or status.get("mode") != "startup":
        return []
    return [
        stage["id"] for stage in status.get("stages", [])
        if isinstance(stage, dict) and stage.get("state") == "failed"
        and isinstance(stage.get("id"), str) and stage["id"]
    ]


def _finalize() -> int:
    _check_operation()
    update_stage("login-finalization", "running", "Checking post-workspace login systems")
    set_overall("running", "Workspace restored; loading cloud systems")
    drive_results = _start_drives()
    try:
        _check_operation()
        file_manager_ok = finish_deferred_file_manager()
    except (OSError, RuntimeError, ValueError) as error:
        detail = f"deferred file-manager restore failed: {error}"
        update_stage("file-manager", "failed", detail, error=detail)
        append_diagnostic("deferred file-manager restore failed", detail)
        file_manager_ok = False
    try:
        _check_operation()
        vscode_ok = finish_deferred_vscode()
    except (OSError, RuntimeError, ValueError) as error:
        detail = f"deferred VS Code restore failed: {error}"
        update_stage("vscode", "failed", detail, error=detail)
        append_diagnostic("deferred VS Code restore failed", detail)
        vscode_ok = False
    codex_error = False
    try:
        _check_operation()
        finish_deferred_codex()
    except (OSError, RuntimeError, ValueError) as error:
        detail = f"deferred Codex verification failed: {error}"
        update_stage("codex", "failed", detail, error=detail)
        append_diagnostic("deferred Codex verification failed", detail)
        codex_error = True
    warmup_ok = _warm_cloud_metadata()
    failed = [stage for stage, ready in drive_results.items() if not ready]
    if not warmup_ok:
        failed.append("warmup")
    if not file_manager_ok:
        failed.append("file-manager")
    if not vscode_ok:
        failed.append("vscode")
    if codex_error:
        failed.append("codex")
    _wait_pending_providers()
    # Refresh provider/workspace aggregates before inspecting prior failures.
    # Our running stage prevents this observation from completing the operation.
    finish()
    failed = list(dict.fromkeys([*failed, *_failed_startup_stages()]))
    if failed:
        from .startup_failure import failure_message
        document = json.loads(status_path().read_text())
        message = failure_message(document, failed)
        update_stage("login-finalization", "failed", message, error=message)
        finish()
        set_overall("failed", message)
        # Ensure the aggregate cannot be accidentally shown as successful.
        fail_active(message)
        receipt = _invocation_receipt()
        context = operations.current()
        if receipt is not None and context is not None:
            # Publish only after the intentional aggregate failure was recorded.
            # A crash, timeout or old invocation must never borrow this diagnosis.
            atomic_json(receipt.with_suffix(".outcome.json"), {
                "operation_context": context.to_dict(), "kind": "startup-incomplete",
                "message": message, "failed_stages": failed,
            })
        return 1
    update_stage("login-finalization", "ready", "Post-workspace login systems verified", current=1, total=1)
    finish("All login systems are ready")
    return 0


def _wait_pending_providers() -> None:
    """Keep startup ownership while exact content/native receipts are genuinely pending."""
    from .provider_progress import (continue_pending_browser_placements, has_pending, reconcile_pending)
    context = operations.current()
    if context is None:
        return  # No startup authority can authorize a continuation.
    while True:
        _check_operation()
        document = json.loads(status_path().read_text())
        if not has_pending(document):
            return
        if document.get("operation_state") != "running":
            return
        # Uses the same nonblocking continuation lock as the coordinator child.
        # This never launches/recreates Chrome, navigates tabs, or retries restore.
        continue_pending_browser_placements(context)
        reconcile_pending(context)
        if not has_pending(json.loads(status_path().read_text())):
            return
        time.sleep(min(.5, context.remaining()))


def retry_operation(operation_id: str, *, new_attempt: bool = False) -> operations.OperationContext:
    """Explicitly retry only finalization, retaining verified same-login proof."""
    from . import login_status
    from .cli import _startup_directory
    from .provider_progress import CATEGORY_STAGES, evidence_state
    from .startup import read_stage_marker, write_stage_marker

    previous = operations.context_from_status(status_path(), "startup", operation_id,
                                              allow_expired=new_attempt)
    _check_operation(allow_expired=new_attempt)
    directory = _startup_directory()
    carried = []
    for category in CATEGORY_STAGES:
        path = directory / f"{category}.done"
        marker = read_stage_marker(path, category)
        if marker is None or not marker.verified_for(previous):
            raise RuntimeError(f"Cannot retry finalization without verified {category} attempt proof")
        carried.append((path, marker))
    for path in sorted((directory / "browser-items").glob("*.done"))[:128]:
        marker = read_stage_marker(path, "browsers")
        if marker is not None and marker.verified_for(previous):
            carried.append((path, marker))
    context = replace(previous, operation_id=uuid4().hex, attempt=previous.attempt + 1)
    if new_attempt:
        # This is an explicit user action after manual provider recovery. It
        # starts distinct authority; ordinary retries never extend a deadline.
        context = operations.OperationContext.create(previous.login_generation, "startup",
                                                      attempt=previous.attempt + 1)
    previous_document = None

    def begin(document):
        nonlocal previous_document
        # Shutdown may have suspended startup while this retry waited for the
        # status lock. Recheck before changing either proof or authority.
        _check_operation(allow_expired=new_attempt)
        stages = {stage.get("id"): stage for stage in document.get("stages", []) if isinstance(stage, dict)}
        finalizer = stages.get("login-finalization", {})
        if document.get("operation_state") not in {"running", "failed"} or finalizer.get("state") != "failed":
            raise RuntimeError("Only a failed login finalizer can be explicitly retried")
        if any(name not in stages for name, _label in login_status.DEFAULT_STAGES):
            raise RuntimeError("Cannot retry incomplete startup evidence")
        for name, stage in stages.items():
            if name == "login-finalization":
                continue
            if (stage.get("state") not in {"ready", "skipped"} or stage.get("provider_error")
                    or (stage.get("provider_results") and evidence_state(stage["provider_results"]) != "ready")):
                raise RuntimeError(f"Cannot retry finalization while {name} is unverified")
        for path, marker in carried:
            if read_stage_marker(path, marker.category) != marker:
                raise RuntimeError("Startup attempt proof changed during finalization retry")
            if path.parent == directory and tuple(stages[marker.category].get("provider_results", ())) != marker.provider_results:
                raise RuntimeError(f"Current {marker.category} evidence differs from its verified attempt")
        previous_document = json.loads(json.dumps(document))
        document.update(operation_context=context.to_dict(), operation_id=context.operation_id,
                        operation_state="running", recovery_pending=False, commit_authorized=False,
                        continued_from_operation=previous.to_dict())
        finalizer.update(state="running", message="Retrying verified login finalization")
        finalizer.pop("error", None)
        migrated = []
        authority_attempted = False
        try:
            # Keep proof migration under the same lock as ownership rotation:
            # a newer operation must not interleave and have its markers
            # overwritten by this retry after taking ownership.
            for path, marker in carried:
                migrated.append((path, marker))
                write_stage_marker(path, replace(marker, operation_context=context.to_dict()))
            authority_attempted = True
            login_status._record_operation(document)
        except (OSError, ValueError):
            # A failed migration has not published a new status document. Put
            # the old proof back so an explicit retry remains possible.
            for path, marker in reversed(migrated):
                write_stage_marker(path, marker)
            if authority_attempted:
                login_status._record_operation({"operation_context": previous.to_dict()})
            raise

    def rollback_publish():
        # _locked_update still owns the status lock here, including when the
        # failed atomic write replaced the status before raising (e.g. fsync).
        for path, marker in carried:
            write_stage_marker(path, marker)
        login_status._record_operation(previous_document)
        atomic_json(status_path(), previous_document)

    if not login_status._locked_update(begin, context=previous, mode="startup", allow_expired=new_attempt,
                                      lock_timeout=1.0 if new_attempt else previous.remaining(1.0),
                                      rollback_publish=rollback_publish):
        raise RuntimeError("Could not acquire the current startup operation for finalization retry")
    operations.bind(context)
    return context


def main(retry_operation_id: str | None = None, *, new_attempt: bool = False) -> int:
    previous = operations.current()
    context = None
    try:
        if new_attempt and retry_operation_id is None:
            raise ValueError("a new attempt requires the failed operation ID")
        context = (retry_operation(retry_operation_id, new_attempt=new_attempt) if retry_operation_id is not None else
                   operations.context_from_status(status_path(), "startup", allow_expired=True))
        state = json.loads(status_path().read_text()).get("operation_state")
        if state in {"completed", "failed", "cancelled"}:
            return 0 if state == "completed" else 1
        invocation = _invocation_receipt()
        if invocation is not None:
            atomic_json(invocation, context.to_dict())
        context.check()
        return _finalize()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        # Keep the original publisher authority: a late failure cannot mark a
        # replacement shutdown operation failed. Expiry does not erase errors.
        if context is not None:
            detail = f"Login finalization failed: {type(error).__name__}: {error}"
            update_stage("login-finalization", "failed", detail, error=detail)
            fail_active(detail)
            append_diagnostic("login finalizer", detail)
        else:
            print(f"wsctl-login-finalize: {error}", file=sys.stderr)
        return 1
    finally:
        operations.bind(previous)


def _invocation_receipt() -> Path | None:
    invocation = os.environ.get("INVOCATION_ID", "")
    if len(invocation) != 32 or any(char not in "0123456789abcdef" for char in invocation):
        return None
    return status_path().parent / "finalizers" / f"{invocation}.json"


def service_result() -> int:
    from . import login_status

    receipt = _invocation_receipt()
    if receipt is None:
        return 0
    try:
        context = operations.OperationContext.from_dict(json.loads(receipt.read_text()))
        with operations.publisher(context):
            operations.context_from_status(status_path(), "startup", allow_expired=True)
            result = os.environ.get("SERVICE_RESULT", "unknown")
            if result != "success":
                detail = f"Login finalizer service stopped before completion: {result}"

                def fail_unfinished(status: dict) -> None:
                    stage = next((item for item in status.get("stages", [])
                                  if isinstance(item, dict) and item.get("id") == "login-finalization"), None)
                    if (status.get("operation_state") in {"completed", "failed", "cancelled"}
                            or (stage and stage.get("state") in login_status.TERMINAL_STATES)):
                        # Abort publication under the same ownership lock. An
                        # intentional exit already has a more useful diagnosis.
                        raise ValueError("Finalizer already reported its outcome")
                    if stage is None:
                        stage = {"id": "login-finalization", "label": "Login Finalization"}
                        status.setdefault("stages", []).append(stage)
                    stage.update(state="failed", message=detail, error=detail)
                    login_status._record_stage_event(stage, "failed", detail)
                    login_status._refresh_provider_placements(status)
                    status["overall_message"] = detail

                login_status._locked_update(fail_unfinished, context=context, mode="startup",
                                            allow_expired=True, event=detail)
    except (OSError, RuntimeError, ValueError):
        pass  # Missing/stale receipts cannot adopt the latest operation.
    finally:
        receipt.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if args == ["--service-result"]:
        raise SystemExit(service_result())
    new_attempt = len(args) == 3 and args[-1] == "--new-attempt"
    if new_attempt:
        args = args[:-1]
    if args and (len(args) != 2 or args[0] != "--retry-operation"):
        raise SystemExit("usage: wsctl-login-finalize [--service-result | --retry-operation OPERATION_ID [--new-attempt]]")
    raise SystemExit(main(args[1] if args else None, new_attempt=new_attempt))
