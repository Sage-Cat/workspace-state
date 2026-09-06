"""Load network drives after workspace restoration and report HUD progress."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .login_status import append_diagnostic, fail_active, finish, set_overall, update_stage


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
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)


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
            time.sleep(1.0)
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


def main() -> int:
    set_overall("running", "Workspace restored; loading cloud systems")
    drive_results = _start_drives()
    warmup_ok = _warm_cloud_metadata()
    failed = [stage for stage, ready in drive_results.items() if not ready]
    if not warmup_ok:
        failed.append("warmup")
    if failed:
        message = "Login completed with failures: " + ", ".join(failed)
        set_overall("failed", message)
        # Ensure the aggregate cannot be accidentally shown as successful.
        fail_active(message)
        return 1
    finish("All login systems are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
