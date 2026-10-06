from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class FileManagerCliTests(unittest.TestCase):
    def args(self, category="file-manager", **kwargs):
        values = dict(category=category, workspace=None, session=None, select=False,
                      dry_run=False, no_place=False, login_status=False, wait=0,
                      force=False, repair_processes=False, adopt_restored=False,
                      verify_codex=False)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def test_restore_routes_category_and_legacy_missing_key_is_successful(self):
        with patch.object(cli, "restore_file_manager", return_value=0) as restore:
            counts = cli._restore({"sessions": [], "desktop": {}}, self.args())
        self.assertEqual(counts["file-manager"], 0)
        restore.assert_called_once()
        self.assertIsNone(restore.call_args.args[0])

    def test_capture_records_file_manager_failure_without_losing_snapshot(self):
        with patch.object(cli, "capture", side_effect=lambda **kwargs: {
                 "sessions": [], "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), \
             patch.object(cli, "capture_browser", return_value={}), \
             patch.object(cli, "capture_shell", return_value={}), \
             patch.object(cli, "capture_social_apps", return_value={}), \
             patch.object(cli, "capture_file_manager", side_effect=RuntimeError("nemo unavailable")):
            snapshot = cli._capture_all()
        self.assertEqual(snapshot["sessions"], [])
        self.assertIn("nemo unavailable", snapshot["capture_errors"]["file_manager"])

    def test_tmux_autosave_preserves_file_manager_recipe(self):
        previous = {"sessions": [], "file_manager": {"provider": "nemo", "windows": []}}
        live = {"sessions": [], "terminals": [], "desktop": {"shell_companion": True}}
        with patch.object(cli, "load", return_value=previous), \
             patch.object(cli, "capture", return_value=live), \
             patch.object(cli, "save", return_value="saved") as save, \
             patch.object(cli, "_terminal_problems", return_value=[]), \
             patch.object(cli, "state_lock") as lock:
            lock.return_value.__enter__.return_value = None
            lock.return_value.__exit__.return_value = False
            cli._autosave_from_tmux()
        self.assertEqual(save.call_args.args[0]["file_manager"], previous["file_manager"])

    def test_file_manager_startup_failure_does_not_block_workspace_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.args(category=None, owns_tmux_restore=True)
            def restore(snapshot, category_args, startup=False):
                if category_args.category == "file-manager":
                    raise RuntimeError("Nemo destination unavailable")
                return {category_args.category: 0, "codex_ready": 0, "codex_total": 0,
                        "codex_verified": 1, "virtual_machines_total": 0,
                        "virtual_machines_message": "none"}
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={"sessions": [], "desktop": {}}), \
                 patch.object(cli, "needs_storage", return_value=False), \
                 patch.object(cli, "_restore", side_effect=restore), \
                 patch.object(cli, "_wait_for_shell"), \
                 patch.object(cli, "_publish_workspace_restored") as publish, \
                 patch.object(cli, "_arm_autosave_if_startup_complete"), \
                 patch.object(cli, "set_overall"), patch.object(cli, "update_stage"):
                with self.assertRaisesRegex(RuntimeError, "Nemo destination unavailable"):
                    cli.cmd_startup(args)
            publish.assert_called_once()

    def test_automatic_startup_defers_storage_record_without_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self.args(category=None, owns_tmux_restore=True)
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={"sessions": [], "desktop": {}, "file_manager": {"windows": [{"locations": ["file:///home/sagecat/Drives/gdrive"]}]} }), \
                 patch.object(cli, "needs_storage", return_value=True), \
                 patch.object(cli, "_wait_for_shell"), \
                 patch.object(cli, "_restore", return_value={category: 0 for category in cli.CATEGORIES}) as restore, \
                 patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                 patch.object(cli, "_arm_autosave_if_startup_complete"), \
                 patch.object(cli, "set_overall"), patch.object(cli, "update_stage"):
                self.assertEqual(cli.cmd_startup(args), 0)
            self.assertNotIn("file-manager", [call.args[1].category for call in restore.call_args_list])
            run.assert_called_once()
            self.assertIn(cli.WORKSPACE_RESTORED_TARGET, run.call_args.args[0])
            self.assertFalse((root / "file-manager.done").exists())
            self.assertTrue((root / "file-manager.deferred").exists())

    def test_deferred_restore_finishes_once_and_arms_autosave(self):
        from workspace_state import login_status, operations
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            operations.bind(None)
            self.addCleanup(operations.bind, None)
            login_status.initialize("file-manager-test")
            context = operations.current()
            for identifier, _label in login_status.DEFAULT_STAGES:
                login_status.update_stage(identifier, "ready", "Verified fixture")
            root = Path(directory)
            (root / "file-manager.deferred").touch()
            for category in cli.CATEGORIES:
                if category != "file-manager":
                    write_stage_marker(root / f"{category}.done", StageMarker(category, "ready", operation_context=context.to_dict()))
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={"created_at": "checkpoint"}), \
                 patch.object(cli, "_restore", return_value={"file-manager": 2}) as restore, \
                 patch.object(cli, "update_stage"), patch.object(cli, "_arm_autosave") as arm:
                self.assertTrue(cli.finish_deferred_file_manager())
                self.assertTrue(cli.finish_deferred_file_manager())
            restore.assert_called_once()
            self.assertEqual(restore.call_args.args[1].category, "file-manager")
            arm.assert_called_once()
            self.assertFalse((root / "file-manager.deferred").exists())
            self.assertEqual(read_stage_marker(root / "file-manager.done").snapshot, "checkpoint")
            self.assertTrue(read_stage_marker(root / "file-manager.done").verified)

    def test_failed_deferred_restore_is_not_relaunched_or_reported_successful(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file-manager.deferred").touch()
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={}), \
                 patch.object(cli, "_restore", side_effect=RuntimeError("mount unavailable")) as restore, \
                 patch.object(cli, "update_stage") as stage, \
                 patch.object(cli, "_arm_autosave_if_startup_complete") as arm:
                self.assertFalse(cli.finish_deferred_file_manager())
                self.assertFalse(cli.finish_deferred_file_manager())
            restore.assert_called_once()
            arm.assert_called_once()
            self.assertEqual(read_stage_marker(root / "file-manager.done").state, "failed")
            self.assertTrue(any(call.args[1] == "failed" for call in stage.call_args_list))

    def test_unrequested_deferred_restore_does_not_load_or_launch(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "_startup_directory", return_value=Path(directory)), \
             patch.object(cli, "load") as load, patch.object(cli, "_restore") as restore:
            self.assertTrue(cli.finish_deferred_file_manager())
        load.assert_not_called()
        restore.assert_not_called()

    def test_deferred_checkpoint_load_failure_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "file-manager.deferred").touch()
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", side_effect=ValueError("invalid checkpoint")):
                with self.assertRaisesRegex(ValueError, "invalid checkpoint"):
                    cli.finish_deferred_file_manager()


if __name__ == "__main__":
    unittest.main()
