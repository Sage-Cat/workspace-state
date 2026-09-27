from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack
from unittest.mock import Mock, patch

from workspace_state import cli, login_finalize, login_status

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class CodexStartupStatusTests(unittest.TestCase):
    def setUp(self):
        waiting = patch.object(cli, "waiting_directory_ids", return_value=set())
        self.waiting = waiting.start()
        self.addCleanup(waiting.stop)

    def test_repeated_startup_preserves_codex_outcome_and_counts(self):
        for state in ("degraded", "failed", "ready", "skipped"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
            ), patch("workspace_state.cli._boot_id", return_value="test-boot"), patch(
                "workspace_state.cli.load", return_value={"sessions": []},
            ), patch("workspace_state.cli._publish_workspace_restored"), patch(
                "workspace_state.cli._arm_autosave_if_startup_complete",
            ), patch("workspace_state.cli._restore") as restore:
                login_status.initialize("test-login")
                for identifier, _label in login_status.DEFAULT_STAGES:
                    login_status.update_stage(identifier, "ready", "Ready", current=1, total=1)
                login_status.update_stage(
                    "codex", state, "Prior verification outcome", current=4, total=19,
                    error="database is locked" if state in {"failed", "degraded"} else None,
                )
                before = json.loads(login_status.status_path().read_text())
                prior_codex = next(stage for stage in before["stages"] if stage["id"] == "codex")
                cli._startup_marker("terminals").write_text("saved-snapshot\n")

                result = cli.cmd_startup(Namespace(
                    category="terminals", dry_run=False, force=False,
                    owns_tmux_restore=True,
                ))

                after = json.loads(login_status.status_path().read_text())
                codex = next(stage for stage in after["stages"] if stage["id"] == "codex")
                self.assertEqual(result, 0)
                self.assertEqual(codex, prior_codex)
                self.assertEqual(after["overall_state"], state if state in {"failed", "degraded"} else "ready")
                restore.assert_not_called()

    def test_missing_codex_timeout_reports_unverified_panes(self):
        snapshot = {"sessions": [{
            "name": "work", "launch_terminal": False,
            "windows": [{"index": 1, "panes": [{
                "index": 1, "codex": {"session_id": "saved-id"},
            }]}],
        }], "terminals": []}
        args = Namespace(
            workspace=None, session=None, select=False, dry_run=False,
            no_place=False, repair_processes=False, adopt_restored=True,
            verify_codex=True, wait=120, login_status=True,
        )
        with patch("workspace_state.cli._live_terminal_clients", return_value={}), patch(
            "workspace_state.cli.recreate_tmux", return_value=("work", []),
        ), patch("workspace_state.cli.missing_codex_ids", return_value={"saved-id"}), patch(
            "workspace_state.cli.time.monotonic", side_effect=[100.0, 115.0],
        ), patch("workspace_state.cli.update_stage") as update:
            outcome = cli._restore_terminals(snapshot, args)

        self.assertFalse(outcome.codex_verified)
        self.assertFalse(outcome.codex_deferred)
        self.assertEqual(outcome.codex_ready, 0)
        report = next(call for call in update.call_args_list if call.args[:2] == ("codex", "degraded"))
        self.assertIn("could not be verified", report.args[2])
        self.assertIn("startup errors or prompts", report.args[2])
        self.assertNotIn("still starting", report.args[2])


class DeferredCodexTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.status = root / "status.json"
        self.status.write_text(json.dumps({"stages": [{"id": "codex", "state": "degraded"}]}))
        self.stack.enter_context(patch.object(cli, "status_path", return_value=self.status))
        self.snapshot = {"sessions": [{
            "name": "work", "launch_terminal": False,
            "windows": [{"index": 1, "panes": [
                {"index": 1, "codex": {"session_id": "local-id"}},
                {"index": 2, "codex": {"session_id": "cloud-id"}},
            ]}],
        }], "terminals": []}
        self.load = self.stack.enter_context(patch.object(cli, "load", return_value=self.snapshot))
        self.missing = self.stack.enter_context(patch.object(cli, "missing_codex_ids", return_value={"cloud-id"}))
        self.pending = self.stack.enter_context(patch.object(cli, "pending_start_ids", return_value=set()))
        self.waiting = self.stack.enter_context(patch.object(cli, "waiting_directory_ids", return_value=set()))
        self.update = self.stack.enter_context(patch.object(cli, "update_stage"))
        self.arm = self.stack.enter_context(patch.object(cli, "_arm_autosave_if_startup_complete"))
        self.sleep = self.stack.enter_context(patch.object(cli.time, "sleep"))
        self.launch = self.stack.enter_context(patch.object(cli, "launch_terminal"))
        self.recreate = self.stack.enter_context(patch.object(cli, "recreate_tmux", return_value=("work", [])))
        self.stack.enter_context(patch.object(cli, "_live_terminal_clients", return_value={}))

    def test_directory_wait_defers_verification_before_mounts_without_sleep(self):
        self.waiting.return_value = {"cloud-id"}
        args = Namespace(workspace=None, session=None, select=False, dry_run=False,
                         no_place=False, repair_processes=False, adopt_restored=True,
                         verify_codex=True, wait=120, login_status=True)
        outcome = cli._restore_terminals(self.snapshot, args)
        self.assertFalse(outcome.codex_verified)
        self.assertTrue(outcome.codex_deferred)
        self.assertEqual((outcome.codex_ready, outcome.codex_total), (1, 2))
        self.missing.assert_called_once()
        self.sleep.assert_not_called()
        report = self.update.call_args
        self.assertEqual(report.args[:2], ("codex", "waiting"))
        self.assertIn("deferred", report.args[2])
        self.assertEqual(report.kwargs, {"current": 1, "total": 2})

    def test_startup_completion_keeps_known_directory_deferral_waiting(self):
        self.waiting.return_value = {"cloud-id"}
        args = Namespace(category="terminals", workspace=None, session=None, select=False,
                         dry_run=False, no_place=False, repair_processes=False, adopt_restored=True,
                         verify_codex=True, wait=120, login_status=True, force=False,
                         owns_tmux_restore=True)
        with patch.object(cli, "_startup_directory", return_value=self.status.parent), patch.object(
            cli, "_wait_for_shell",
        ), patch.object(cli, "_publish_workspace_restored"), patch.object(cli, "set_overall"):
            self.assertEqual(cli.cmd_startup(args), 0)
        codex_reports = [call for call in self.update.call_args_list if call.args[0] == "codex"]
        self.assertEqual(codex_reports[-1].args[:2], ("codex", "waiting"))
        self.assertEqual(codex_reports[-1].kwargs, {"current": 1, "total": 2})
        self.assertNotIn("startup errors", codex_reports[-1].args[2])
        self.assertNotIn("degraded", [call.args[1] for call in codex_reports])

    def test_unrelated_missing_session_does_not_take_directory_wait_shortcut(self):
        self.waiting.return_value = {"unrelated-id"}
        args = Namespace(workspace=None, session=None, select=False, dry_run=False,
                         no_place=False, repair_processes=False, adopt_restored=True,
                         verify_codex=True, wait=120, login_status=True)
        with patch.object(cli.time, "monotonic", side_effect=[100, 100, 115]):
            cli._restore_terminals(self.snapshot, args)
        self.assertEqual(self.missing.call_count, 2)
        self.sleep.assert_called_once_with(0.5)

    def test_finalizer_waits_for_pending_wrapper_and_rechecks_after_mount(self):
        self.pending.return_value = {"cloud-id"}
        self.missing.side_effect = [{"cloud-id"}, set(), set()]
        with patch.object(cli.time, "monotonic", side_effect=[100, 100, 101, 104]):
            self.assertTrue(cli.finish_deferred_codex())
        self.assertEqual(self.missing.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.update.assert_called_once_with("codex", "ready", "Resumed 2 Codex conversation(s)", current=2, total=2)
        self.arm.assert_called_once_with()
        self.launch.assert_not_called()
        self.recreate.assert_not_called()

    def test_initially_present_sessions_must_remain_live_for_stability_interval(self):
        self.missing.return_value = set()
        with patch.object(cli.time, "monotonic", side_effect=[100, 100, 102.9, 103]):
            self.assertTrue(cli.finish_deferred_codex())
        self.assertEqual(self.missing.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.update.call_args.args[:2], ("codex", "ready"))

    def test_session_disappearing_before_stability_remains_degraded(self):
        self.missing.side_effect = [set(), {"cloud-id"}, set(), set()]
        with patch.object(cli.time, "monotonic", side_effect=[100, 100, 102, 129, 130]):
            self.assertFalse(cli.finish_deferred_codex())
        self.assertEqual(self.update.call_args.args[:2], ("codex", "degraded"))
        self.assertIn("stability verification timed out", self.update.call_args.args[2])
        self.assertEqual(self.update.call_args.kwargs, {"current": 2, "total": 2})
        self.arm.assert_not_called()

    def test_finalizer_pending_wait_is_bounded_and_counts_remain_accurate(self):
        self.pending.return_value = {"cloud-id"}
        with patch.object(cli.time, "monotonic", side_effect=[100, 129.5, 130]):
            self.assertFalse(cli.finish_deferred_codex())
        self.assertEqual(self.missing.call_count, 2)
        self.sleep.assert_called_once_with(0.5)
        self.assertEqual(self.update.call_args.args[:2], ("codex", "degraded"))
        self.assertEqual(self.update.call_args.kwargs, {"current": 1, "total": 2})

    def test_finalizer_without_pending_wrapper_performs_only_one_recheck(self):
        self.assertFalse(cli.finish_deferred_codex())
        self.missing.assert_called_once_with(self.snapshot["sessions"][0], "work")
        self.sleep.assert_not_called()
        self.launch.assert_not_called()
        self.recreate.assert_not_called()

    def test_finalizer_preserves_prior_failed_stage(self):
        self.status.write_text(json.dumps({"stages": [{"id": "codex", "state": "failed", "error": "restore failed"}]}))
        self.assertFalse(cli.finish_deferred_codex())
        self.load.assert_not_called()
        self.missing.assert_not_called()
        self.update.assert_not_called()
        self.arm.assert_not_called()

    def test_login_rechecks_codex_after_drives_and_deferred_apps(self):
        order = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(login_finalize, "_check_operation"))
            stack.enter_context(patch.object(login_finalize, "_failed_startup_stages", return_value=[]))
            for name in ("_start_drives", "finish_deferred_file_manager", "finish_deferred_vscode",
                         "finish_deferred_codex", "_warm_cloud_metadata"):
                mocked = stack.enter_context(patch.object(login_finalize, name, return_value=True))
                order.attach_mock(mocked, name)
            login_finalize._start_drives.return_value = {"gdrive": True}
            for name in ("set_overall", "finish", "update_stage", "append_diagnostic", "fail_active"):
                stack.enter_context(patch.object(login_finalize, name))
            self.assertEqual(login_finalize._finalize(), 0)
        self.assertEqual([item[0] for item in order.mock_calls], [
            "_start_drives", "finish_deferred_file_manager", "finish_deferred_vscode",
            "finish_deferred_codex", "_warm_cloud_metadata",
        ])


class CodexAutosaveGateTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.status = self.root / "status.json"
        self.autosave = self.root / "autosave.ready"
        self.stack.enter_context(patch.object(cli, "status_path", return_value=self.status))
        self.stack.enter_context(patch.object(cli, "_autosave_marker", return_value=self.autosave))
        self.stack.enter_context(patch.object(cli, "_startup_marker", side_effect=lambda name: self.root / name))
        from workspace_state import operations
        self.context = operations.OperationContext.create("autosave-gate", "startup")
        self.stack.enter_context(patch.object(operations, "current", return_value=self.context))
        for category in cli.CATEGORIES:
            write_stage_marker(self.root / category, StageMarker(category, "ready", operation_context=self.context.to_dict()))

    def set_codex_state(self, state):
        self.status.write_text(json.dumps({
            "mode": "startup", "session_id": self.context.login_generation,
            "operation_id": self.context.operation_id, "operation_context": self.context.to_dict(),
            "stages": [{"id": "codex", "state": state}],
        }))

    def test_unresolved_codex_blocks_automatic_save_despite_complete_categories(self):
        for state in ("failed", "degraded", "running", "waiting", "pending"):
            with self.subTest(state=state):
                self.set_codex_state(state)
                cli._arm_autosave_if_startup_complete()
                self.assertFalse(self.autosave.exists())

    def test_ready_or_skipped_codex_reenables_automatic_save(self):
        for state in ("ready", "skipped"):
            with self.subTest(state=state):
                self.set_codex_state(state)
                cli._arm_autosave_if_startup_complete()
                self.assertEqual(self.autosave.read_text(), "ready\n")
                self.autosave.unlink()

    def test_missing_status_cannot_authorize_verified_markers_for_unknown_attempt(self):
        cli._arm_autosave_if_startup_complete()
        self.assertFalse(self.autosave.exists())

    def test_ready_codex_still_requires_all_category_markers(self):
        self.set_codex_state("ready")
        (self.root / cli.CATEGORIES[-1]).unlink()
        cli._arm_autosave_if_startup_complete()
        self.assertFalse(self.autosave.exists())


if __name__ == "__main__":
    unittest.main()
