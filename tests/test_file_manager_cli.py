from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli, login_status, operations


class FileManagerCliTests(unittest.TestCase):
    def args(self, category="file-manager", **kwargs):
        values = dict(category=category, workspace=None, session=None, select=False,
                      dry_run=False, no_place=False, login_status=False, wait=0,
                      force=False,
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
        from workspace_state import operations
        # Startup binds an operation context; never leak it to later tests.
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
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
                 patch.object(cli, "set_overall"), patch.object(cli, "update_stage") as stage:
                self.assertEqual(cli.cmd_startup(args), 0)  # a failed category is a result
            publish.assert_called_once()
            self.assertTrue(any(call.args[:2] == ("file-manager", "failed") for call in stage.call_args_list))

    def test_unrequested_deferred_restore_does_not_load_or_launch(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "_startup_directory", return_value=Path(directory)), \
             patch.object(cli, "load") as load, patch.object(cli, "_restore") as restore:
            self.assertTrue(cli.finish_deferred_file_manager())
        load.assert_not_called()
        restore.assert_not_called()


    @contextmanager
    def deferred_status(self):
        """A login whose startup left the file manager waiting for the cloud drives."""
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            operations.bind(None)
            self.addCleanup(operations.bind, None)
            login_status.initialize("file-manager-test")
            login_status.update_stage("file-manager", "waiting", "Waiting for cloud drives")
            yield

    def stage(self):
        return next(item for item in json.loads(login_status.status_path().read_text())["stages"]
                    if item["id"] == "file-manager")

    def test_automatic_startup_defers_storage_record_in_the_status_document(self):
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            login_status.initialize("file-manager-test")
            args = self.args(category=None, owns_tmux_restore=True)
            with patch.object(cli, "load", return_value={"sessions": [], "desktop": {}, "file_manager": {"windows": [{"locations": ["file:///home/sagecat/Drives/gdrive"]}]} }), \
                 patch.object(cli, "needs_storage", return_value=True), \
                 patch.object(cli, "_wait_for_shell"), \
                 patch.object(cli, "_restore", return_value={category: 0 for category in cli.CATEGORIES}) as restore, \
                 patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
                self.assertEqual(cli.cmd_startup(args), 0)
            self.assertNotIn("file-manager", [call.args[1].category for call in restore.call_args_list])
            run.assert_called_once()
            self.assertIn(cli.WORKSPACE_RESTORED_TARGET, run.call_args.args[0])
            self.assertEqual(self.stage()["state"], "waiting")

    def test_deferred_restore_finishes_once(self):
        with self.deferred_status():
            with patch.object(cli, "load", return_value={"created_at": "checkpoint"}), \
                 patch.object(cli, "_restore", return_value={"file-manager": 2}) as restore:
                self.assertTrue(cli.finish_deferred_file_manager())
                self.assertTrue(cli.finish_deferred_file_manager())
            restore.assert_called_once()
            self.assertEqual(restore.call_args.args[1].category, "file-manager")
            self.assertEqual(self.stage()["state"], "ready")

    def test_failed_deferred_restore_is_not_relaunched_or_reported_successful(self):
        with self.deferred_status():
            with patch.object(cli, "load", return_value={}), \
                 patch.object(cli, "_restore", side_effect=RuntimeError("mount unavailable")) as restore:
                self.assertFalse(cli.finish_deferred_file_manager())
                self.assertFalse(cli.finish_deferred_file_manager())
            restore.assert_called_once()
            self.assertEqual(self.stage()["state"], "failed")

    def test_deferred_checkpoint_load_failure_is_not_hidden(self):
        with self.deferred_status(), patch.object(cli, "load", side_effect=ValueError("invalid checkpoint")):
            with self.assertRaisesRegex(ValueError, "invalid checkpoint"):
                cli.finish_deferred_file_manager()


if __name__ == "__main__":
    unittest.main()
