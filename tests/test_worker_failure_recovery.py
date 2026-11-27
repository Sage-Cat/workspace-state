"""A dead restore worker closes its stages and hands over to the finalizer."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, login_status, operations, startup
from workspace_state.util import atomic_json


def stage(identifier, state, **extra):
    return {"id": identifier, "label": identifier, "state": state, **extra}


class WorkerFailureTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = operations.OperationContext.create("test-login", "startup")
        waiting_results = [{"item_id": "a", "placement": {"state": "waiting", "detail": "x", "request_id": "r1"}}]
        stale_results = [{"item_id": "b", "placement": {"state": "waiting", "detail": "x"}}]
        self.document = dict(
            operation_context=self.context.to_dict(), operation_id=self.context.operation_id,
            schema_version=login_status.SCHEMA_VERSION, session_id=self.context.login_generation, mode="startup", operation_state="running",
            overall_state="running", stages=[
                stage("gnome", "ready"), stage("displays", "ready"), stage("tmux", "ready"),
                stage("terminals", "ready"), stage("codex", "waiting"),
                stage("browsers", "running"), stage("social-apps", "pending"),
                stage("file-manager", "waiting", provider_results=waiting_results),
                stage("vscode", "waiting", provider_results=stale_results),
                stage("workspace", "running"),
                stage("gdrive", "pending"), stage("warmup", "pending")])
        self.write()
        operations.bind(self.context)
        self.addCleanup(operations.bind, None)

    def write(self):
        atomic_json(login_status.status_path(), self.document)
        login_status._record_operation(self.document)

    def status(self):
        return json.loads(login_status.status_path().read_text())

    def states(self):
        return {item["id"]: item["state"] for item in self.status()["stages"]}

    def test_dead_worker_fails_its_open_stages_and_starts_the_finalizer_once(self):
        with patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertTrue(startup.report_worker_failure("Startup worker ended without completion: timeout"))
        publish.assert_called_once_with()
        states = self.states()
        for name in ("browsers", "social-apps", "workspace", "vscode"):
            self.assertEqual(states[name], "failed", name)
        self.assertIn("timeout", next(s for s in self.status()["stages"] if s["id"] == "browsers")["error"])
        # Owned by the compositor, the drives or the finalizer: left alone.
        self.assertEqual(states["file-manager"], "waiting")
        self.assertEqual(states["codex"], "waiting")
        self.assertEqual((states["gdrive"], states["warmup"]), ("pending", "pending"))
        self.assertEqual((states["gnome"], states["tmux"]), ("ready", "ready"))
        self.assertEqual(self.status()["operation_state"], "running")

    def test_the_finalizer_path_can_still_close_the_operation(self):
        with patch.object(cli, "_publish_workspace_restored"):
            startup.report_worker_failure("Startup worker ended without completion: timeout")
        # The compositor verifies the request it owned; the finalizer observes that.
        self.document = self.status()
        next(s for s in self.document["stages"] if s["id"] == "file-manager")["provider_results"][0][
            "placement"] = {"state": "verified", "detail": "placed"}
        self.write()
        # What the finalizer does: drives, warm-up and its own stage reach a terminal state.
        for name in ("file-manager", "codex"):
            login_status.update_stage(name, "failed", "closed by the finalizer", error="closed by the finalizer")
        for name in ("gdrive", "warmup", "login-finalization"):
            login_status.update_stage(name, "ready", "ok", current=1, total=1)
        login_status.finish()
        self.assertEqual(self.status()["operation_state"], "failed")

    def test_a_superseded_operation_changes_and_starts_nothing(self):
        newer = operations.OperationContext.create("test-login", "startup")
        self.document.update(operation_context=newer.to_dict(), operation_id=newer.operation_id)
        self.write()
        with patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertFalse(startup.report_worker_failure("Startup worker ended without completion: timeout"))
        publish.assert_not_called()
        self.assertEqual(self.states()["browsers"], "running")

    def test_an_operation_of_another_boot_changes_and_starts_nothing(self):
        with patch.object(operations, "boot_id", return_value="another-boot"), \
                patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertFalse(startup.report_worker_failure("Startup worker ended without completion: timeout"))
        publish.assert_not_called()
        self.assertEqual(self.states()["browsers"], "running")

    def test_a_target_that_cannot_start_does_not_hide_the_recorded_failure(self):
        with patch.object(cli, "_publish_workspace_restored", side_effect=RuntimeError("no systemd")):
            self.assertTrue(startup.report_worker_failure("Startup worker ended without completion: timeout"))
        self.assertEqual(self.states()["browsers"], "failed")

    def test_a_successful_service_result_does_nothing_new(self):
        with patch.dict(os.environ, {"SERVICE_RESULT": "success"}), \
                patch.object(startup.sys, "argv", ["startup", "worker-exit"]), \
                patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertEqual(startup.main(), 0)
        publish.assert_not_called()
        self.assertEqual(self.states()["browsers"], "running")

    def test_a_failed_service_result_reports_the_failure(self):
        with patch.dict(os.environ, {"SERVICE_RESULT": "timeout"}), \
                patch.object(startup.sys, "argv", ["startup", "worker-exit"]), \
                patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertEqual(startup.main(), 0)
        publish.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
