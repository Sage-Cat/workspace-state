from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import login_status, operations, startup

SESSION = "a" * 16
LOGIN = "b" * 16
UNIT = f"wsctl-startup-restore-{LOGIN}.service"


class TerminalsPendingTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(operations.bind, operations.current())
        self.root = Path(directory.name) / "workspace-state"
        self.root.mkdir(mode=0o700)
        (self.root / "login-generation").write_text(SESSION + "\n")
        (self.root / "login-key").write_text(LOGIN + "\n")
        self.assertTrue(login_status.initialize(SESSION, login_key=LOGIN, show_startup_hud=False))
        self.active = {startup.AGENT_UNIT}
        self.unit_patch = patch.object(startup, "_unit_may_run",
                                       side_effect=lambda name: name in self.active)
        self.unit = self.unit_patch.start()
        self.addCleanup(self.unit_patch.stop)

    def edit(self, mutate):
        path = login_status.status_path()
        document = json.loads(path.read_text())
        mutate(document)
        path.write_text(json.dumps(document))

    def stage(self, state):
        self.edit(lambda document: next(
            item for item in document["stages"] if item["id"] == "terminals").update(state=state))

    def test_a_running_startup_of_this_login_with_a_live_agent_is_pending(self):
        self.assertTrue(startup.terminals_pending())

    def test_the_restore_service_alone_keeps_it_pending(self):
        self.active = {UNIT}
        self.assertTrue(startup.terminals_pending())

    def test_another_login_is_not_pending(self):
        self.edit(lambda document: document.update(login_key="c" * 16))
        self.assertFalse(startup.terminals_pending())
        self.edit(lambda document: document.update(login_key=LOGIN, session_id="d" * 16))
        self.assertFalse(startup.terminals_pending())
        # The same session id with a missing key means the session id is the key.
        self.edit(lambda document: (document.update(session_id=SESSION), document.pop("login_key")))
        self.assertFalse(startup.terminals_pending())

    def test_a_finished_operation_is_not_pending(self):
        for state in ("completed", "failed", "cancelled", "authorized"):
            with self.subTest(state=state):
                self.edit(lambda document: document.update(operation_state=state))
                self.assertFalse(startup.terminals_pending())

    def test_final_terminals_are_not_pending_and_other_states_are(self):
        # `waiting` is set only after tmux is restored, as the old marker was.
        for state in ("ready", "degraded", "failed", "skipped", "waiting"):
            with self.subTest(state=state):
                self.stage(state)
                self.assertFalse(startup.terminals_pending())
        for state in ("pending", "running"):
            with self.subTest(state=state):
                self.stage(state)
                self.assertTrue(startup.terminals_pending())

    def test_a_passed_deadline_is_not_pending(self):
        context = operations.OperationContext.from_dict(
            json.loads(login_status.status_path().read_text())["operation_context"])
        with patch("workspace_state.operations.time.monotonic", return_value=context.deadline + 1):
            self.assertFalse(startup.terminals_pending())

    def test_a_status_of_another_boot_is_not_pending(self):
        self.edit(lambda document: document["operation_context"].update(boot_id="another-boot"))
        self.assertFalse(startup.terminals_pending())

    def test_no_agent_and_no_restore_service_is_not_pending(self):
        self.active = set()
        self.assertFalse(startup.terminals_pending())
        self.unit.assert_any_call(startup.AGENT_UNIT)
        self.unit.assert_any_call(UNIT)

    def test_the_unit_check_runs_only_when_everything_else_says_pending(self):
        self.stage("ready")
        self.assertFalse(startup.terminals_pending())
        self.unit.assert_not_called()

    def test_a_missing_status_is_not_pending_even_with_a_live_agent(self):
        login_status.status_path().unlink()
        self.assertFalse(startup.terminals_pending())

    def test_a_malformed_status_is_pending_only_while_something_can_restore(self):
        for text in ("{not json", "[]", json.dumps({"schema_version": 1, "mode": "startup"}), ""):
            with self.subTest(text=text):
                login_status.status_path().write_text(text)
                self.active = {startup.AGENT_UNIT}
                self.assertTrue(startup.terminals_pending())
                self.active = {UNIT}
                self.assertTrue(startup.terminals_pending())
                self.active = set()
                self.assertFalse(startup.terminals_pending())

    def test_a_status_with_a_broken_context_is_malformed(self):
        self.edit(lambda document: document["operation_context"].pop("deadline"))
        self.assertTrue(startup.terminals_pending())
        self.active = set()
        self.assertFalse(startup.terminals_pending())

    def test_the_command_exits_zero_only_when_settled(self):
        with patch.object(startup.sys, "argv", ["startup", "terminals-settled"]):
            self.assertEqual(startup.main(), 1)  # pending
            self.active = set()
            self.assertEqual(startup.main(), 0)  # settled

    def test_an_exception_never_exits_zero(self):
        with patch.object(startup.sys, "argv", ["startup", "terminals-settled"]), \
                patch.object(startup, "terminals_pending", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                startup.main()

    def record_restore(self, login_key=LOGIN):
        self.edit(lambda document: document.update(restore_unit=UNIT, restore_login_key=login_key))

    def test_a_recorded_restore_service_alone_decides(self):
        self.record_restore()
        # The agent lives all session and must not mask a dead restore service.
        self.active = {startup.AGENT_UNIT}
        self.assertFalse(startup.terminals_pending())
        self.active = {UNIT}
        self.assertTrue(startup.terminals_pending())
        self.unit.assert_called_with(UNIT)

    def test_a_record_of_another_login_does_not_replace_the_agent_check(self):
        self.record_restore("c" * 16)
        self.active = {startup.AGENT_UNIT}
        self.assertTrue(startup.terminals_pending())

    def test_only_a_clear_dead_answer_means_not_running(self):
        self.unit_patch.stop()
        self.addCleanup(self.unit_patch.start)

        def answer(returncode, output):
            result = subprocess.CompletedProcess([], returncode, stdout=output, stderr="")
            return patch.object(startup.subprocess, "run", return_value=result)

        for output in ("inactive\n", "failed\n"):
            with self.subTest(output=output), answer(0, output):
                self.assertFalse(startup._unit_may_run("x.service"))
        for output in ("active\n", "activating\n", "deactivating\n", "reloading\n",
                       "refreshing\n", "", "maintenance\n", "garbage\n"):
            with self.subTest(output=output), answer(0, output):
                self.assertTrue(startup._unit_may_run("x.service"))
        with answer(1, "inactive\n"):  # a failed query is not an answer
            self.assertTrue(startup._unit_may_run("x.service"))
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("systemctl", 2)):
            with self.subTest(error=error), patch.object(startup.subprocess, "run", side_effect=error):
                self.assertTrue(startup._unit_may_run("x.service"))
        with answer(0, "active\n") as run:
            startup._unit_may_run("x.service")
            self.assertEqual(run.call_args.args[0], [
                "systemctl", "--user", "show", "-p", "ActiveState", "--value", "x.service"])
            self.assertEqual(run.call_args.kwargs["timeout"], startup.UNIT_CHECK_TIMEOUT)


class RemovedStartupCommandsTests(unittest.TestCase):
    def test_report_failure_stays_for_workers_of_an_older_release(self):
        with patch.object(startup.sys, "argv", ["startup", "report-failure", "exit status 3"]), \
                patch.object(startup, "report_worker_failure") as report:
            self.assertEqual(startup.main(), 0)
        report.assert_called_once_with("Startup worker failed: exit status 3")

    def test_context_and_worker_budget_are_gone(self):
        for command in ("context", "worker-budget"):
            with self.subTest(command=command), \
                    patch.object(startup.sys, "argv", ["startup", command]), \
                    patch.object(startup, "runtime_identity", side_effect=RuntimeError("directory")):
                with self.assertRaises(RuntimeError):
                    startup.main()


if __name__ == "__main__":
    unittest.main()
