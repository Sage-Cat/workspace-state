import json
import os
from contextlib import ExitStack, contextmanager
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
        self.target = {"workspace": 2, "monitor": 1, "state": "normal",
                       "geometry": {"x": 1920, "y": 0, "width": 800, "height": 600}}
        self.profile = shutdown_profiles._profile_from_mapping({
            "schema_version": 1, "id": "windows-vm", "label": "Windows VM",
            "adapter": "qemu-windows-hibernate", "adapter_config": {"vm_directory": "/vm"},
        })
        self.runtime = shutdown_profiles.ProfileRuntime(self.profile, {"restore_placement": self.target})
        self.document = {"source_boot_id": "previous", "restored_boot_id": "current",
                         "restored_login_generation": "a" * 16}

    @contextmanager
    def completed_receipt(self, *, generation="a" * 16):
        identity = shutdown_profiles.ProcessIdentity(123, 456)
        values = {
            "_boot_id": "current", "_current_restore_generation": generation,
            "_read_startup_restore": (self.document, [self.runtime]),
            "_live_qemu": identity, "_qmp_status": "running", "_qga_ping": None,
            "_resolved_qemu_placement": self.target, "_qemu_viewer_window": dict(self.target, id=42),
            "_same_process": True, "_wait_for_qemu_ready": identity,
            "_launch_qemu_runtime": None, "_ensure_qemu_viewer": None, "_place_qemu_viewer": None,
            "atomic_json": None, "update_stage": None,
        }
        with ExitStack() as stack:
            yield {name: stack.enter_context(patch.object(shutdown_profiles, name, return_value=value))
                   for name, value in values.items()}

    def assert_no_restore(self, calls):
        for name in ("_launch_qemu_runtime", "_ensure_qemu_viewer", "_place_qemu_viewer",
                     "_wait_for_qemu_ready", "update_stage"):
            calls[name].assert_not_called()

    def ready(self, context=None):
        login_status.update_stage("virtual-machines", "ready", "VM guest and viewer verified", current=1, total=1)
        path = cli._startup_marker("virtual-machines")
        write_stage_marker(path, StageMarker("virtual-machines", "ready", message="Verified VM",
                                             operation_context=(context or self.context).to_dict()))
        return path

    def test_receipt_noop_verifies_guest_and_viewer_without_mutation(self):
        with self.completed_receipt() as calls:
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertTrue(outcome.already_completed)
        self.assertEqual((outcome.restored, outcome.total), (0, 0))
        for name in ("_live_qemu", "_qmp_status", "_qga_ping", "_qemu_viewer_window", "_same_process"):
            calls[name].assert_called_once()
        calls["_resolved_qemu_placement"].assert_called_once_with(Path("/vm"), self.target, persist=False)
        calls["atomic_json"].assert_not_called()
        self.assert_no_restore(calls)

    def test_same_login_missing_or_unverified_runtime_is_not_reopened(self):
        for name, value in (("_live_qemu", None), ("_qemu_viewer_window", None),
                            ("_qmp_status", "paused"), ("_same_process", False),
                            ("_qemu_viewer_window", dict(self.target, workspace=0))):
            with self.subTest(probe=name, value=value), self.completed_receipt() as calls:
                calls[name].return_value = value
                with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "no longer verified.*explicit"):
                    shutdown_profiles.restore_startup_profiles()
                self.assert_no_restore(calls)
                calls["atomic_json"].assert_not_called()

    def test_unresponsive_guest_agent_invalidates_completed_receipt(self):
        with self.completed_receipt() as calls:
            calls["_qga_ping"].side_effect = shutdown_profiles.ShutdownProfileError("guest agent unavailable")
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "guest agent unavailable"):
                shutdown_profiles.restore_startup_profiles()
        self.assert_no_restore(calls)

    def test_explicit_restore_reconciles_missing_guest_in_same_login(self):
        with self.completed_receipt() as calls:
            calls["_live_qemu"].return_value = None
            outcome = shutdown_profiles.restore_startup_profiles(restore_completed=True)
        self.assertEqual((outcome.restored, outcome.total), (1, 1))
        self.assertFalse(outcome.already_completed)
        calls["_launch_qemu_runtime"].assert_called_once()
        calls["_place_qemu_viewer"].assert_called_once()
        self.assertEqual(self.document["restored_login_generation"], "a" * 16)

    def test_new_login_restores_saved_intent_when_viewer_is_missing(self):
        with self.completed_receipt(generation="b" * 16) as calls:
            calls["_qemu_viewer_window"].return_value = None
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertEqual((outcome.restored, outcome.total), (1, 1))
        calls["_launch_qemu_runtime"].assert_not_called()
        calls["_ensure_qemu_viewer"].assert_called_once()
        calls["_place_qemu_viewer"].assert_called_once()
        self.assertEqual(self.document["restored_login_generation"], "b" * 16)

    def test_new_login_launches_missing_guest_from_saved_intent(self):
        with self.completed_receipt(generation="b" * 16) as calls:
            calls["_live_qemu"].return_value = None
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertEqual((outcome.restored, outcome.total), (1, 1))
        calls["_launch_qemu_runtime"].assert_called_once()
        calls["_ensure_qemu_viewer"].assert_called_once()
        self.assertEqual(self.document["restored_login_generation"], "b" * 16)

    def test_new_login_counts_live_verified_guest_without_repositioning(self):
        with self.completed_receipt(generation="b" * 16) as calls:
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertEqual((outcome.restored, outcome.total), (1, 1))
        self.assertFalse(outcome.already_completed)
        self.assert_no_restore(calls)
        calls["atomic_json"].assert_called_once()
        self.assertEqual(self.document["restored_login_generation"], "b" * 16)

    def test_legacy_receipt_cannot_guess_a_new_login_to_reopen_stopped_guest(self):
        self.document.pop("restored_login_generation")
        with self.completed_receipt(generation="b" * 16) as calls:
            calls["_live_qemu"].return_value = None
            with self.assertRaisesRegex(shutdown_profiles.ShutdownProfileError, "explicit"):
                shutdown_profiles.restore_startup_profiles()
        self.assert_no_restore(calls)

    def test_legacy_live_receipt_is_verified_for_current_login(self):
        self.document.pop("restored_login_generation")
        with self.completed_receipt() as calls:
            outcome = shutdown_profiles.restore_startup_profiles()
        self.assertEqual((outcome.restored, outcome.total), (1, 1))
        self.assertFalse(outcome.already_completed)
        self.assert_no_restore(calls)
        self.assertEqual(self.document["restored_login_generation"], "a" * 16)

    def test_explicit_completed_dry_run_does_not_reopen_guest(self):
        with self.completed_receipt() as calls:
            calls["_live_qemu"].return_value = None
            outcome = shutdown_profiles.restore_startup_profiles(dry_run=True, restore_completed=True)
        self.assertIn("Would restore", outcome.message)
        self.assert_no_restore(calls)
        calls["atomic_json"].assert_not_called()

    def test_generation_rejects_foreign_boot_and_inherited_operation(self):
        from workspace_state import startup
        boot = self.context.boot_id
        for runtime_boot, generation in (("previous", "a" * 16), (boot, "b" * 16), (boot, None), (boot, "abc")):
            with self.subTest(boot=runtime_boot, generation=generation), patch.object(
                startup, "runtime_identity", return_value=(Path("/run/example"), runtime_boot, generation),
            ):
                self.assertIsNone(shutdown_profiles._current_restore_generation(boot))
        with patch.object(startup, "runtime_identity", return_value=(Path("/run/example"), boot, "a" * 16)):
            self.assertEqual(shutdown_profiles._current_restore_generation(boot), "a" * 16)

    def test_manual_completed_noop_succeeds_without_provisional_running_stage(self):
        args = cli.parser().parse_args(["restore", "virtual-machines"])
        args.login_status = True
        with patch.object(cli, "load", return_value={}), patch.object(
            cli, "restore_startup_profiles", return_value=self.outcome,
        ) as restore, patch.object(cli, "update_stage") as update:
            self.assertEqual(cli.cmd_restore(args), 0)
        update.assert_not_called()
        restore.assert_called_once_with(dry_run=False, restore_completed=True)

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
        ) as restore, patch.object(cli, "_wait_for_tmux_restore", return_value=True), patch.object(
            cli, "_wait_for_shell",
        ), patch.object(cli, "_publish_workspace_attempt_completion"), patch.object(
            cli, "_publish_workspace_restored",
        ), patch.object(cli, "_arm_autosave_if_startup_complete"):
            self.assertEqual(cli.cmd_startup(args), 0)
        stage_after = next(item for item in json.loads(login_status.status_path().read_text())["stages"]
                           if item["id"] == "virtual-machines")
        self.assertEqual(stage_after, stage_before)
        self.assertEqual(marker.read_bytes(), marker_before)
        restore.assert_called_once_with(dry_run=False, restore_completed=True)

    def test_automatic_restore_does_not_explicitly_replay_completed_vm(self):
        args = cli.parser().parse_args(["startup", "virtual-machines"])
        with patch.object(cli, "restore_startup_profiles", return_value=self.outcome) as restore:
            cli._restore({}, args, startup=True)
        restore.assert_called_once_with(dry_run=False, restore_completed=False)


if __name__ == "__main__":
    unittest.main()
