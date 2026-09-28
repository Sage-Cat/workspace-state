import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, login_status, operations, shutdown_profiles
from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class VmRestoreNoopTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary.name,
                                              "XDG_STATE_HOME": str(Path(temporary.name) / "state")})
        environment.start()
        self.addCleanup(environment.stop)
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        login_status.initialize("a" * 16)
        self.context = operations.current()
        self.outcome = shutdown_profiles.StartupProfileRestoreOutcome(
            0, 0, "VM restore was already completed for this OS boot", already_completed=True,
        )

    def ready(self, context=None):
        login_status.update_stage("virtual-machines", "ready", "VM guest and viewer verified", current=1, total=1)
        path = cli._startup_marker("virtual-machines")
        write_stage_marker(path, StageMarker("virtual-machines", "ready", message="Verified VM",
                                             operation_context=(context or self.context).to_dict()))
        return path

    def test_receipt_noop_does_not_probe_reopen_or_reposition_a_closed_vm(self):
        document = {"source_boot_id": "previous", "restored_boot_id": "current"}
        with patch.object(shutdown_profiles, "_boot_id", return_value="current"), patch.object(
            shutdown_profiles, "_read_startup_restore", return_value=(document, [object()]),
        ), patch.object(shutdown_profiles, "_live_qemu") as probe, patch.object(
            shutdown_profiles, "_launch_qemu_runtime",
        ) as launch, patch.object(shutdown_profiles, "_ensure_qemu_viewer") as viewer, patch.object(
            shutdown_profiles, "_resolved_qemu_placement",
        ) as placement, patch.object(shutdown_profiles, "update_stage") as update:
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertTrue(outcome.already_completed)
        self.assertEqual((outcome.restored, outcome.total), (0, 0))
        for action in (probe, launch, viewer, placement, update):
            action.assert_not_called()

    def test_manual_completed_noop_succeeds_without_provisional_running_stage(self):
        args = cli.parser().parse_args(["restore", "virtual-machines"])
        args.login_status = True
        with patch.object(cli, "load", return_value={}), patch.object(
            cli, "restore_startup_profiles", return_value=self.outcome,
        ), patch.object(cli, "update_stage") as update:
            self.assertEqual(cli.cmd_restore(args), 0)
        update.assert_not_called()

    def test_completed_noop_preserves_verified_same_login_stage_and_marker_bytes(self):
        marker = self.ready()
        status_before, marker_before = login_status.status_path().read_bytes(), marker.read_bytes()
        state = cli._publish_category_outcome("virtual-machines", {}, already_completed=True)
        self.assertEqual(state, "ready")
        self.assertEqual(login_status.status_path().read_bytes(), status_before)
        self.assertEqual(marker.read_bytes(), marker_before)

    def test_completed_noop_without_same_login_proof_is_degraded_without_counts(self):
        for foreign in (False, True):
            with self.subTest(foreign=foreign):
                other = operations.OperationContext.create("b" * 16, "startup")
                marker = self.ready(other if foreign else None)
                if not foreign:
                    marker.unlink()
                state = cli._publish_category_outcome("virtual-machines", {}, already_completed=True)
                self.assertEqual(state, "degraded")
                document = json.loads(login_status.status_path().read_text())
                stage = next(item for item in document["stages"] if item["id"] == "virtual-machines")
                self.assertEqual(stage["state"], "degraded")
                self.assertIn("previously completed this boot; details unavailable", stage["message"])
                self.assertNotIn("current", stage)
                self.assertNotIn("total", stage)
                self.assertFalse(read_stage_marker(marker, "virtual-machines").verified)

    def test_startup_noop_does_not_fall_through_to_zero_count_publication(self):
        marker = self.ready()
        stage_before = next(item for item in json.loads(login_status.status_path().read_text())["stages"]
                            if item["id"] == "virtual-machines")
        marker_before = marker.read_bytes()
        args = cli.parser().parse_args(["startup", "virtual-machines", "--force"])
        with patch.object(cli, "load", return_value={}), patch.object(
            cli, "restore_startup_profiles", return_value=self.outcome,
        ), patch.object(cli, "_wait_for_tmux_restore", return_value=True), patch.object(
            cli, "_wait_for_shell",
        ), patch.object(cli, "_publish_workspace_attempt_completion"), patch.object(
            cli, "_publish_workspace_restored",
        ), patch.object(cli, "_arm_autosave_if_startup_complete"):
            self.assertEqual(cli.cmd_startup(args), 0)
        stage_after = next(item for item in json.loads(login_status.status_path().read_text())["stages"]
                           if item["id"] == "virtual-machines")
        self.assertEqual(stage_after, stage_before)
        self.assertEqual(marker.read_bytes(), marker_before)


if __name__ == "__main__":
    unittest.main()
