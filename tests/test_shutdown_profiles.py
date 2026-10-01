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
        ) as place, patch(
            "workspace_state.shutdown_profiles._verify_completed_qemu_restore",
        ) as verify:
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
        verify.assert_called_once()
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
            observed_target = shutdown_profiles._resolved_qemu_placement(
                vm_directory, placement, persist=False,
            )
            self.assertFalse((vm_directory / "viewer-placement.json").exists())
            target = shutdown_profiles._resolved_qemu_placement(
                vm_directory, placement,
            )

        self.assertEqual(observed_target, target)
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

    def test_completed_receipt_generation_is_optional_but_validated(self):
        receipt = {
            "schema_version": 1, "operation_id": "a" * 32, "session_id": "b" * 16,
            "action": "poweroff", "source_boot_id": "previous", "committed_at": 1,
            "restored_boot_id": "current", "restored_at": 2, "entries": [],
        }
        for generation in (None, "c" * 16, "foreign-session", "abc", 5):
            with self.subTest(generation=generation):
                document = dict(receipt, restored_login_generation=generation)
                shutdown_profiles.atomic_json(shutdown_profiles.startup_restore_path(), document)
                if generation in (None, "c" * 16):
                    self.assertEqual(shutdown_profiles._read_startup_restore()[0], document)
                else:
                    with self.assertRaises(shutdown_profiles.ShutdownProfileError):
                        shutdown_profiles._read_startup_restore()

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
                    "monitor": target["monitor"], "state": state,
                    "geometry": target["geometry"], "monitor_geometry": target["monitor_geometry"]}
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=window), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": target["workspace"]},
        ), patch("workspace_state.shutdown_profiles.move_window_result", return_value={"placed": True}):
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=10)
        self.assertEqual(result["state"], "maximized")
        self.assertGreaterEqual(clock[0], 3)

    def test_qemu_async_placement_requires_observed_stable_target(self):
        target = self.qemu_placement()
        for response in ({"ok": True, "placed": False, "status": "applied", "token": "request"},
                         {"ok": True, "placed": False, "status": "deferred", "token": "request"}):
            with self.subTest(response=response):
                clock = [0.0]
                desired = dict(target)
                desired["workspace"] += 1
                def sleep(seconds):
                    clock[0] += seconds
                def window(_directory):
                    return {"id": 42, "workspace": desired["workspace"],
                            "monitor": target["monitor"] if clock[0] >= 1 else -1,
                            "state": target["state"], "geometry": target["geometry"],
                            "monitor_geometry": target["monitor_geometry"]}
                def move(_window_id, destination):
                    desired.update(destination)
                    return response
                with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
                    "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
                ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=window), patch(
                    "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": target["workspace"] + 1},
                ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move) as moved:
                    result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=10)
                self.assertEqual(result["monitor"], target["monitor"])
                self.assertGreaterEqual(clock[0], 3)
                self.assertEqual(moved.call_count, 2)

    def test_qemu_maximized_resize_settles_before_inactive_handoff(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, geometry=dict(target["geometry"], width=3840, height=2030))
        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= 6:
                viewer["geometry"] = target["geometry"]
        def move(_window_id, destination):
            if destination["workspace"] == target["workspace"]:
                self.assertGreaterEqual(clock[0], 6.4)
                self.assertEqual(viewer["geometry"], target["geometry"])
            viewer["workspace"] = destination["workspace"]
            return {"placed": False, "status": "applied", "token": "resize"}
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=lambda _: dict(viewer)), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move) as moved:
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=12)
        self.assertEqual(result["workspace"], target["workspace"])
        self.assertGreaterEqual(clock[0], 8.4)
        self.assertEqual([call.args[1]["workspace"] for call in moved.call_args_list], [0, target["workspace"]])

    def test_qemu_pending_final_move_is_not_reissued_after_five_seconds(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value=viewer), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": target["workspace"]},
        ), patch("workspace_state.shutdown_profiles.move_window_result", return_value={"placed": False, "status": "applied"}) as moved:
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "placement did not settle"):
                shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=8)
        moved.assert_called_once()

    def test_qemu_supervisor_final_destination_completes_staging(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")
        def sleep(seconds):
            clock[0] += seconds
            # The supervisor supersedes staging with the requested final
            # destination; a transient match still needs two stable seconds.
            if clock[0] >= .5:
                viewer.update(target)
                if 1 <= clock[0] < 1.5:
                    viewer["state"] = "normal"
        def move(_window_id, destination):
            viewer["workspace"] = destination["workspace"]
            return {"placed": False, "status": "applied"}
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=lambda _: dict(viewer)), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move) as moved:
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)
        self.assertEqual(result["workspace"], target["workspace"])
        self.assertGreaterEqual(clock[0], 3.5)
        self.assertLess(clock[0], 4)
        moved.assert_called_once()
        self.assertEqual(moved.call_args.args[1]["workspace"], 0)

    def test_qemu_staging_failure_releases_gate_before_vm_startup_deadline(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value=viewer), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result", return_value={"placed": False, "status": "applied"}), patch(
            "workspace_state.shutdown_profiles.placement_lock",
        ) as gate:
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "staging did not settle"):
                shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)
        self.assertEqual(clock[0], 10)
        gate.release.assert_called_once()

    def test_qemu_cancelled_staging_recovers_supervisor_inactive_normal_window(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")
        requests = []
        cancelled = set()

        def sleep(seconds):
            clock[0] += seconds
            if len(requests) == 1:
                # The supervisor moves the still-normal window off the active
                # workspace; native maximization cannot finish there.
                cancelled.add("stage-1")
                viewer["workspace"] = target["workspace"]
            elif len(requests) == 2 and clock[0] >= requests[-1][0] + 0.5:
                viewer.update(requests[-1][1])

        def move(_window_id, destination):
            requests.append((clock[0], dict(destination)))
            viewer["workspace"] = destination["workspace"]
            return {"placed": False, "status": "applied", "token": f"stage-{len(requests)}"}

        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=sleep,
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=lambda _: dict(viewer)), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move), patch(
            "workspace_state.shutdown_profiles.expected_window_status",
            side_effect=lambda token: "cancelled" if token in cancelled else "applied",
        ):
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)

        self.assertEqual([destination["workspace"] for _, destination in requests], [0, 0, target["workspace"]])
        self.assertEqual(result["state"], target["state"])
        self.assertEqual(result["workspace"], target["workspace"])
        self.assertGreaterEqual(clock[0] - requests[-1][0], 2)
        self.assertLess(clock[0], 4)

    def test_qemu_staging_retries_only_confirmed_cancellation(self):
        target = self.qemu_placement()
        for status in ("accepted", "applied", "deferred", "failed", "unknown"):
            with self.subTest(status=status):
                clock = [0.0]
                with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
                    "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
                ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value=dict(target, id=42, state="normal")), patch(
                    "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
                ), patch("workspace_state.shutdown_profiles.move_window_result",
                         return_value={"placed": False, "status": "applied", "token": "staging"}) as moved, patch(
                    "workspace_state.shutdown_profiles.expected_window_status", return_value=status,
                ) as receipt:
                    with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "staging did not settle"):
                        shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)
                moved.assert_called_once()
                receipt.assert_called_with("staging")
                self.assertEqual(clock[0], 10)

    def test_qemu_repeated_staging_cancellation_keeps_original_gate_deadline(self):
        target = self.qemu_placement()
        clock = [0.0]
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", return_value=dict(target, id=42, state="normal")), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result",
                 return_value={"placed": False, "status": "applied", "token": "staging"}) as moved, patch(
            "workspace_state.shutdown_profiles.expected_window_status", return_value="cancelled",
        ), patch("workspace_state.shutdown_profiles.placement_lock") as gate:
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "staging did not settle"):
                shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)
        self.assertGreater(moved.call_count, 1)
        self.assertEqual(clock[0], 10)
        gate.release.assert_called_once()

    def test_qemu_cancelled_staging_does_not_disturb_stable_final_destination(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")

        def move(_window_id, _destination):
            viewer.update(target)
            return {"placed": False, "status": "applied", "token": "staging"}

        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=lambda _: dict(viewer)), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": 0},
        ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move) as moved, patch(
            "workspace_state.shutdown_profiles.expected_window_status", return_value="cancelled",
        ) as receipt:
            result = shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=180)
        self.assertEqual(result["workspace"], target["workspace"])
        self.assertEqual(clock[0], 2)
        moved.assert_called_once()
        receipt.assert_not_called()

    def test_qemu_window_discovery_and_final_verification_do_not_hold_gate(self):
        target = self.qemu_placement()
        clock = [0.0]
        viewer = dict(target, id=42, state="normal")
        held = [False]
        observed_outside_gate = []
        def window(_directory):
            if not held[0]:
                observed_outside_gate.append((clock[0], viewer["state"]))
            return None if clock[0] < 1 else dict(viewer)
        def acquire(**_kwargs):
            held[0] = True
            return True
        def move(_window_id, destination):
            self.assertTrue(held[0])
            viewer.update(destination)
            return {"placed": True}
        with patch("workspace_state.shutdown_profiles.time.monotonic", side_effect=lambda: clock[0]), patch(
            "workspace_state.shutdown_profiles.time.sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        ), patch("workspace_state.shutdown_profiles._qemu_viewer_window", side_effect=window), patch(
            "workspace_state.shutdown_profiles.capture_shell", return_value={"active_workspace": target["workspace"]},
        ), patch("workspace_state.shutdown_profiles.move_window_result", side_effect=move), patch(
            "workspace_state.shutdown_profiles.placement_lock",
        ) as gate:
            gate.acquire.side_effect = acquire
            gate.release.side_effect = lambda: held.__setitem__(0, False)
            shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=8)
        gate.acquire.assert_called_once_with(timeout=7)
        self.assertTrue(any(at < 1 for at, _ in observed_outside_gate))
        self.assertTrue(any(at >= 3 and state == "maximized" for at, state in observed_outside_gate))

    def test_qemu_busy_gate_does_not_outlive_operation_deadline(self):
        target = self.qemu_placement()
        with patch("workspace_state.shutdown_profiles.time.monotonic", return_value=0), patch(
            "workspace_state.shutdown_profiles._qemu_viewer_window", return_value=dict(target, id=42, state="normal"),
        ), patch("workspace_state.shutdown_profiles.placement_lock") as gate, patch(
            "workspace_state.shutdown_profiles.move_window_result",
        ) as move:
            gate.acquire.return_value = False
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "deadline elapsed while waiting"):
                shutdown_profiles._place_qemu_viewer(Path("/vm"), target, timeout=8)
        gate.acquire.assert_called_once_with(timeout=8)
        gate.release.assert_not_called()
        move.assert_not_called()

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
