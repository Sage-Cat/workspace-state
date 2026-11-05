"""Milestone 4: category problems are results; one shared conversation deadline."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time as real_time
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli, login_status, operations, provider_progress
from workspace_state.provider_results import (
    EvidenceState, PhaseEvidence, ProviderCount, ProviderItemResult, ProviderRestoreError,
)
from workspace_state.startup import StageMarker, write_stage_marker

SRC = Path(__file__).resolve().parents[1] / "src"


class FakeTime:
    """A controllable clock; unknown attributes fall through to the real module."""

    def __init__(self, now=100.0):
        self.now = now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def __getattr__(self, name):
        return getattr(real_time, name)


def verified(item_id):
    phase = PhaseEvidence(EvidenceState.VERIFIED, "ok")
    return ProviderItemResult("chrome", item_id, phase, phase, phase)


def failed(item_id):
    return ProviderItemResult("chrome", item_id, PhaseEvidence(EvidenceState.FAILED, "profile missing"),
                              PhaseEvidence(EvidenceState.SKIPPED, "none"), PhaseEvidence(EvidenceState.SKIPPED, "none"))


class StartupResultTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root)}))
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        operations.bind(None)
        login_status.initialize("isolated-result-session")
        self.stack.enter_context(patch.object(cli, "_startup_directory", return_value=self.root))
        self.stack.enter_context(patch.object(cli, "load", return_value={"sessions": [], "desktop": {}}))
        self.stack.enter_context(patch.object(cli, "needs_storage", return_value=False))
        self.stack.enter_context(patch.object(cli, "_publish_workspace_restored"))
        self.wait = self.stack.enter_context(patch.object(cli, "_wait_for_shell"))
        self.args = argparse.Namespace(category=None, dry_run=False, force=False, owns_tmux_restore=True,
                                       wait=0, no_place=False, workspace=None, session=None, select=False)

    def stage(self, identifier):
        data = json.loads(login_status.status_path().read_text())
        return next(item for item in data["stages"] if item["id"] == identifier)

    def restore(self, browsers):
        def run(snapshot, category_args, startup=False):
            if category_args.category == "browsers":
                if isinstance(browsers, BaseException):
                    raise browsers
                return {"browsers": browsers}
            return {category_args.category: 0, "codex_ready": 0, "codex_total": 0, "codex_verified": 1,
                    "virtual_machines_total": 0, "virtual_machines_message": "none"}
        return patch.object(cli, "_restore", side_effect=run)

    def test_partly_failed_category_is_degraded_and_startup_exits_zero(self):
        results = [verified("Default/one"), failed("Default/two")]
        with self.restore(ProviderCount(1, results)), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        stage = self.stage("browsers")
        self.assertEqual(stage["state"], "degraded")
        self.assertIn("1 of 2 item(s) need attention", stage["message"])
        self.assertIn("profile missing", stage["message"])
        # The marker keeps a repeated startup from retrying completed work.
        self.assertTrue((self.root / "browsers.done").exists())

    def test_raised_category_job_stays_failed_and_startup_exits_zero(self):
        with self.restore(RuntimeError("companion unavailable")), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        self.assertEqual(self.stage("browsers")["state"], "failed")
        self.assertIn("companion unavailable", self.stage("browsers")["message"])
        self.assertTrue((self.root / "browsers.done").exists())

    def test_repeated_startup_skips_completed_degraded_category(self):
        results = [verified("Default/one"), failed("Default/two")]
        with self.restore(ProviderCount(1, results)) as restore, redirect_stdout(io.StringIO()):
            cli.cmd_startup(self.args)
            restore.reset_mock()
            self.assertEqual(cli.cmd_startup(self.args), 0)
        restore.assert_not_called()
        self.assertEqual(self.stage("browsers")["state"], "degraded")

    def test_orchestration_failure_still_raises(self):
        self.wait.side_effect = RuntimeError("GNOME placement capabilities unavailable")
        with self.restore(0), self.assertRaises(RuntimeError):
            cli.cmd_startup(self.args)
        self.assertEqual(self.stage("workspace")["state"], "failed")

    def test_restore_jobs_failed_is_gone(self):
        self.assertFalse(hasattr(cli, "RestoreJobsFailed"))


class NeedsAttentionStaysPendingTests(unittest.TestCase):
    def test_unrestored_session_and_its_conversation_stay_pending_through_the_carry(self):
        from test_pending_items import CODEX, LATER, carry, pane, session, snapshot, window
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX))))
        # The live "main" has another layout, so nothing was restored or seen.
        current = snapshot(created_at=LATER)
        carry(current, previous, seen=set())
        self.assertIn("session:main", current["pending"])
        self.assertIn(f"codex:{CODEX}", current["pending"])
        self.assertEqual([item["name"] for item in current["sessions"]], ["main"])


class SettlingStartupTests(unittest.TestCase):
    """Real publish, marker, observation and repeated-startup path."""

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}))
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        operations.bind(None)
        login_status.initialize("isolated-settling-session")
        for name in ("needs_storage", "needs_vscode_storage"):
            self.stack.enter_context(patch.object(cli, name, return_value=False))
        self.stack.enter_context(patch.object(cli, "load", return_value={"sessions": [], "desktop": {}}))
        self.stack.enter_context(patch.object(cli, "_publish_workspace_restored"))
        self.stack.enter_context(patch.object(cli, "_wait_for_shell"))
        self.args = argparse.Namespace(category=None, dry_run=False, force=False, owns_tmux_restore=True,
                                       wait=0, no_place=False, workspace=None, session=None, select=False)

    def stage(self, identifier):
        data = json.loads(login_status.status_path().read_text())
        return next(item for item in data["stages"] if item["id"] == identifier)

    def marker_state(self, category):
        return json.loads(cli._startup_marker(category).read_text())["state"]

    def test_waiting_with_a_failed_session_settles_to_degraded_and_survives_a_repeat(self):
        waiting = PhaseEvidence(EvidenceState.WAITING, "placement pending", True, "probe-token")
        skipped = PhaseEvidence(EvidenceState.SKIPPED, "none")
        results = [ProviderItemResult("alacritty", "work/2", verified("x").identity, verified("x").content, waiting),
                   ProviderItemResult("tmux", "main", PhaseEvidence(EvidenceState.FAILED, "tmux session main is in use"),
                                      skipped, skipped)]

        def run(snapshot, category_args, startup=False):
            if category_args.category == "terminals":
                return {"terminals": ProviderCount(1, results), "codex_ready": 1, "codex_total": 1,
                        "codex_verified": 1, "codex_deferred": 0}
            return {category_args.category: 0, "virtual_machines_total": 0, "virtual_machines_message": "none",
                    "virtual_machines_already_completed": False}

        with patch.object(cli, "_restore", side_effect=run) as restore, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
            self.assertEqual(self.stage("terminals")["state"], "waiting")
            self.assertEqual(self.marker_state("terminals"), "waiting")
            with patch.object(provider_progress, "_query_request",
                              side_effect=lambda token, remaining: {"token": token, "status": "verified"}):
                provider_progress.reconcile_pending()
            self.assertEqual(self.stage("terminals")["state"], "degraded")
            self.assertEqual(self.marker_state("terminals"), "attempted")
            restore.reset_mock()
            self.assertEqual(cli.cmd_startup(self.args), 0)
            restore.assert_not_called()
        self.assertEqual(self.stage("terminals")["state"], "degraded")
        self.assertNotEqual(self.stage("codex")["state"], "failed")
        self.assertNotEqual(json.loads(login_status.status_path().read_text())["overall_state"], "failed")

    def test_waiting_stage_with_a_failed_item_keeps_the_failure_visible_to_the_finalizer(self):
        from workspace_state import login_finalize
        native = PhaseEvidence(EvidenceState.WAITING, "handoff", True, "native-handoff:probe")
        skipped = PhaseEvidence(EvidenceState.SKIPPED, "none")
        results = [ProviderItemResult("alacritty", "work/2", verified("x").identity, verified("x").content, native),
                   ProviderItemResult("tmux", "main", PhaseEvidence(EvidenceState.FAILED, "tmux session main is in use"),
                                      skipped, skipped)]

        def run(snapshot, category_args, startup=False):
            if category_args.category == "terminals":
                return {"terminals": ProviderCount(1, results), "codex_ready": 1, "codex_total": 1,
                        "codex_verified": 1, "codex_deferred": 0}
            return {category_args.category: 0, "virtual_machines_total": 0, "virtual_machines_message": "none",
                    "virtual_machines_already_completed": False}

        with patch.object(cli, "_restore", side_effect=run), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        stage = self.stage("terminals")
        self.assertEqual(stage["state"], "waiting")
        self.assertIn("1 of 2 item(s) need attention", stage["message"])
        self.assertIn("tmux session main is in use", stage["message"])
        self.assertIn("waiting for placement of the rest", stage["message"])
        self.assertEqual(login_finalize._attention_startup_stages(), ["terminals"])
        # GNOME keeps deferring the handoff: the finalizer still reports the failure.
        updates = []
        with ExitStack() as stack:
            for name in ("_check_operation", "finish", "set_overall", "fail_active", "_wait_pending_providers"):
                stack.enter_context(patch.object(login_finalize, name))
            stack.enter_context(patch.object(login_finalize, "update_stage",
                                             side_effect=lambda *a, **k: updates.append(a)))
            for name in ("finish_deferred_file_manager", "finish_deferred_vscode", "finish_deferred_codex",
                         "_warm_cloud_metadata"):
                stack.enter_context(patch.object(login_finalize, name, return_value=True))
            stack.enter_context(patch.object(login_finalize, "_start_drives", return_value={}))
            self.assertEqual(login_finalize._finalize(), 0)
        self.assertEqual(updates[-1][:2], ("login-finalization", "ready"))
        self.assertIn("items need attention: terminals", updates[-1][2])
        self.assertNotIn("verified", updates[-1][2])
        # Once the handoff settles the stage is degraded.
        with patch.object(provider_progress, "_query_request",
                          side_effect=lambda token, remaining: {"token": token, "status": "verified"}):
            provider_progress.reconcile_pending()
        self.assertEqual(self.stage("terminals")["state"], "degraded")

    def test_clean_waiting_stage_is_not_an_attention_stage(self):
        from workspace_state import login_finalize
        native = PhaseEvidence(EvidenceState.WAITING, "handoff", True, "native-handoff:probe")
        item = ProviderItemResult("alacritty", "work/2", verified("x").identity, verified("x").content, native)

        def run(snapshot, category_args, startup=False):
            if category_args.category == "terminals":
                return {"terminals": ProviderCount(1, [item]), "codex_ready": 0, "codex_total": 0,
                        "codex_verified": 1, "codex_deferred": 0}
            return {category_args.category: 0, "virtual_machines_total": 0, "virtual_machines_message": "none",
                    "virtual_machines_already_completed": False}

        with patch.object(cli, "_restore", side_effect=run), redirect_stdout(io.StringIO()):
            cli.cmd_startup(self.args)
        self.assertEqual(self.stage("terminals")["state"], "waiting")
        self.assertNotIn("need attention", self.stage("terminals")["message"])
        self.assertEqual(login_finalize._attention_startup_stages(), [])

    def browsers(self, error):
        self.args.category = "browsers"
        with patch.object(cli, "_restore_browsers", side_effect=error), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)

    def test_browsers_with_some_failed_items_are_degraded_through_the_real_path(self):
        self.browsers(ProviderRestoreError("Chrome restore needs attention", [verified("Default/one"), failed("Default/two")]))
        stage = self.stage("browsers")
        self.assertEqual(stage["state"], "degraded")
        self.assertIn("1 of 2 item(s) need attention", stage["message"])
        self.assertEqual(self.marker_state("browsers"), "attempted")

    def test_browsers_with_no_verified_item_are_failed(self):
        self.browsers(ProviderRestoreError("Chrome restore needs attention", [failed("Default/one")]))
        self.assertEqual(self.stage("browsers")["state"], "failed")

    def test_browsers_exception_without_results_is_failed(self):
        self.browsers(RuntimeError("companion unavailable"))
        self.assertEqual(self.stage("browsers")["state"], "failed")

    def test_manual_restore_with_attention_items_exits_three(self):
        counts = {category: 0 for category in cli.CATEGORIES}
        counts["terminals"] = ProviderCount(1, [verified("a"), failed("main")])
        args = argparse.Namespace(category="terminals")
        with patch.object(cli, "_restore", return_value=counts), patch.object(cli, "load", return_value={}), \
                redirect_stderr(io.StringIO()):
            self.assertEqual(cli.cmd_restore(args), 3)


class SharedDeadlineTests(unittest.TestCase):
    KEY = "codex:saved-id"

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")}))
        self.directory = self.root / "startup"
        self.directory.mkdir()
        self.stack.enter_context(patch.object(cli, "_startup_directory", return_value=self.directory))
        self.stack.enter_context(patch.object(cli, "_boot_id", return_value="test-boot"))
        self.clock = FakeTime()
        self.stack.enter_context(patch.object(cli, "time", self.clock))
        self.update = self.stack.enter_context(patch.object(cli, "update_stage"))
        self.stack.enter_context(patch.object(cli, "_record_seen"))
        self.stack.enter_context(patch.object(cli, "_marker_context", return_value=None))
        self.stack.enter_context(patch.object(cli, "_live_terminal_clients", return_value={}))
        self.stack.enter_context(patch.object(cli, "recreate_tmux", return_value=("work", [])))
        self.stack.enter_context(patch.object(cli, "waiting_directory_ids", return_value=set()))
        self.stack.enter_context(patch.object(cli, "pending_start_ids", return_value=set()))
        from workspace_state import conversation_guard
        # Verification would otherwise query a tmux server.
        self.stack.enter_context(patch.object(conversation_guard, "live_sessions", return_value={}))
        self.missing = self.stack.enter_context(
            patch.object(cli, "_missing_conversations", return_value={self.KEY}))
        self.path = self.directory / "conversation-deadline.json"
        self.snapshot = {"sessions": [{"name": "work", "launch_terminal": False, "windows": [{
            "index": 1, "panes": [{"index": 1, "codex": {"session_id": "saved-id"}}]}]}], "terminals": []}

    def args(self, **kwargs):
        values = dict(workspace=None, session=None, select=False, dry_run=False, no_place=False,
                      repair_processes=False, adopt_restored=True, verify_codex=True, wait=120,
                      login_status=True)
        values.update(kwargs)
        return argparse.Namespace(**values)

    def restore_terminals(self, **kwargs):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli._restore_terminals(self.snapshot, self.args(**kwargs))

    def codex_states(self):
        return [call.args[1] for call in self.update.call_args_list if call.args[0] == "codex"]

    def test_first_use_creates_the_deadline_and_later_use_reuses_it(self):
        self.assertFalse(self.path.exists())
        first = cli._conversation_deadline()
        self.assertEqual(first, 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)
        self.assertEqual(cli.CONVERSATION_DEADLINE_SECONDS, 180.0)
        self.assertTrue(self.path.exists())
        self.clock.now = 150.0
        self.assertEqual(cli._conversation_deadline(), first)
        self.assertEqual(list(self.directory.glob("conversation-deadline.json.*")), [])

    def test_a_separate_process_reuses_the_deadline(self):
        environment = dict(os.environ, PYTHONPATH=str(SRC))
        script = ("from pathlib import Path\nfrom unittest.mock import patch\n"
                  "from workspace_state import cli\n"
                  "with patch.object(cli, '_startup_directory', return_value=Path(%r)), "
                  "patch.object(cli, '_boot_id', return_value='test-boot'):\n"
                  "    print(repr(cli._conversation_deadline()))\n" % str(self.directory))
        with patch.object(cli, "time", real_time):
            created = float(subprocess.run([sys.executable, "-c", script], env=environment, cwd=self.root,
                                           capture_output=True, text=True, check=True).stdout)
            self.assertEqual(cli._conversation_deadline(), created)
        self.assertLessEqual(created - real_time.monotonic(), cli.CONVERSATION_DEADLINE_SECONDS)

    def test_damaged_or_foreign_file_never_extends_the_bound(self):
        self.path.write_text(json.dumps({"deadline": 10 ** 9, "boot_id": "test-boot"}))
        self.assertEqual(cli._conversation_deadline(), 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)
        self.path.write_text(json.dumps({"deadline": 10 ** 9, "boot_id": "another-boot"}))
        self.assertLessEqual(cli._conversation_deadline(), 100.0)
        self.path.write_text("{broken")
        self.assertLessEqual(cli._conversation_deadline(), 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)

    def test_startup_restore_waits_when_only_its_own_bound_ends(self):
        outcome = self.restore_terminals()
        self.assertTrue(self.path.exists())
        self.assertTrue(outcome.codex_deferred)
        self.assertFalse(outcome.codex_verified)
        self.assertEqual(self.codex_states()[-1], "waiting")
        self.assertIn("verification continues", self.update.call_args_list[-1].args[2])
        self.assertNotIn("degraded", self.codex_states())
        self.assertLess(self.clock.now, 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)

    def test_startup_restore_is_degraded_after_the_shared_deadline(self):
        self.path.write_text(json.dumps({"deadline": 90.0, "boot_id": "test-boot"}))
        outcome = self.restore_terminals()
        self.assertFalse(outcome.codex_deferred)
        self.assertEqual(self.codex_states()[-1], "degraded")

    def test_startup_restore_ends_at_the_shared_deadline_when_it_is_earlier(self):
        self.path.write_text(json.dumps({"deadline": 105.0, "boot_id": "test-boot"}))
        outcome = self.restore_terminals()
        self.assertFalse(outcome.codex_deferred)
        self.assertEqual(self.codex_states()[-1], "degraded")
        self.assertLessEqual(self.clock.now, 106.0)

    def test_verified_conversations_finish_without_waiting(self):
        self.missing.return_value = set()
        outcome = self.restore_terminals()
        self.assertTrue(outcome.codex_verified)
        self.assertFalse(outcome.codex_deferred)
        self.assertEqual(self.codex_states()[-1], "ready")

    def test_manual_restore_keeps_its_bounded_wait_and_writes_no_deadline(self):
        outcome = self.restore_terminals(login_status=False)
        self.assertFalse(self.path.exists())
        self.assertFalse(outcome.codex_verified)
        self.assertFalse(outcome.codex_deferred)
        self.assertEqual(self.clock.now, 115.0)  # max(15, 5 * conversations)

    def finalize(self):
        session_id = "22222222-2222-4222-8222-222222222222"
        self.snapshot["sessions"][0]["windows"][0]["panes"] = [{"index": 1, "conversation": {
            "provider": "claude", "session_id": session_id, "persisted": True}}]
        status = self.root / "status.json"
        status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        self.missing.return_value = {"claude:" + session_id}
        with patch.object(cli, "status_path", return_value=status), \
                patch.object(cli, "load", return_value=self.snapshot):
            return cli.finish_deferred_codex()

    def test_finalizer_waits_until_the_shared_deadline_then_degrades(self):
        snapshots = []
        original = self.update.side_effect

        def record(*args, **kwargs):
            snapshots.append((args[1], self.clock.now))
        self.update.side_effect = record
        self.assertFalse(self.finalize())
        self.assertEqual(snapshots[0][0], "waiting")
        self.assertLess(snapshots[0][1], 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)
        self.assertEqual(snapshots[-1][0], "degraded")
        self.assertGreaterEqual(snapshots[-1][1], 100.0 + cli.CONVERSATION_DEADLINE_SECONDS)
        self.assertEqual([state for state, _ in snapshots[:-1]], ["waiting"])
        self.assertIsNone(original)

    def test_finalizer_shares_the_deadline_used_by_the_restore(self):
        self.path.write_text(json.dumps({"deadline": 101.0, "boot_id": "test-boot"}))
        self.assertFalse(self.finalize())
        self.assertLess(self.clock.now, 103.0)
        self.assertEqual(self.update.call_args.args[1], "degraded")

    def test_finalizer_started_after_the_deadline_still_completes_the_stability_window(self):
        self.path.write_text(json.dumps({"deadline": 90.0, "boot_id": "test-boot"}))
        session_id = "22222222-2222-4222-8222-222222222222"
        self.snapshot["sessions"][0]["windows"][0]["panes"] = [{"index": 1, "conversation": {
            "provider": "claude", "session_id": session_id, "persisted": True}}]
        status = self.root / "status.json"
        status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        self.missing.return_value = set()
        with patch.object(cli, "status_path", return_value=status), patch.object(cli, "load", return_value=self.snapshot):
            self.assertTrue(cli.finish_deferred_codex())
        self.assertEqual(self.update.call_args.args[:2], ("codex", "ready"))
        self.assertGreaterEqual(self.clock.now, 100.0 + cli.CODEX_STABLE_SECONDS)

    def test_conversation_that_disappears_after_the_deadline_degrades_without_hanging(self):
        self.path.write_text(json.dumps({"deadline": 90.0, "boot_id": "test-boot"}))
        session_id = "22222222-2222-4222-8222-222222222222"
        self.snapshot["sessions"][0]["windows"][0]["panes"] = [{"index": 1, "conversation": {
            "provider": "claude", "session_id": session_id, "persisted": True}}]
        status = self.root / "status.json"
        status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        answers = iter([set(), {"claude:" + session_id}])
        self.missing.side_effect = lambda *args: next(answers, {"claude:" + session_id})
        with patch.object(cli, "status_path", return_value=status), patch.object(cli, "load", return_value=self.snapshot):
            self.assertFalse(cli.finish_deferred_codex())
        self.assertEqual(self.update.call_args.args[1], "degraded")
        self.assertLess(self.clock.now, 110.0)

    def test_startup_restore_with_all_present_finishes_stability_after_the_deadline(self):
        self.path.write_text(json.dumps({"deadline": 90.0, "boot_id": "test-boot"}))
        self.missing.return_value = set()
        outcome = self.restore_terminals()
        self.assertTrue(outcome.codex_verified)
        self.assertEqual(self.codex_states()[-1], "ready")

    def test_finalizer_does_not_wait_for_sessions_that_need_attention(self):
        claude = "33333333-3333-4333-8333-333333333333"
        self.snapshot["sessions"].insert(0, {"name": "main", "launch_terminal": False, "windows": [{
            "index": 0, "panes": [{"index": 0, "conversation": {
                "provider": "claude", "session_id": claude, "persisted": True}}]}]})
        write_stage_marker(self.directory / "terminals.done", StageMarker(
            "terminals", "attempted", "", "", None,
            (failed("main").to_dict() | {"provider": "tmux"},)))
        seen_names = []

        def missing(pairs, context):
            seen_names.extend(name for _session, name in pairs)
            return set()
        self.missing.side_effect = missing
        status = self.root / "status.json"
        status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        with patch.object(cli, "status_path", return_value=status), patch.object(cli, "load", return_value=self.snapshot), \
                patch.object(cli, "_startup_marker", side_effect=lambda category: self.directory / f"{category}.done"):
            self.assertFalse(cli.finish_deferred_codex())
        self.assertEqual(set(seen_names), {"work"})
        self.assertLess(self.clock.now, 110.0)
        last = self.update.call_args
        self.assertEqual(last.args[:2], ("codex", "degraded"))
        self.assertIn("1 conversation(s) remain pending in sessions that need attention", last.args[2])
        self.assertNotIn("check the remaining tmux panes", last.args[2])
        self.assertEqual(last.kwargs["current"], 1)


if __name__ == "__main__":
    unittest.main()
