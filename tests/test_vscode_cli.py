from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli


def args(category="vscode", **overrides):
    values = dict(category=category, workspace=None, session=None, select=False,
                  dry_run=False, no_place=False, login_status=False, wait=0,
                  force=False,
                  verify_codex=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class VscodeCliTests(unittest.TestCase):
    def test_capture_failure_is_recorded_without_erasing_previous_category(self):
        with patch.object(cli, "capture", side_effect=lambda **kwargs: {
                 "sessions": [], "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}), \
             patch.object(cli, "capture_browser", return_value={}), \
             patch.object(cli, "capture_social_apps", return_value={}), \
             patch.object(cli, "capture_file_manager", return_value={"windows": []}), \
             patch.object(cli, "capture_vscode", side_effect=RuntimeError("bridge unavailable")), \
             patch.object(cli, "capture_shell", return_value={"available": True}):
            result = cli._capture_all()
        self.assertEqual(result["sessions"], [])
        self.assertIn("bridge unavailable", result["capture_errors"]["vscode"])
        self.assertNotIn("vscode", result)

    def test_unsafe_editor_state_is_a_vscode_category_failure(self):
        error = cli.UnsafeEditorState("unsaved edits")
        with patch.object(cli, "capture", return_value={"sessions": [], "created_at": "2026-01-01T00:00:00+00:00"}), \
             patch.object(cli, "capture_browser", return_value={}), \
             patch.object(cli, "capture_social_apps", return_value={}), \
             patch.object(cli, "capture_file_manager", return_value={"windows": []}), \
             patch.object(cli, "capture_vscode", side_effect=error), \
             patch.object(cli, "capture_shell", return_value={"available": True}):
            snapshot = cli._capture_all()
        self.assertEqual(snapshot["capture_errors"]["vscode"], ["unsaved edits"])
        self.assertIs(snapshot["capture_errors"]["vscode_unsafe"], True)
        self.assertNotIn("vscode", snapshot)
        self.assertEqual(snapshot["file_manager"], {"windows": []})
        self.assertEqual(snapshot["capture_context"]["provider_evidence"]["vscode"]["state"], "failed")

    def test_empty_vscode_capture_closes_windows_seen_this_login(self):
        from workspace_state import pending
        snapshot = {"sessions": [], "terminals": [], "desktop": {"shell_companion": True},
                    "browsers": {"google_chrome": {"available": True, "profiles": []}},
                    "vscode": {"provider": "vscode", "version": 1, "windows": []}}
        seen = Path(self.enterContext(tempfile.TemporaryDirectory()))
        pending.record_seen(seen, {pending.vscode_key({"old": True})})
        with patch.object(cli, "_capture_all", return_value=snapshot), \
             patch.object(cli, "_seen_directory", return_value=seen), \
             patch.object(cli, "load", return_value={"sessions": [], "vscode": {"provider": "vscode", "version": 1, "windows": [{"old": True}]} }), \
             patch.object(cli, "state_lock") as lock, patch.object(cli, "save", return_value=Path("saved")):
            lock.return_value.__enter__.return_value = None
            lock.return_value.__exit__.return_value = False
            result = cli.cmd_save(argparse.Namespace(allow_partial=False))
        self.assertEqual(result, 0)
        self.assertEqual(snapshot["vscode"]["windows"], [])

    def test_manual_restore_routes_vscode_and_legacy_missing_is_noop(self):
        with patch.object(cli, "restore_vscode", return_value=2) as restore:
            counts = cli._restore({"sessions": [], "desktop": {}}, args())
        self.assertEqual(counts["vscode"], 2)
        restore.assert_called_once()
        with patch.object(cli, "restore_vscode") as restore:
            counts = cli._restore({"sessions": [], "desktop": {}}, args())
        restore.assert_called_once_with(None, dry_run=False, no_place=False,
                                        workspace=None, reporter=unittest.mock.ANY, timeout=30)

    def test_storage_deferred_category_finishes_after_mounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vscode.deferred").touch()
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={"created_at": "checkpoint"}), \
                 patch.object(cli, "_restore", return_value={"vscode": 1}) as restore, \
                 patch.object(cli, "update_stage"):
                self.assertTrue(cli.finish_deferred_vscode())
            restore.assert_called_once()
            self.assertFalse((root / "vscode.deferred").exists())
            self.assertTrue((root / "vscode.done").exists())

    def test_startup_missing_legacy_category_is_terminal_skipped(self):
        from workspace_state import operations
        # Startup binds an operation context; never leak it to later tests.
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            root = Path(directory)
            startup_args = args(category=None, owns_tmux_restore=True)
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "load", return_value={"sessions": [], "desktop": {}}), \
                 patch.object(cli, "_wait_for_shell"), patch.object(cli, "_restore", return_value={category: 0 for category in cli.CATEGORIES}), \
                 patch.object(cli, "_publish_workspace_restored"), \
                 patch.object(cli, "set_overall"), patch.object(cli, "update_stage"):
                self.assertEqual(cli.cmd_startup(startup_args), 0)
            self.assertTrue((root / "vscode.done").exists())

    def test_marker_error_is_visible_and_not_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "vscode.done").write_text("failed: bridge unavailable\n")
            with patch.object(cli, "_startup_directory", return_value=root), \
                 patch.object(cli, "_restore") as restore:
                self.assertFalse(cli.finish_deferred_vscode())
            restore.assert_not_called()


if __name__ == "__main__":
    unittest.main()
