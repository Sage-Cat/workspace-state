"""Load network drives after workspace restoration and report HUD progress."""

from __future__ import annotations

import subprocess
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

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


def _check_operation() -> None:
    context = operations.current()
    if context is None:
        return
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
    failed = list(dict.fromkeys([*failed, *_failed_startup_stages()]))
    if failed:
        message = "Login completed with failures: " + ", ".join(failed)
        set_overall("failed", message)
        # Ensure the aggregate cannot be accidentally shown as successful.
        fail_active(message)
        return 1
    finish("All login systems are ready")
    return 0


def main() -> int:
    previous = operations.current()
    try:
        context = operations.context_from_status(status_path(), "startup", allow_expired=True)
        invocation = _invocation_receipt()
        if invocation is not None:
            atomic_json(invocation, context.to_dict())
        context.check()
        return _finalize()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        # Keep the original publisher authority: a late failure cannot mark a
        # replacement shutdown operation failed. Expiry does not erase errors.
        if operations.current() is not None:
            detail = f"Login finalization failed: {type(error).__name__}: {error}"
            update_stage("login-finalization", "failed", detail, error=detail)
            fail_active(detail)
            append_diagnostic("login finalizer", detail)
        return 1
    finally:
        operations.bind(previous)


def _invocation_receipt() -> Path | None:
    invocation = os.environ.get("INVOCATION_ID", "")
    if len(invocation) != 32 or any(char not in "0123456789abcdef" for char in invocation):
        return None
    return status_path().parent / "finalizers" / f"{invocation}.json"


def service_result() -> int:
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
                update_stage("login-finalization", "failed", detail, error=detail)
                fail_active(detail)
    except (OSError, RuntimeError, ValueError):
        pass  # Missing/stale receipts cannot adopt the latest operation.
    finally:
        receipt.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(service_result() if sys.argv[1:] == ["--service-result"] else main())
