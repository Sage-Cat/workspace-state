from __future__ import annotations

import unittest
from unittest.mock import patch

from workspace_state import login_finalize


class FileManagerFinalizeTests(unittest.TestCase):
    def setUp(self):
        ownership = patch.object(login_finalize, "_check_operation")
        ownership.start()
        self.addCleanup(ownership.stop)
        failures = patch.object(login_finalize, "_failed_startup_stages", return_value=[])
        failures.start()
        self.addCleanup(failures.stop)
        # This category's tests must not consume the real login's VS Code
        # failure/deferred marker or accidentally run a live editor restore.
        self.vscode = patch.object(login_finalize, "finish_deferred_vscode", return_value=True)
        self.vscode.start()
        self.addCleanup(self.vscode.stop)
        codex = patch.object(login_finalize, "finish_deferred_codex", return_value=True)
        codex.start()
        self.addCleanup(codex.stop)

    def test_deferred_restore_runs_after_drives_before_warmup(self):
        order: list[str] = []
        with patch.object(login_finalize, "set_overall"), \
             patch.object(login_finalize, "_start_drives", side_effect=lambda: order.append("drives") or {"gdrive": True}), \
             patch.object(login_finalize, "finish_deferred_file_manager", side_effect=lambda: order.append("file-manager") or True), \
             patch.object(login_finalize, "_warm_cloud_metadata", side_effect=lambda: order.append("warmup") or True), \
             patch.object(login_finalize, "finish"):
            self.assertEqual(login_finalize._finalize(), 0)
        self.assertEqual(order, ["drives", "file-manager", "warmup"])

    def test_deferred_failure_is_reported_but_warmup_continues(self):
        with patch.object(login_finalize, "set_overall"), \
             patch.object(login_finalize, "_start_drives", return_value={"gdrive": True}), \
             patch.object(login_finalize, "finish_deferred_file_manager", return_value=False), \
             patch.object(login_finalize, "_warm_cloud_metadata", return_value=True), \
             patch.object(login_finalize, "fail_active") as fail_active:
            self.assertEqual(login_finalize._finalize(), 1)
        fail_active.assert_called_once()
        self.assertIn("file-manager", fail_active.call_args.args[0])

    def test_unexpected_deferred_error_does_not_strand_warmup(self):
        with patch.object(login_finalize, "set_overall"), \
             patch.object(login_finalize, "_start_drives", return_value={"gdrive": True}), \
             patch.object(login_finalize, "finish_deferred_file_manager", side_effect=OSError("checkpoint unreadable")), \
             patch.object(login_finalize, "_warm_cloud_metadata", return_value=True) as warmup, \
             patch.object(login_finalize, "update_stage") as stage, \
             patch.object(login_finalize, "append_diagnostic"), \
             patch.object(login_finalize, "fail_active"):
            self.assertEqual(login_finalize._finalize(), 1)
        warmup.assert_called_once()
        self.assertTrue(any(call.args[:2] == ("file-manager", "failed") for call in stage.call_args_list))


if __name__ == "__main__":
    unittest.main()
