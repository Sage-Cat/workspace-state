from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from workspace_state import profile_test, shutdown_profiles as profiles


class IsolatedProfileTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.profile = profiles._profile_from_mapping({
            "schema_version": 1, "id": "windows-vm", "label": "Windows",
            "adapter": "qemu-windows-hibernate", "adapter_config": {"vm_directory": "/vm"},
        })
        self.placement = {"workspace_name": "PhD", "monitor_identity": {"serial": "test"},
                          "state": "maximized", "geometry_relative": {"width": 1920}}
        self.adapter = self.stack.enter_context(patch.object(profiles, "QemuWindowsHibernateAdapter"))
        self.adapter.return_value.probe.side_effect = self.probe
        self.stack.enter_context(patch.object(profiles, "load_profiles", return_value=[self.profile]))
        self.stack.enter_context(patch.object(profiles, "state_root", return_value=self.root))
        self.stack.enter_context(patch.object(profiles, "transaction_path", return_value=self.root / "host-transaction"))
        self.stack.enter_context(patch.object(profiles, "_live_qemu", return_value=profiles.ProcessIdentity(10, 20)))
        self.stack.enter_context(patch.object(profiles, "_ensure_qemu_viewer"))
        self.stack.enter_context(patch.object(profiles, "_qemu_viewer_window", return_value={"id": 1}))
        saved = profiles.ProfileRuntime(self.profile, {"restore_placement": self.placement})
        self.stack.enter_context(patch.object(profiles, "_read_startup_restore", return_value=({}, [saved])))
        self.stack.enter_context(patch.object(profiles, "_capture_qemu_restore_placement", return_value=self.placement))
        self.adapter.return_value.prepare.return_value = "hibernated"
        self.adapter.return_value.verify.return_value = "inactive"
        self.adapter.return_value.rollback.return_value = "restored"

    def probe(self, runtime):
        runtime.state.update(restore_placement=self.placement, original_identity=profiles.ProcessIdentity(10, 20))
        return True, "ready"

    def test_round_trip_does_not_touch_host_transactions(self):
        with patch.object(profiles, "_write_transaction") as write, patch.object(profiles, "disarm_transaction") as commit:
            report = profile_test.run_profile_test("windows-vm")
        write.assert_not_called()
        commit.assert_not_called()
        self.assertEqual(json.loads(report.read_text())["state"], "ready")
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)
        self.adapter.return_value.prepare.assert_called_once()
        self.adapter.return_value.rollback.assert_called_once()

    def test_failed_hibernation_is_recovered_but_still_fails_test(self):
        self.adapter.return_value.prepare.side_effect = profiles.ShutdownProfileError("guest failed")
        with self.assertRaisesRegex(profiles.ShutdownProfileError, "guest failed"):
            profile_test.run_profile_test("windows-vm")
        self.adapter.return_value.rollback.assert_called_once()
        report = next((self.root / "profile-tests").glob("*.json"))
        self.assertEqual(json.loads(report.read_text())["state"], "failed")

    def test_restore_only_never_hibernates(self):
        profile_test.run_profile_test("windows-vm", restore_only=True)
        self.adapter.return_value.prepare.assert_not_called()
        self.adapter.return_value.rollback.assert_called_once()

    def test_missing_viewer_does_not_capture_unplaced_temporary_window(self):
        with patch.object(profiles, "_qemu_viewer_window", return_value=None):
            with self.assertRaisesRegex(profiles.ShutdownProfileError, "restore-only"):
                profile_test.run_profile_test("windows-vm")
        self.adapter.return_value.prepare.assert_not_called()

    def test_active_host_transaction_blocks_test(self):
        (self.root / "host-transaction").touch()
        with self.assertRaisesRegex(profiles.ShutdownProfileError, "transaction"):
            profile_test.run_profile_test("windows-vm")
        self.adapter.return_value.prepare.assert_not_called()

    def test_cancel_during_hibernation_recovers_before_returning(self):
        def prepare(_runtime, cancel):
            cancel.handle()
            return "hibernated"
        self.adapter.return_value.prepare.side_effect = prepare
        with self.assertRaises(profiles.ShutdownProfilesCancelled):
            profile_test.run_profile_test("windows-vm")
        self.adapter.return_value.rollback.assert_called_once()
