"""Explicit VM round-trips, independent of the host power/session protocol."""

from __future__ import annotations

import fcntl
import os
import signal
import time
import uuid
from pathlib import Path

from . import shutdown_profiles as profiles
from .util import atomic_json


class Cancellation:
    def __init__(self) -> None:
        self.cancelled = False

    def requested(self) -> bool:
        return self.cancelled

    def handle(self, *_args: object) -> None:
        self.cancelled = True


def run_profile_test(identifier: str, *, restore_only: bool = False) -> Path:
    """Leave the VM running; never publish a host shutdown/startup transaction."""
    matches = [p for p in profiles.load_profiles() if p.identifier == identifier]
    if len(matches) != 1 or matches[0].adapter != "qemu-windows-hibernate":
        raise profiles.ShutdownProfileError("Select one installed Windows VM profile")
    if profiles.transaction_path().exists():
        raise profiles.ShutdownProfileError("A host shutdown profile transaction needs recovery first")
    profile = matches[0]
    vm_directory = Path(profile.adapter_config["vm_directory"])
    root = profiles.state_root() / "profile-tests"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    descriptor = os.open(root / "test.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise profiles.ShutdownProfileError("Another isolated VM test is running") from error
        return _run(profile, vm_directory, root, restore_only=restore_only)
    finally:
        os.close(descriptor)


def _run(profile, vm_directory: Path, root: Path, *, restore_only: bool) -> Path:
    report = root / f"{profile.identifier}-{uuid.uuid4().hex}.json"
    document = {"profile_id": profile.identifier, "restore_only": restore_only,
                "host_power_action": None, "events": [], "state": "running"}
    runtime = profiles.ProfileRuntime(profile)
    adapter = profiles.QemuWindowsHibernateAdapter()
    cancel = Cancellation()
    previous = {sig: signal.signal(sig, cancel.handle) for sig in (signal.SIGINT, signal.SIGTERM)}

    def record(phase: str, message: str, state: str = "running") -> None:
        document["state"] = state
        document["events"].append({"time": time.time(), "phase": phase, "message": message})
        atomic_json(report, document)
        report.chmod(0o600)
        print(f"{phase}: {message}", flush=True)

    try:
        record("report", str(report))
        if restore_only or profiles._live_qemu(vm_directory) is None:
            receipt = profiles._read_startup_restore()
            saved = [] if receipt is None else [
                item for item in receipt[1]
                if item.profile.identifier == profile.identifier
                and item.profile.adapter_config == profile.adapter_config
            ]
            if len(saved) != 1:
                raise profiles.ShutdownProfileError(
                    "No matching saved placement for explicit restoration; start/place the VM first and use the round-trip test"
                )
            runtime.state["restore_placement"] = saved[0].state["restore_placement"]
            if cancel.requested():
                raise profiles.ShutdownProfilesCancelled
            record("restore", adapter.rollback(runtime))
        else:
            if profiles._qemu_viewer_window(vm_directory) is None:
                raise profiles.ShutdownProfileError(
                    "VM has no visible viewer; run --restore-only before testing hibernation"
                )

        applicable, message = adapter.probe(runtime)
        if not applicable:
            raise profiles.ShutdownProfileError(message)
        document["placement"] = runtime.state["restore_placement"]
        document["original_pid"] = runtime.state["original_identity"].pid
        record("probe", message)
        if not restore_only:
            if cancel.requested():
                raise profiles.ShutdownProfilesCancelled
            # Persist exact placement BEFORE sending the guest power command.
            record("hibernate", "Requesting guest hibernation; host stays running")
            try:
                record("hibernate", adapter.prepare(runtime, cancel))
                record("verify", adapter.verify(runtime, cancel))
            except BaseException as error:
                record("hibernate-failed", str(error) or type(error).__name__, "failed")
                raise
            finally:
                # Even a failed/interrupt-requested prepare must attempt recovery.
                record("restore", "Restoring Windows and its saved viewer placement")
                record("restore", adapter.rollback(runtime))
        current = profiles._capture_qemu_restore_placement(vm_directory)
        expected = runtime.state["restore_placement"]
        if any(current[key] != expected[key] for key in ("workspace_name", "monitor_identity", "state", "geometry_relative")):
            raise profiles.ShutdownProfileError("Restored viewer differs from the saved placement")
        identity = profiles._live_qemu(vm_directory)
        if identity is None:
            raise profiles.ShutdownProfileError("VM disappeared after viewer verification")
        document["restored_pid"] = identity.pid
        if cancel.requested():
            record("cancelled", "VM recovered; isolated test was cancelled", "cancelled")
            raise profiles.ShutdownProfilesCancelled
        record("complete", f"QEMU {identity.pid}, QGA and viewer verified at {current['workspace_name']}", "ready")
        return report
    except BaseException as error:
        if document["state"] != "cancelled":
            record("failed", str(error) or type(error).__name__, "failed")
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
