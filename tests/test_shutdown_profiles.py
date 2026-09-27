from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import shutdown_profiles


class NeverCancelled:
    def requested(self) -> bool:
        return False


class ToggleCancellation:
    def __init__(self) -> None:
        self.value = False

    def requested(self) -> bool:
        return self.value


class FakeAdapter:
    def __init__(self, *, cancel: ToggleCancellation | None = None) -> None:
        self.cancel = cancel
        self.calls: list[str] = []

    def probe(self, runtime):
        self.calls.append("probe")
        runtime.state["identity"] = "retained"
        return True, "active"

    def prepare(self, runtime, cancel):
        self.calls.append("prepare")
        self.assert_runtime(runtime)
        if self.cancel is not None:
            self.cancel.value = True
        return "prepared"

    def verify(self, runtime, cancel):
        self.calls.append("verify")
        return "verified"

    def rollback(self, runtime):
        self.calls.append("rollback")
        return "restored"

    @staticmethod
    def assert_runtime(runtime):
        if runtime.state.get("identity") != "retained":
            raise AssertionError("probe state was not retained")


class ShutdownProfilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config"
        self.runtime = self.root / "runtime"
        environment = patch.dict(
            os.environ,
            {
                "XDG_CONFIG_HOME": str(self.config),
                "XDG_RUNTIME_DIR": str(self.runtime),
                "XDG_STATE_HOME": str(self.root / "state"),
                "WSCTL_SHUTDOWN_PROFILE_DIRS": str(self.config / "profiles"),
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def command_mapping(**overrides):
        result = {
            "schema_version": 1,
            "id": "example-job",
            "label": "Example job",
            "adapter": "command",
            "actions": ["poweroff", "restart"],
            "critical": True,
            "timeout_seconds": 10,
            "rollback_timeout_seconds": 20,
            "cancel_policy": "terminate-then-rollback",
            "probe": ["/usr/bin/true"],
            "prepare": ["/usr/bin/true"],
            "verify": ["/usr/bin/true"],
            "rollback": ["/usr/bin/true"],
        }
        result.update(overrides)
        return result

    @staticmethod
    def qemu_placement():
        return {
            "workspace": 2,
            "workspace_name": "Windows",
            "monitor": 1,
            "monitor_identity": {
                "connector": "HDMI-2",
                "edid_hash": "physical-display",
                "serial": "DISPLAY-1",
            },
            "monitor_intent": {
                "connector": "HDMI-2",
                "edid_hash": "physical-display",
                "serial": "DISPLAY-1",
            },
            "monitor_geometry": {
                "connector": "HDMI-2",
                "x": 1920,
                "y": 0,
                "width": 1920,
                "height": 1080,
            },
            "geometry": {
                "x": 1920,
                "y": 0,
                "width": 1920,
                "height": 1080,
            },
            "geometry_relative": {
                "x": 0,
                "y": 0,
                "width": 1920,
                "height": 1080,
            },
            "state": "maximized",
        }

    def write_profile(self, text: str, name: str = "job.toml") -> Path:
        directory = self.config / "profiles"
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / name
        path.write_text(text)
        path.chmod(0o600)
        return path

    def test_loads_strict_command_profile(self):
        self.write_profile(
            """schema_version = 1
id = "example-job"
label = "Example job"
adapter = "command"
actions = ["poweroff"]
critical = true
timeout_seconds = 10
rollback_timeout_seconds = 20
cancel_policy = "terminate-then-rollback"
probe = ["/usr/bin/true"]
prepare = ["/usr/bin/true"]
verify = ["/usr/bin/true"]
rollback = ["/usr/bin/true"]
"""
        )

        profiles = shutdown_profiles.load_profiles()

        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0].identifier, "example-job")
        self.assertEqual(profiles[0].stage_id, "profile-example-job")
        self.assertEqual(profiles[0].actions, frozenset({"poweroff"}))

    def test_rejects_relative_executable_and_writable_config(self):
        raw = self.command_mapping(probe=["relative-command"])
        with self.assertRaisesRegex(
            shutdown_profiles.ShutdownProfileError, "absolute executable"
        ):
            shutdown_profiles._profile_from_mapping(raw)

        path = self.write_profile("schema_version = 1\n")
        path.chmod(0o622)
        with self.assertRaisesRegex(
            shutdown_profiles.ShutdownProfileError, "writable-by-others"
        ):
            shutdown_profiles.load_profiles()

    def test_disabled_profile_is_ignored(self):
        self.write_profile(
            """schema_version = 1
id = "disabled-job"
label = "Disabled job"
adapter = "command"
enabled = false
"""
        )
        self.assertEqual(shutdown_profiles.load_profiles(), [])

    def test_session_writes_ahead_and_rolls_back_exact_profile(self):
        profile = shutdown_profiles._profile_from_mapping(self.command_mapping())
        adapter = FakeAdapter()
        events = []
        session = shutdown_profiles.ShutdownProfileSession(
            [profile],
            operation_id="a" * 32,
            session_id="b" * 16,
            action="poweroff",
            cancel=NeverCancelled(),
            reporter=lambda *event: events.append(event),
        )
        with patch(
            "workspace_state.shutdown_profiles._adapter_for", return_value=adapter
        ):
            session.run()
            self.assertTrue(shutdown_profiles.transaction_exists("a" * 32))
            transaction = json.loads(shutdown_profiles.transaction_path().read_text())
            self.assertEqual(transaction["profiles"][0]["id"], "example-job")
            session.rollback_all("cancelled")

        self.assertEqual(adapter.calls, ["probe", "prepare", "verify", "rollback"])
        self.assertFalse(shutdown_profiles.transaction_path().exists())
        self.assertIn(("profile-example-job", "ready", "verified"), events)
        self.assertIn(("profile-example-job", "skipped", "restored"), events)

    def test_finish_then_cancel_is_rolled_back_after_prepare(self):
        profile = shutdown_profiles._profile_from_mapping(
            self.command_mapping(cancel_policy="finish-then-rollback")
        )
        cancellation = ToggleCancellation()
        adapter = FakeAdapter(cancel=cancellation)
        session = shutdown_profiles.ShutdownProfileSession(
            [profile],
            operation_id="c" * 32,
            session_id="d" * 16,
            action="restart",
            cancel=cancellation,
            reporter=lambda *_event: None,
        )
        with patch(
            "workspace_state.shutdown_profiles._adapter_for", return_value=adapter
        ):
            with self.assertRaises(shutdown_profiles.ShutdownProfilesCancelled):
                session.run()
            session.rollback_all("cancelled")

        self.assertEqual(adapter.calls, ["probe", "prepare", "rollback"])
        self.assertFalse(shutdown_profiles.transaction_path().exists())

    def test_execstop_recovery_rehydrates_the_snapshotted_profile(self):
        profile = shutdown_profiles._profile_from_mapping(self.command_mapping())
        runtime = shutdown_profiles.ProfileRuntime(profile)
        shutdown_profiles._write_transaction(
            "e" * 32, "f" * 16, "poweroff", [runtime]
        )
        adapter = FakeAdapter()
        with patch(
            "workspace_state.shutdown_profiles._adapter_for", return_value=adapter
        ):
            shutdown_profiles.recover_transaction("e" * 32)

        self.assertEqual(adapter.calls, ["rollback"])
        self.assertFalse(shutdown_profiles.transaction_path().exists())

    def test_new_operation_cannot_overwrite_an_armed_rollback_journal(self):
        profile = shutdown_profiles._profile_from_mapping(self.command_mapping())
        runtime = shutdown_profiles.ProfileRuntime(profile)
        shutdown_profiles._write_transaction(
            "a" * 32, "b" * 16, "poweroff", [runtime]
        )

        with self.assertRaisesRegex(
            shutdown_profiles.ShutdownProfileError, "another shutdown operation"
        ):
            shutdown_profiles._write_transaction(
                "c" * 32, "b" * 16, "restart", [runtime]
            )

        document = json.loads(shutdown_profiles.transaction_path().read_text())
        self.assertEqual(document["operation_id"], "a" * 32)

    def test_qemu_viewer_capture_keeps_workspace_name_and_physical_display(self):
        shell = {
            "workspaces": [{"index": 2, "name": "Windows"}],
            "windows": [{
                "id": 91,
                "pid": 123,
                "app_id": "org.virt-manager.virt-viewer",
                "app_ids": ["org.virt-manager.virt-viewer"],
                "wm_class": "remote-viewer",
                **self.qemu_placement(),
            }],
        }
        with patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value=shell,
        ), patch(
            "workspace_state.shutdown_profiles._live_qemu_viewer",
            return_value=shutdown_profiles.ProcessIdentity(123, 456),
        ):
            placement = shutdown_profiles._capture_qemu_restore_placement(
                Path("/vm")
            )

        self.assertEqual(placement["workspace_name"], "Windows")
        self.assertEqual(
            placement["monitor_intent"]["edid_hash"],
            "physical-display",
        )

    def test_qemu_viewer_capture_retries_a_transient_shell_transition(self):
        hidden = {
            "available": True,
            "workspaces": [{"index": 2, "name": "Windows"}],
            "windows": [],
        }
        visible = {
            **hidden,
            "windows": [{
                "id": 91,
                "pid": 123,
                "app_id": "org.virt-manager.virt-viewer",
                "app_ids": ["org.virt-manager.virt-viewer"],
                "wm_class": "remote-viewer",
                **self.qemu_placement(),
            }],
        }
        with patch(
            "workspace_state.shutdown_profiles.capture_shell",
            side_effect=[hidden, visible],
        ), patch(
            "workspace_state.shutdown_profiles._live_qemu_viewer",
            return_value=shutdown_profiles.ProcessIdentity(123, 456),
        ), patch("workspace_state.shutdown_profiles.time.sleep") as sleep:
            placement = shutdown_profiles._capture_qemu_restore_placement(
                Path("/vm"), timeout=1,
            )

        self.assertEqual(placement["workspace_name"], "Windows")
        sleep.assert_called_once_with(0.25)

    def test_pre_hud_vm_capture_round_trips_operation_bound_state(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        qemu = shutdown_profiles.ProcessIdentity(123, 456)
        viewer = shutdown_profiles.ProcessIdentity(321, 654)
        with patch(
            "workspace_state.shutdown_profiles._live_qemu", return_value=qemu,
        ), patch(
            "workspace_state.shutdown_profiles._live_qemu_viewer",
            return_value=viewer,
        ), patch(
            "workspace_state.shutdown_profiles._capture_qemu_restore_placement",
            return_value=self.qemu_placement(),
        ), patch(
            "workspace_state.shutdown_profiles._same_process", return_value=True,
        ):
            shutdown_profiles.capture_shutdown_profile_preflight(
                [profile],
                operation_id="a" * 32,
                session_id="b" * 16,
                action="poweroff",
            )
            states = shutdown_profiles.load_shutdown_profile_preflight(
                [profile],
                operation_id="a" * 32,
                session_id="b" * 16,
                action="poweroff",
            )

        self.assertTrue(states["windows-vm"]["preflight_active"])
        self.assertEqual(states["windows-vm"]["preflight_qemu_identity"], qemu)
        self.assertEqual(states["windows-vm"]["preflight_viewer_identity"], viewer)
        self.assertEqual(
            states["windows-vm"]["restore_placement"]["workspace_name"],
            "Windows",
        )

    def test_qemu_probe_uses_pre_hud_placement_without_querying_gnome(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        qemu = shutdown_profiles.ProcessIdentity(123, 456)
        viewer = shutdown_profiles.ProcessIdentity(321, 654)
        runtime = shutdown_profiles.ProfileRuntime(profile, {
            "preflight_active": True,
            "preflight_qemu_identity": qemu,
            "preflight_viewer_identity": viewer,
            "restore_placement": self.qemu_placement(),
        })
        with patch(
            "workspace_state.shutdown_profiles._live_qemu", return_value=qemu,
        ), patch(
            "workspace_state.shutdown_profiles._same_process", return_value=True,
        ), patch(
            "workspace_state.shutdown_profiles._live_qemu_viewer",
            return_value=viewer,
        ), patch(
            "workspace_state.shutdown_profiles._qmp_status", return_value="running",
        ), patch(
            "workspace_state.shutdown_profiles._qga_ping",
        ), patch(
            "workspace_state.shutdown_profiles._capture_qemu_restore_placement",
        ) as capture:
            applicable, _message = (
                shutdown_profiles.QemuWindowsHibernateAdapter().probe(runtime)
            )

        self.assertTrue(applicable)
        capture.assert_not_called()

    def test_end_session_promotes_only_active_qemu_jobs_to_next_boot_receipt(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        runtime = shutdown_profiles.ProfileRuntime(
            profile,
            {"restore_placement": self.qemu_placement()},
        )
        shutdown_profiles._write_transaction(
            "a" * 32, "b" * 16, "poweroff", [runtime]
        )

        with patch(
            "workspace_state.shutdown_profiles._boot_id", return_value="boot-before",
        ):
            self.assertTrue(shutdown_profiles.disarm_transaction(
                "a" * 32,
                action="poweroff",
                session_id="b" * 16,
            ))

        self.assertFalse(shutdown_profiles.transaction_path().exists())
        receipt_path = shutdown_profiles.startup_restore_path()
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(receipt["source_boot_id"], "boot-before")
        self.assertEqual(receipt["entries"][0]["profile"]["id"], "windows-vm")
        self.assertEqual(
            receipt["entries"][0]["placement"]["workspace_name"],
            "Windows",
        )
        self.assertEqual(receipt_path.stat().st_mode & 0o777, 0o600)

    def test_committed_vm_restore_runs_once_per_boot_and_survives_reset(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        runtime = shutdown_profiles.ProfileRuntime(
            profile,
            {"restore_placement": self.qemu_placement()},
        )
        shutdown_profiles._write_transaction(
            "c" * 32, "d" * 16, "restart", [runtime]
        )
        with patch(
            "workspace_state.shutdown_profiles._boot_id", return_value="boot-before",
        ):
            self.assertTrue(shutdown_profiles.disarm_transaction(
                "c" * 32,
                action="restart",
                session_id="d" * 16,
            ))
            same_boot = shutdown_profiles.restore_startup_profiles()
        self.assertEqual(same_boot.total, 0)

        identity = shutdown_profiles.ProcessIdentity(321, 654)
        with patch(
            "workspace_state.shutdown_profiles._boot_id",
            side_effect=["boot-after", "boot-after", "boot-after-crash"],
        ), patch(
            "workspace_state.shutdown_profiles._resolved_qemu_placement",
            return_value=self.qemu_placement(),
        ), patch(
            "workspace_state.shutdown_profiles._live_qemu", return_value=None,
        ), patch(
            "workspace_state.shutdown_profiles._run_external", return_value=(0, "launched"),
        ) as launch, patch(
            "workspace_state.shutdown_profiles._wait_for_qemu_ready",
            return_value=identity,
        ), patch(
            "workspace_state.shutdown_profiles._ensure_qemu_viewer",
        ), patch(
            "workspace_state.shutdown_profiles._place_qemu_viewer",
        ) as place:
            restored = shutdown_profiles.restore_startup_profiles()
            repeated = shutdown_profiles.restore_startup_profiles()
            restored_after_crash = shutdown_profiles.restore_startup_profiles()

        self.assertEqual((restored.restored, restored.total), (1, 1))
        self.assertEqual(repeated.total, 0)
        self.assertEqual(
            (restored_after_crash.restored, restored_after_crash.total),
            (1, 1),
        )
        self.assertEqual(launch.call_count, 2)
        self.assertEqual(place.call_count, 2)
        receipt = json.loads(shutdown_profiles.startup_restore_path().read_text())
        self.assertEqual(receipt["restored_boot_id"], "boot-after-crash")

    def test_vm_restore_remaps_physical_display_and_workspace_before_launch(self):
        vm_directory = self.root / "vm"
        vm_directory.mkdir(mode=0o700)
        placement = self.qemu_placement()
        current = {
            "workspaces": [{"index": 5, "name": "Windows"}],
            "monitors": [{
                "index": 3,
                "connector": "DP-9",
                "x": 4000,
                "y": 100,
                "width": 1920,
                "height": 1080,
                "primary": False,
                "identity": {
                    "connector": "DP-9",
                    "edid_hash": "physical-display",
                    "serial": "DISPLAY-1",
                },
            }],
        }
        with patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value=current,
        ), patch(
            "workspace_state.desktop.capture_shell", return_value=current,
        ):
            target = shutdown_profiles._resolved_qemu_placement(
                vm_directory, placement,
            )

        self.assertEqual(target["workspace"], 5)
        self.assertEqual(target["monitor"], 3)
        viewer = json.loads((vm_directory / "viewer-placement.json").read_text())
        self.assertEqual(viewer["workspace"], 5)
        self.assertEqual(viewer["monitor"], "DP-9")
        self.assertEqual(viewer["geometry"], [0, 0, 1920, 1080])

    def test_vm_restore_refuses_an_ambiguous_workspace_name(self):
        vm_directory = self.root / "vm"
        vm_directory.mkdir(mode=0o700)
        current = {
            "workspaces": [
                {"index": 2, "name": "Windows"},
                {"index": 5, "name": "Windows"},
            ],
        }
        with patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value=current,
        ):
            with self.assertRaisesRegex(
                shutdown_profiles.ShutdownProfileError, "ambiguous",
            ):
                shutdown_profiles._resolved_qemu_placement(
                    vm_directory, self.qemu_placement(),
                )

    def test_empty_committed_shutdown_supersedes_an_old_vm_restore(self):
        shutdown_profiles.atomic_json(shutdown_profiles.startup_restore_path(), {
            "schema_version": 1,
            "operation_id": "a" * 32,
            "session_id": "b" * 16,
            "action": "poweroff",
            "source_boot_id": "old-boot",
            "committed_at": 1,
            "restored_boot_id": None,
            "entries": [{"invalid": "old intent"}],
        })
        with patch(
            "workspace_state.shutdown_profiles._boot_id", return_value="new-source-boot",
        ):
            self.assertTrue(shutdown_profiles.disarm_transaction(
                "e" * 32,
                action="poweroff",
                session_id="f" * 16,
            ))

        receipt = json.loads(shutdown_profiles.startup_restore_path().read_text())
        self.assertEqual(receipt["operation_id"], "e" * 32)
        self.assertEqual(receipt["entries"], [])

    def test_qemu_adapter_hibernates_only_the_verified_live_process(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        runtime = shutdown_profiles.ProfileRuntime(profile)
        identity = shutdown_profiles.ProcessIdentity(123, 456)
        cancellation = NeverCancelled()
        with patch(
            "workspace_state.shutdown_profiles._live_qemu", return_value=identity
        ), patch(
            "workspace_state.shutdown_profiles._qmp_status", return_value="running"
        ), patch(
            "workspace_state.shutdown_profiles._qga_ping"
        ), patch(
            "workspace_state.shutdown_profiles._capture_qemu_restore_placement",
            return_value=self.qemu_placement(),
        ), patch(
            "workspace_state.shutdown_profiles._qga_hibernate", return_value=77
        ) as hibernate, patch(
            "workspace_state.shutdown_profiles._qga_exec_status",
            return_value={"exited": True, "exitcode": 0},
        ), patch(
            "workspace_state.shutdown_profiles._same_process",
            side_effect=[True, False],
        ):
            adapter = shutdown_profiles.QemuWindowsHibernateAdapter()
            self.assertTrue(adapter.probe(runtime)[0])
            self.assertIn("hibernation", adapter.prepare(runtime, cancellation))

        hibernate.assert_called_once_with(Path("/vm"))
        self.assertEqual(runtime.state["hibernate_guest_pid"], 77)
        self.assertEqual(runtime.state["restore_placement"]["workspace_name"], "Windows")

    def test_qemu_adapter_rejects_a_failed_windows_hibernate_command(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        runtime = shutdown_profiles.ProfileRuntime(profile)
        runtime.state["original_identity"] = shutdown_profiles.ProcessIdentity(123, 456)
        with patch(
            "workspace_state.shutdown_profiles._same_process", return_value=True,
        ), patch(
            "workspace_state.shutdown_profiles._qga_hibernate", return_value=77,
        ), patch(
            "workspace_state.shutdown_profiles._qga_exec_status",
            return_value={"exited": True, "exitcode": 5},
        ):
            with self.assertRaisesRegex(
                shutdown_profiles.ShutdownProfileError, "exited with status 5"
            ):
                shutdown_profiles.QemuWindowsHibernateAdapter().prepare(
                    runtime, NeverCancelled()
                )

    def test_qemu_rollback_launches_and_verifies_guest_agent(self):
        profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1,
            "id": "windows-vm",
            "label": "Windows VM",
            "adapter": "qemu-windows-hibernate",
            "adapter_config": {"vm_directory": "/vm"},
        })
        runtime = shutdown_profiles.ProfileRuntime(profile)
        identity = shutdown_profiles.ProcessIdentity(321, 654)
        with patch(
            "workspace_state.shutdown_profiles._live_qemu",
            side_effect=[None, identity],
        ), patch(
            "workspace_state.shutdown_profiles._run_external",
            return_value=(0, "launched"),
        ) as launch, patch(
            "workspace_state.shutdown_profiles._qmp_status", return_value="running",
        ), patch(
            "workspace_state.shutdown_profiles._qga_ping",
        ), patch(
            "workspace_state.shutdown_profiles.time.monotonic", return_value=0,
        ):
            message = shutdown_profiles.QemuWindowsHibernateAdapter().rollback(runtime)

        self.assertIn("QEMU and QGA are ready", message)
        command = launch.call_args.args[0]
        self.assertEqual(command[0], "systemd-run")
        self.assertIn("--service-type=forking", command)
        self.assertIn("--property=PIDFile=/vm/run/qemu.pid", command)
        self.assertIn("--property=KillMode=control-group", command)
        self.assertEqual(command[-2:], ["--", "/vm/launch.sh"])
        self.assertFalse(any("PartOf=" in item or "--wait" == item for item in command))

    def test_qemu_launcher_failure_includes_service_diagnostics(self):
        with patch("workspace_state.shutdown_profiles._run_external", return_value=(1, "launch failed")):
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "journalctl --user -u wsctl-vm-"):
                shutdown_profiles._launch_qemu_runtime(Path("/vm"), label="restore", timeout=15)

    def test_existing_verified_viewer_is_not_duplicated(self):
        with patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value={"id": 42}), patch(
            "workspace_state.shutdown_profiles._run_external"
        ) as external:
            shutdown_profiles._ensure_qemu_viewer(Path("/vm"), timeout=15)
        external.assert_not_called()

    def test_qemu_placement_must_remain_stable_after_transient_match(self):
        target = self.qemu_placement()
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
        def window(_directory):
            state = "normal" if 0.5 <= clock[0] < 1 else target["state"]
            return {"id": 42, "workspace": target["workspace"],
                    "monitor": target["monitor"], "state": state}
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=window), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": target["workspace"]},
        ), patch("workspace_state.shutdown_profiles.move_window_result", return_value={"placed": True}):
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=10)
        self.assertEqual(result["state"], "maximized")
        self.assertGreaterEqual(clock[0], 3)

    def test_missing_viewer_uses_canonical_supervisor(self):
        with patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value=None), patch(
            "workspace_state.shutdown_profiles._run_external", return_value=(0, "42")
        ) as external:
            shutdown_profiles._ensure_qemu_viewer(Path("/vm"), timeout=15)
        self.assertEqual(external.call_args.args[0][:3], ["python3", "/vm/viewer_supervisor.py", "start"])

    def test_install_qemu_profile_is_private_and_reloadable(self):
        vm = self.root / "vm"
        for relative in (
            "launch.sh",
            "viewer_supervisor.py",
            "tools/qemu-system-x86_64-smb",
            "disk/windows10-22h2.qcow2",
        ):
            target = vm / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("test")
            target.chmod(0o700)
        path = shutdown_profiles.install_qemu_windows_profile(vm)

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        with patch.dict(
            os.environ, {"WSCTL_SHUTDOWN_PROFILE_DIRS": str(path.parent)}
        ):
            profile = shutdown_profiles.load_profiles()[0]
        self.assertEqual(profile.adapter, "qemu-windows-hibernate")
        self.assertEqual(profile.adapter_config["vm_directory"], str(vm.resolve()))


if __name__ == "__main__":
    unittest.main()
