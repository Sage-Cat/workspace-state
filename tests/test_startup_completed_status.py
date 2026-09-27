from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack, nullcontext
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, login_finalize, login_status


class CompletedStartupStatusTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}))
        login_status.initialize("isolated-test-session")
        self.stack.enter_context(patch.object(cli, "_startup_lock", side_effect=nullcontext))
        self.stack.enter_context(patch.object(cli, "_startup_marker", side_effect=lambda category: self.root / f"{category}.done"))
        self.stack.enter_context(patch.object(cli, "load", return_value={"sessions": []}))
        self.stack.enter_context(patch.object(cli, "_publish_workspace_restored"))
        self.stack.enter_context(patch.object(cli, "_arm_autosave_if_startup_complete"))
        self.restore = self.stack.enter_context(patch.object(cli, "_restore"))

    def stage(self, identifier):
        data = json.loads(login_status.status_path().read_text())
        return next(item for item in data["stages"] if item["id"] == identifier)

    def startup(self, category):
        return cli.cmd_startup(Namespace(
            category=category, dry_run=False, force=False, owns_tmux_restore=True,
        ))

    def test_repeated_startup_preserves_terminal_outcome_counts_and_events(self):
        for category in cli.CATEGORIES:
            (self.root / f"{category}.done").write_text("saved-snapshot\n")
            for state, count in (("ready", 4), ("skipped", 0), ("degraded", 3)):
                with self.subTest(category=category, state=state):
                    login_status.update_stage(category, state, "Prior verified outcome", current=count, total=4)
                    before = self.stage(category)
                    self.assertEqual(self.startup(category), 0)
                    self.assertEqual(self.stage(category), before)
        self.restore.assert_not_called()

    def test_marker_without_terminal_proof_never_invents_success_or_counts(self):
        category = "file-manager"
        (self.root / f"{category}.done").write_text("saved-snapshot\n")
        self.assertEqual(self.stage(category)["state"], "pending")
        self.assertEqual(self.startup(category), 0)
        stage = self.stage(category)
        self.assertEqual(stage["state"], "degraded")
        self.assertIn("completion details are unavailable", stage["message"])
        self.assertNotIn("current", stage)
        self.assertNotIn("total", stage)
        self.restore.assert_not_called()

    def test_failed_prior_report_is_preserved_even_with_success_marker(self):
        category = "browsers"
        (self.root / f"{category}.done").write_text("saved-snapshot\n")
        login_status.update_stage(category, "failed", "Missing profile", current=2, total=4, error="Missing profile")
        before = self.stage(category)
        with self.assertRaisesRegex(cli.RestoreJobsFailed, "browsers: Missing profile"):
            self.startup(category)
        self.assertEqual(self.stage(category), before)
        self.restore.assert_not_called()

    def test_failed_marker_remains_authoritative_over_prior_success(self):
        category = "social-apps"
        (self.root / f"{category}.done").write_text("failed: window disappeared\n")
        login_status.update_stage(category, "ready", "Earlier success", current=4, total=4)
        with self.assertRaisesRegex(cli.RestoreJobsFailed, "window disappeared"):
            self.startup(category)
        self.assertEqual(self.stage(category)["state"], "failed")
        self.assertIn("window disappeared", self.stage(category)["error"])
        self.restore.assert_not_called()


class FinalizerAggregateStatusTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}))
        login_status.initialize("isolated-finalizer-session")
        for identifier, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(identifier, "ready", "Verified", current=4, total=4)
        self.stack.enter_context(patch.object(login_finalize, "_start_drives", return_value={"gdrive": True}))
        for name in ("finish_deferred_file_manager", "finish_deferred_vscode", "finish_deferred_codex", "_warm_cloud_metadata"):
            self.stack.enter_context(patch.object(login_finalize, name, return_value=True))

    def status(self):
        return json.loads(login_status.status_path().read_text())

    def test_summary_merges_earlier_browser_failure_with_deferred_failure(self):
        login_status.update_stage("browsers", "failed", "Browser placement failed", error="Browser placement failed")
        login_status.update_stage("vscode", "failed", "Editor failed", error="Editor failed")
        login_finalize.finish_deferred_vscode.return_value = False
        self.assertEqual(login_finalize.main(), 1)
        status = self.status()
        self.assertEqual(status["overall_state"], "failed")
        self.assertEqual(status["overall_message"], "Login completed with failures: vscode, browsers")

    def test_earlier_failure_prevents_all_ready_message_when_finalizer_succeeds(self):
        login_status.update_stage("browsers", "failed", "Browser placement failed", error="Browser placement failed")
        self.assertEqual(login_finalize.main(), 1)
        self.assertEqual(self.status()["overall_message"], "Login completed with failures: browsers")

    def test_nonfailed_incomplete_states_are_not_promoted_to_failure(self):
        for state in ("running", "degraded"):
            with self.subTest(state=state):
                login_status.update_stage("browsers", state, "Verification incomplete", current=2, total=4)
                self.assertEqual(login_finalize.main(), 0)
                self.assertEqual(self.status()["overall_state"], state)

    def test_shutdown_report_is_not_imported_into_startup_failure_list(self):
        login_status.status_path().write_text(json.dumps({
            "mode": "shutdown", "stages": [{"id": "workspace-save", "state": "failed"}],
        }))
        self.assertEqual(login_finalize._failed_startup_stages(), [])


if __name__ == "__main__":
    unittest.main()
