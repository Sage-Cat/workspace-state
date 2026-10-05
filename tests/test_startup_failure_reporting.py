"""Synthetic, operation-scoped reporting; no host services are probed."""
import os
from contextlib import ExitStack
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import alerts, login_finalize, login_status, operations, startup_failure
from workspace_state.util import atomic_json


class StartupFailureReportingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": temp.name})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = operations.OperationContext.create("test-login", "startup")
        self.document = dict(operation_context=self.context.to_dict(), operation_id=self.context.operation_id,
                             session_id=self.context.login_generation, mode="startup", operation_state="failed",
                             stages=[{"id": "browsers", "state": "failed", "provider_results": [
                                 {"identity": {"state": "failed", "detail":
                                  "Original grouped Chrome window does not match the saved tabs; no replacement window created."}}]},
                                 {"id": "workspace", "state": "failed", "provider_completion_pending": True}])
        self.message = startup_failure.failure_message(self.document, ["browsers", "workspace"])
        self.document["stages"].append(dict(id="login-finalization", state="failed", message=self.message))
        self.source = dict(host="local", scope="user", unit="wsctl-login-finalize.service")
        self.props = dict(Result="exit-code", ExecMainStatus="1", InvocationID="a" * 32)
        self.receipt = login_status.status_path().parent / "finalizers" / ("a" * 32 + ".json")
        self.write()

    def write(self):
        atomic_json(login_status.status_path(), self.document)
        atomic_json(self.receipt, self.context.to_dict())
        atomic_json(self.receipt.with_suffix(".outcome.json"), dict(
            operation_context=self.context.to_dict(), kind="startup-incomplete", message=self.message))

    def test_reconciliation_message_explains_review_and_aggregate(self):
        self.assertIn("Compare saved and open tabs", self.message)
        self.assertIn("choose the baseline", self.message)
        self.assertIn("summarize", self.message)
        extra = startup_failure.failure_message(self.document, ["browsers", "workspace", "warmup"])
        self.assertIn("Other failed steps: warmup", extra)

    def test_other_errors_are_not_misdiagnosed_as_browser_reconciliation(self):
        self.document["stages"][0]["provider_results"][0]["identity"]["detail"] = "Companion not connected"
        self.assertEqual(startup_failure.failure_message(self.document, ["browsers", "workspace"]),
                         "Login completed with failures: browsers, workspace")

    def test_verified_content_failed_placement_has_specific_aggregate_explanation(self):
        self.document['stages'][0]['provider_results'] = [{
            'provider': 'chrome', 'identity': {'state': 'verified'},
            'content': {'state': 'verified'},
            'placement': {'state': 'failed', 'detail': 'GNOME rejected exact placement'},
        }]
        message = startup_failure.failure_message(self.document, ['browsers', 'workspace', 'warmup'])
        self.assertIn('native window placement failed', message)
        self.assertIn('preserve the open tabs', message)
        self.assertIn('Workspace failure is the same aggregate', message)
        self.assertIn('Other failed steps: warmup', message)
        self.assertNotIn('choose the baseline', message)
        self.document['stages'][0]['provider_results'][0]['content']['state'] = 'waiting'
        self.assertFalse(startup_failure.browser_placement_failed(self.document))

    def test_new_reconciliation_contract_at_category_and_identity(self):
        browser = self.document["stages"][0]
        browser["provider_results"][0]["identity"]["detail"] = "Browser reconciliation needs review: ambiguous observation"
        self.assertTrue(startup_failure.browser_reconciliation_pending(self.document))
        browser["provider_results"] = []
        browser["provider_error"] = "Browser reconciliation needs review: incomplete catalog"
        self.assertTrue(startup_failure.browser_reconciliation_pending(self.document))
        self.assertIn("incomplete catalog", startup_failure.failure_message(self.document, ["browsers"]))
        browser["state"] = "ready"
        self.assertFalse(startup_failure.browser_reconciliation_pending(self.document))

    def test_independent_workspace_failure_is_not_hidden(self):
        self.document["stages"][1].pop("provider_completion_pending")
        message = startup_failure.failure_message(self.document, ["browsers", "workspace"])
        self.assertIn("Other failed steps: workspace", message)

    def test_exact_invocation_outcome_correlates(self):
        self.assertEqual(alerts._startup_outcome(self.source, self.props), self.message)

    def test_wrong_invocation_or_signal_or_remote_cannot_borrow_outcome(self):
        for props in [self.props | {"InvocationID": "b" * 32}, self.props | {"Result": "signal"},
                      self.props | {"ExecMainStatus": "9"}, self.props | {"InvocationID": "../unsafe"}]:
            self.assertIsNone(alerts._startup_outcome(self.source, props))
        self.assertIsNone(alerts._startup_outcome(self.source | {"host": "remote"}, self.props))
        self.assertIsNone(alerts._startup_outcome(self.source | {"unit": "another.service"}, self.props))

    def test_previous_boot_operation_attempt_or_generation_cannot_correlate(self):
        with patch.object(alerts, "boot_id", return_value="another-boot"):
            self.assertIsNone(alerts._startup_outcome(self.source, self.props))
        for key, value in [("operation_id", "other"), ("session_id", "new-login"), ("mode", "shutdown")]:
            with self.subTest(key=key):
                changed = self.document | {key: value}
                atomic_json(login_status.status_path(), changed)
                self.assertIsNone(alerts._startup_outcome(self.source, self.props))
        self.document["operation_context"]["attempt"] += 1
        atomic_json(login_status.status_path(), self.document)
        self.assertIsNone(alerts._startup_outcome(self.source, self.props))

    def test_crash_or_missing_outcome_remains_generic(self):
        self.receipt.with_suffix(".outcome.json").unlink()
        self.assertIsNone(alerts._startup_outcome(self.source, self.props))
        self.write()
        self.document["stages"][-1]["message"] = "Login finalization failed: RuntimeError"
        atomic_json(login_status.status_path(), self.document)
        self.assertIsNone(alerts._startup_outcome(self.source, self.props))

    def test_finalizer_publishes_intentional_outcome_after_failure(self):
        self.receipt.with_suffix(".outcome.json").unlink()
        with ExitStack() as stack:
            stack.enter_context(operations.publisher(self.context))
            stack.enter_context(patch.dict(os.environ, {"INVOCATION_ID": "a" * 32}))
            for name in ("_check_operation", "finish", "set_overall", "fail_active", "update_stage"):
                stack.enter_context(patch.object(login_finalize, name))
            for name in ("finish_deferred_file_manager", "finish_deferred_vscode", "finish_deferred_codex",
                         "_warm_cloud_metadata"):
                stack.enter_context(patch.object(login_finalize, name, return_value=True))
            stack.enter_context(patch.object(login_finalize, "_start_drives", return_value={}))
            stack.enter_context(patch.object(login_finalize, "_failed_startup_stages",
                                             return_value=["browsers", "workspace"]))
            self.assertEqual(login_finalize._finalize(), 1)
        self.assertEqual(alerts._startup_outcome(self.source, self.props), self.message)

    def test_probe_preserves_failure_with_actionable_detail(self):
        props = self.props | dict(LoadState="loaded", ActiveState="failed", UnitFileState="enabled")
        import subprocess
        replies = [subprocess.CompletedProcess([], 0, "\n".join(f"{k}={v}" for k, v in props.items()), ""),
                   subprocess.CompletedProcess([], 0, "", ""), subprocess.CompletedProcess([], 0, "", "")]
        with patch.object(alerts, "command", side_effect=replies):
            outcome = alerts.probe(self.source | {"kind": "systemd", "expected": "on-demand"}, None)
        self.assertEqual(outcome["health"], "blocked")
        self.assertEqual(outcome["issues"][0][0], "service-failed")
        self.assertIn("Compare saved and open tabs", outcome["issues"][0][3])
        self.assertIn("не окреме падіння", outcome["messages"]["service-failed"])


if __name__ == "__main__":
    unittest.main()
