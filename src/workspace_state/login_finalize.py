"""Load network drives after workspace restoration and report HUD progress."""

from __future__ import annotations

import subprocess
import json
import os
import sys
import time
from pathlib import Path

from . import host_config, operations
from .host_config import Drive
from .cli import finish_deferred_codex, finish_deferred_file_manager, finish_deferred_vscode
from .login_status import append_diagnostic, fail_active, finish, set_overall, status_path, update_stage
from .util import atomic_json


DRIVE_TIMEOUT_SECONDS = 360.0
WARMUP_TIMEOUT_SECONDS = 13 * 60.0


def _run(*args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    _check_operation()
    context = operations.current()
    if context is not None:
        timeout = context.remaining(timeout)
    if context is None or context.mode != "startup":
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                              check=False, env=operations.child_environment())
    process = subprocess.Popen(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=operations.child_environment())
    deadline = time.monotonic() + timeout
    try:
        while True:
            context.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(.25, remaining))
                return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    except BaseException:
        # This is the finite command helper only, never an application cgroup.
        # A submitted systemd mount/start job remains managed by systemd.
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise


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


def host_config_stage() -> str:
    from .login_status import CONFIG_STAGE
    return CONFIG_STAGE[0]


def _start_drives() -> dict[str, bool]:
    results: dict[str, bool] = {}
    pending: dict[str, tuple[Drive, float]] = {}
    config = host_config.load()
    if config.error:
        # An unreadable private configuration is a visible failed stage, never a crash.
        update_stage(host_config_stage(), "failed", config.error, error=config.error)
        append_diagnostic("invalid host configuration", config.error)
        return {host_config_stage(): False}
    for drive in config.started:
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


def _attention_startup_stages() -> list[str]:
    """Failed or degraded stages published by restoration or finalization."""
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        return []
    if not isinstance(status, dict) or status.get("mode") != "startup":
        return []
    from .provider_progress import item_needs_attention
    return [
        stage["id"] for stage in status.get("stages", [])
        if isinstance(stage, dict)
        and (stage.get("state") in {"failed", "degraded"}
             # A waiting stage that already has failed items still needs attention.
             or stage.get("state") == "waiting" and isinstance(stage.get("provider_results"), list)
             and any(item_needs_attention(item) for item in stage["provider_results"]))
        and stage.get("id") != "login-finalization"
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
    # Without configured drives there is nothing to warm and the stage does not exist.
    warmup_ok = _warm_cloud_metadata() if host_config.load().started else True
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
    failed = list(dict.fromkeys([*failed, *_attention_startup_stages()]))
    if failed:
        # Failed or degraded stages are results, not a failure of this service:
        # finalization itself completed, and the overall message reports them.
        message = "Login finalization completed; items need attention: " + ", ".join(failed)
        update_stage("login-finalization", "ready", message, current=1, total=1)
        finish()
        return 0
    update_stage("login-finalization", "ready", "Post-workspace login systems verified", current=1, total=1)
    finish("All login systems are ready")
    return 0


def _wait_pending_providers() -> None:
    """Keep startup ownership while exact content/native receipts are genuinely pending."""
    from .provider_progress import has_pending, reconcile_pending, only_native_handoffs_pending
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
        if only_native_handoffs_pending(document):
            # The compositor already owns every remaining final target. The
            # coordinator observes those receipts after unlock; keeping this
            # service alive would turn a long screen lock into a timeout failure.
            return
        # This only observes: it never launches Chrome, navigates tabs or retries restore.
        reconcile_pending(context)
        if not has_pending(json.loads(status_path().read_text())):
            return
        time.sleep(min(.5, context.remaining()))


def main() -> int:
    previous = operations.current()
    context = None
    try:
        context = operations.context_from_status(status_path(), "startup", allow_expired=True)
        state = json.loads(status_path().read_text()).get("operation_state")
        if state in {"completed", "failed", "cancelled"}:
            finalizer = next((stage for stage in json.loads(status_path().read_text()).get("stages", [])
                              if isinstance(stage, dict) and stage.get("id") == "login-finalization"), {})
            # A finalizer that already reported needs-attention results is done.
            return 0 if state == "completed" or (
                state == "failed" and finalizer.get("state") == "ready") else 1
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
    if args:
        raise SystemExit("usage: wsctl-login-finalize [--service-result]")
    raise SystemExit(main())
