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
        self.assertEqual(launch.call_args.args[0], ["/vm/launch.sh"])

    def test_install_qemu_profile_is_private_and_reloadable(self):
        vm = self.root / "vm"
        for relative in (
            "launch.sh",
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
