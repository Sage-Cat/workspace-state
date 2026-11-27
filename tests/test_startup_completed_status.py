from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack, nullcontext, redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import cli, login_finalize, login_status, operations
from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderItemResult


class RepeatedStartupStatusTests(unittest.TestCase):
    """A repeated startup reconciles; only pending compositor placements are left alone."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}))
        login_status.initialize("isolated-test-session")
        self.stack.enter_context(patch.object(cli, "_startup_lock", side_effect=nullcontext))
        self.stack.enter_context(patch.object(cli, "load", return_value={"sessions": []}))
        self.stack.enter_context(patch.object(cli, "_publish_workspace_restored"))
        self.stack.enter_context(patch.object(cli, "_wait_for_shell"))
        self.needs_storage = self.stack.enter_context(patch.object(cli, "needs_storage", return_value=False))
        self.needs_vscode = self.stack.enter_context(patch.object(cli, "needs_vscode_storage", return_value=False))
        self.restore = self.stack.enter_context(patch.object(cli, "_restore", return_value={
            **{category: 0 for category in cli.CATEGORIES},
            "codex_ready": 0, "codex_total": 0, "codex_verified": 1}))

    def stage(self, identifier):
        data = json.loads(login_status.status_path().read_text())
        return next(item for item in data["stages"] if item["id"] == identifier)

    def startup(self, category=None):
        with redirect_stdout(io.StringIO()):
            return cli.cmd_startup(Namespace(
                category=category, dry_run=False, force=False, owns_tmux_restore=True, no_place=True,
                wait=0, workspace=None, session=None, select=False))

    def waiting(self, category, request="native-handoff:token"):
        waiting = {"state": "waiting", "detail": "", "retryable": True, "request_id": request}
        verified = {"state": "verified", "detail": ""}
        login_status.update_stage(category, "waiting", "Waiting for placement", current=0, total=1)
        login_status.publish_provider_results(category, [ProviderItemResult(
            category, "1", PhaseEvidence(EvidenceState.VERIFIED), PhaseEvidence(EvidenceState.VERIFIED),
            PhaseEvidence(EvidenceState.WAITING, "handoff", True, request))])

    def test_a_repeated_startup_restores_each_category_again(self):
        for category in cli.CATEGORIES:
            for state in ("ready", "skipped", "degraded", "failed"):
                with self.subTest(category=category, state=state):
                    login_status.update_stage(category, state, "Prior outcome", current=3, total=4)
                    self.restore.reset_mock()
                    self.assertEqual(self.startup(category), 0)
                    self.restore.assert_called_once()
                    self.assertEqual(self.restore.call_args.args[1].category, category)

    def test_a_category_with_a_pending_placement_is_left_alone(self):
        self.waiting("vscode")
        before = self.stage("vscode")
        self.assertEqual(self.startup("vscode"), 0)
        self.assertEqual(self.stage("vscode"), before)
        self.restore.assert_not_called()

    def test_only_the_pending_category_is_skipped(self):
        self.waiting("vscode")
        self.assertEqual(self.startup(), 0)
        self.assertEqual({call.args[1].category for call in self.restore.call_args_list},
                         set(cli.CATEGORIES) - {"vscode"})

    def categories(self):
        return {call.args[1].category for call in self.restore.call_args_list}

    def test_before_the_finalizer_starts_a_storage_category_waits_for_the_drives(self):
        self.needs_storage.return_value = True
        for _ in range(2):  # a repeat defers it again
            self.restore.reset_mock()
            self.assertEqual(self.startup(), 0)
            self.assertEqual(self.stage("file-manager")["state"], "waiting")
            self.assertNotIn("file-manager", self.categories())

    def test_while_the_finalizer_runs_a_repeat_leaves_the_storage_category_alone(self):
        self.needs_storage.return_value = True
        self.needs_vscode.return_value = True
        login_status.update_stage("login-finalization", "running", "Loading drives")
        login_status.update_stage("file-manager", "ready", "Restored by the finalizer", current=2, total=2)
        before = self.stage("file-manager")
        self.assertEqual(self.startup(), 0)
        self.assertEqual(self.stage("file-manager"), before)
        self.assertTrue({"file-manager", "vscode"}.isdisjoint(self.categories()))
        self.assertIn("terminals", self.categories())

    def test_after_the_finalizer_ended_a_repeat_restores_the_storage_category_directly(self):
        self.needs_storage.return_value = True
        for state in ("ready", "degraded", "failed"):
            with self.subTest(state=state):
                login_status.update_stage("login-finalization", state, "Done", current=1, total=1)
                self.restore.reset_mock()
                self.assertEqual(self.startup(), 0)
                self.assertIn("file-manager", self.categories())

    def test_the_target_is_published_only_for_a_whole_startup(self):
        cli._publish_workspace_restored.reset_mock()
        self.startup("terminals")
        cli._publish_workspace_restored.assert_not_called()
        self.startup()
        cli._publish_workspace_restored.assert_called_once()


class FinishedStartupTests(unittest.TestCase):
    """A startup that has ended is refused, and the refusal changes nothing."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}))
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        operations.bind(None)
        login_status.initialize("isolated-finished-session")
        operations.bind(None)  # A manual run in a shell inherits no operation.

    def test_a_running_startup_is_not_refused(self):
        self.assertIsNone(cli._finished_startup())

    def test_a_completed_startup_is_refused_without_touching_the_status(self):
        for name, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(name, "ready", "Done", current=1, total=1)
        login_status.finish()
        document = json.loads(login_status.status_path().read_text())
        self.assertEqual(document["operation_state"], "completed")
        before = login_status.status_path().read_bytes()
        operations.bind(None)
        stderr = io.StringIO()
        with redirect_stderr(stderr), patch.object(cli, "cmd_startup") as startup:
            self.assertEqual(cli.main(["startup"]), 2)
        startup.assert_not_called()
        self.assertIn("wsctl restore", stderr.getvalue())
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_an_expired_startup_is_refused_without_touching_the_status(self):
        document = json.loads(login_status.status_path().read_text())
        document["operation_context"]["deadline"] = time.monotonic() - 1
        login_status.status_path().write_text(json.dumps(document))
        before = login_status.status_path().read_bytes()
        with redirect_stderr(io.StringIO()), patch.object(cli, "cmd_startup") as startup:
            self.assertEqual(cli.main(["startup"]), 2)
        startup.assert_not_called()
        self.assertEqual(login_status.status_path().read_bytes(), before)


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
        self.assertEqual(login_finalize.main(), 0)
        status = self.status()
        self.assertEqual(status["overall_state"], "failed")
        self.assertEqual(status["overall_message"], "Login completed with items needing attention")
        finalizer = next(stage for stage in status["stages"] if stage["id"] == "login-finalization")
        self.assertEqual(finalizer["state"], "ready")
        self.assertIn("vscode, browsers", finalizer["message"])

    def test_earlier_failure_prevents_all_ready_message_when_finalizer_succeeds(self):
        login_status.update_stage("browsers", "failed", "Browser placement failed", error="Browser placement failed")
        self.assertEqual(login_finalize.main(), 0)
        self.assertEqual(self.status()["overall_message"], "Login completed with items needing attention")

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
        self.assertEqual(login_finalize._attention_startup_stages(), [])


def tearDownModule():
    # Startup code binds the operation context itself; it must not outlive this module.
    from workspace_state import operations
    operations.bind(None)


if __name__ == "__main__":
    unittest.main()
