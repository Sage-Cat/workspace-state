import argparse
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import agent_deck, agent_deck_restore, cli, pending, restore, storage, tmux_server
from workspace_state.checkpoint import CURRENT_VERSION, category_digest, record_provenance


UUID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
STANDALONE = "00000000-0000-4000-8000-000000000003"
PANEL = {"profile": "work", "group": "project"}


def row(**changes):
    return dict(
        {"profile": "work", "group": "project", "id": "record-1", "tool": "claude",
         "native_id": UUID, "account": "example", "title": "Example", "open": True,
         "archived": False, "stopped": False},
        **changes,
    )


def deck(*rows):
    return {"version": 1, "sessions": list(rows)}


def pane(**changes):
    return dict({"index": 0, "cwd": "/tmp", "command": "zsh"}, **changes)


def window(index, *panes, name="editor"):
    return {"index": index, "name": name, "layout": "", "panes": list(panes)}


def sessions(*windows):
    return [{"name": "Example", "launch_terminal": True, "windows": list(windows)}]


def restore_deck(snapshot, items=None, **options):
    options.setdefault("start", True)
    return agent_deck_restore.restore(
        snapshot, items or [], deadline=time.monotonic() + 3, **options,
    )


def captured_deck(*rows, standalone=False):
    snapshot = {
        "version": CURRENT_VERSION, "created_at": "now",
        "sessions": sessions(window(0, pane(agent_deck=PANEL))), "agent_deck": deck(*rows),
    }
    if standalone:
        snapshot["sessions"][0]["windows"].append(window(3, pane(codex={"session_id": STANDALONE})))
    before = {"sessions": sessions(
        window(1, pane(conversation={"provider": "claude", "session_id": UUID, "persisted": True})),
        window(2, pane(codex={"session_id": OTHER})),
    )}
    pending.carry(snapshot, before, fresh={"terminals"}, seen=set(), now="before")
    record_provenance(snapshot, None, source="auto-save")
    storage.validate(snapshot)
    return snapshot


class AgentDeckRestoreTest(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.dict(os.environ, {"XDG_CONFIG_HOME": str(directory)}))
        self.config = directory / "workspace-state/agent-deck.toml"
        self.config.parent.mkdir()

    def test_capture_records_dashboard_and_inventory_in_same_category(self):
        context = unittest.mock.Mock(shell={}, names=[])
        context.evidence.return_value = {}
        layout = {"created_at": "now", "sessions": sessions(window(0, pane(agent_deck=PANEL)))}
        with patch("workspace_state.checkpoint.CaptureContext.begin", return_value=context), \
                patch.object(cli, "capture", return_value=layout) as capture, \
                patch.object(agent_deck, "enabled", return_value=True), \
                patch.object(agent_deck, "inventory", return_value=deck(row())):
            result = cli._capture_all(only={"terminals"}, browsers=False)
        capture.assert_called_once_with(shell={}, names=[], deck_routes=set())
        self.assertEqual(result["agent_deck"], deck(row()))
        self.assertNotEqual(category_digest(result, "terminals"),
                            category_digest({**result, "agent_deck": deck(row(open=False))}, "terminals"))

    def test_partial_capture_retains_old_layout_and_deck_inventory(self):
        previous = {"sessions": sessions(window(0, pane(agent_deck=PANEL))), "agent_deck": deck(row())}
        current = {"agent_deck": deck()}
        self.assertTrue(cli._retain_last_good(current, previous, "terminals", ["unavailable"]))
        self.assertEqual(current, previous)
        self.assertIsNot(current["agent_deck"], previous["agent_deck"])

    def test_saved_dashboard_preserves_shell_after_exit(self):
        with patch.object(agent_deck, "dashboard_command", return_value=["env", "agent-deck", "-p", "work"]), \
                patch.dict(os.environ, {"SHELL": "/bin/sh"}):
            self.assertEqual(restore._pane_shell_command(pane(agent_deck=PANEL)),
                             "env agent-deck -p work; exec /bin/sh")

    def test_legacy_ai_layout_reuses_saved_dashboard_and_keeps_shells(self):
        legacy = pane(command="codex", codex={"session_id": UUID})
        original = sessions(window(4, legacy), window(5, pane(agent_deck=PANEL)), window(6, pane()))
        result = agent_deck_restore.terminal_layout(original)
        windows = result[0]["windows"]
        self.assertEqual([item["index"] for item in windows], [5, 6])
        self.assertEqual(windows[0]["panes"][0]["agent_deck"], PANEL)
        self.assertEqual(windows[1], original[0]["windows"][2])
        self.assertIn("codex", original[0]["windows"][0]["panes"][0])
        previous = agent_deck_restore.terminal_layout(sessions(window(4, legacy)))
        self.assertNotIn("codex", previous[0]["windows"][0]["panes"][0])
        self.assertNotIn("agent_deck", previous[0]["windows"][0]["panes"][0])

    def test_foreign_dashboard_cannot_prove_reuse(self):
        saved = sessions(window(0, pane(agent_deck=PANEL)))[0]
        state = {0: {"name": "editor", "panes": {0: {"pid": 1}}}}
        with patch.object(agent_deck, "dashboard", return_value={"profile": "other", "group": "project"}):
            self.assertFalse(restore._same_tmux_session(saved, state))
        with patch.object(agent_deck, "dashboard", return_value=PANEL):
            self.assertTrue(restore._same_tmux_session(saved, state))

    def test_exact_recovery_verifies_live_identity_after_start(self):
        saved = {"agent_deck": deck(row(), row(id="inactive", native_id=OTHER, open=False))}
        with patch.object(agent_deck, "inventory", side_effect=[deck(row(open=False)), deck(row())]), \
                patch.object(agent_deck, "start_exact") as launch, \
                patch.object(agent_deck_restore.conversation_guard, "live_locations", return_value={}):
            result = restore_deck(saved)
        self.assertTrue(result.verified)
        self.assertEqual(result.ready, (row(),))
        self.assertEqual(launch.call_args.args, (row(open=False),))

    def test_reuses_open_rows_and_refuses_rebound_or_ambiguous_ids(self):
        saved = {"agent_deck": deck(row())}
        for current, expected in (
            (deck(row()), True),
            (deck(row(native_id=OTHER)), False),
            (deck(row(), row(profile="other")), False),
            (deck(row(open=False, archived=True)), False),
            (deck(row(open=False, stopped=True)), False),
        ):
            with self.subTest(current=current), patch.object(agent_deck, "inventory", return_value=current), \
                    patch.object(agent_deck, "start_exact") as launch:
                self.assertEqual(restore_deck(saved).verified, expected)
            launch.assert_not_called()

    def test_inactive_restore_starts_nothing_and_leaves_stopped_rows(self):
        saved = {"agent_deck": deck(row(), row(id="r2", native_id=OTHER), row(id="r3", native_id=None))}
        current = deck(row(), row(id="r2", native_id=OTHER, open=False),
                       row(id="r3", native_id=None, open=False))
        with patch.object(agent_deck, "inventory", return_value=current), \
                patch.object(agent_deck, "start_exact") as launch, \
                patch.object(agent_deck_restore.conversation_guard, "live_locations") as guard:
            result = restore_deck(saved, start=False)
        launch.assert_not_called()
        guard.assert_not_called()
        self.assertTrue(result.verified)
        self.assertEqual(result.ready, (row(),))
        self.assertEqual(len(result.stopped), 2)
        self.assertEqual(result.pending, ())
        self.assertEqual(result.total, 3)

    def test_unmanaged_live_client_prevents_duplicate_start(self):
        with patch.object(agent_deck, "inventory", return_value=deck(row(open=False))), \
                patch.object(agent_deck, "start_exact") as launch, \
                patch.object(agent_deck_restore.conversation_guard, "live_locations",
                             return_value={f"claude:{UUID}": ["terminal"]}):
            self.assertFalse(restore_deck({"agent_deck": deck(row())}).verified)
        launch.assert_not_called()

    def test_missing_guard_leaves_record_pending_without_unprotected_start(self):
        with patch.object(agent_deck, "inventory", return_value=deck(row(open=False))), \
                patch.object(agent_deck, "start_exact", side_effect=agent_deck.AgentDeckError("unsupported guard")
                             ) as launch, \
                patch.object(agent_deck_restore.conversation_guard, "live_locations", return_value={}):
            result = restore_deck({"agent_deck": deck(row())})
        self.assertFalse(result.verified)
        self.assertEqual(result.detail, "unsupported guard")
        launch.assert_called_once()

    def test_restricted_restore_uses_exact_dashboard_group_and_profile(self):
        saved = {"agent_deck": deck(row(), row(id="record-2", native_id=OTHER, group="unrelated"))}
        target = sessions(window(0, pane(agent_deck=PANEL)))
        with patch.object(agent_deck, "inventory", return_value=saved["agent_deck"]):
            result = restore_deck(saved, target, restricted=True)
        self.assertEqual(result.ready, (row(),))
        with patch.object(agent_deck, "inventory", return_value=deck(row(group="moved", open=False))), \
                patch.object(agent_deck, "start_exact") as launch:
            self.assertFalse(restore_deck(saved, target, restricted=True).verified)
        launch.assert_not_called()

    def test_legacy_identity_is_matched_only_to_a_registered_record(self):
        old = sessions(window(0, pane(command="claude", conversation={
            "provider": "claude", "session_id": UUID, "persisted": True,
        })))
        with patch.object(agent_deck, "inventory", return_value=deck()), \
                patch.object(agent_deck, "start_exact") as launch:
            self.assertEqual(restore_deck({}, old).total, 1)
        launch.assert_not_called()
        with patch.object(agent_deck, "inventory", return_value=deck(row())):
            self.assertTrue(restore_deck({}, old).verified)
            self.assertEqual(restore_deck({"agent_deck": deck()}, old).ready, (row(),))
        with patch.object(agent_deck, "inventory", return_value=deck(row(group="other"))), \
                patch.object(agent_deck, "start_exact") as launch:
            self.assertFalse(restore_deck({}, old, restricted=True).verified)
        launch.assert_not_called()

    def test_obsolete_native_pending_remains_in_checkpoint_outside_managed_readiness(self):
        snapshot = captured_deck()
        previous = deepcopy(snapshot)
        with patch.object(agent_deck, "inventory", return_value=deck()), \
                patch.object(agent_deck, "start_exact") as launch:
            result = restore_deck(snapshot, snapshot["sessions"])
        self.assertTrue(result.verified)
        self.assertEqual((result.total, result.ready, result.pending, result.legacy_retained), (0, (), (), 2))
        self.assertEqual(snapshot, previous)
        self.assertEqual({item["key"] for item in pending.describe(snapshot)},
                         {f"claude:{UUID}", f"codex:{OTHER}"})
        launch.assert_not_called()

    def test_registered_only_leaves_all_three_unregistered_ids_for_separate_recovery(self):
        snapshot = captured_deck(standalone=True)
        original = deepcopy(snapshot)
        for setting, total, retained in ((None, 1, 2), ("false", 1, 2), ("true", 0, 3)):
            if setting is not None:
                self.config.write_text(f"registered_only = {setting}\n")
            with self.subTest(setting=setting), patch.object(agent_deck, "inventory", return_value=deck()), \
                    patch.object(agent_deck, "start_exact") as launch:
                result = restore_deck(snapshot, snapshot["sessions"])
                self.assertEqual((result.total, result.legacy_retained), (total, retained))
                self.assertEqual(result.verified, total == 0)
                self.assertEqual(result.ready, ())
                self.assertEqual(snapshot, original)
                launch.assert_not_called()

        fresh = {"sessions": sessions(window(0, pane(agent_deck=PANEL))), "agent_deck": deck()}
        pending.carry(fresh, snapshot, fresh={"terminals"}, seen=set(), now="next")
        storage.validate(fresh)
        self.assertEqual({item["key"] for item in pending.describe(fresh)},
                         {f"claude:{UUID}", f"codex:{OTHER}", f"codex:{STANDALONE}"})

    def test_registered_only_is_a_boolean_setting(self):
        self.config.write_text('registered_only = "true"\n')
        snapshot = captured_deck(standalone=True)
        with patch.object(agent_deck, "inventory", return_value=deck()), \
                self.assertRaisesRegex(RuntimeError, "Expected a boolean registered_only"):
            restore_deck(snapshot, snapshot["sessions"])

    def test_no_capture_proof_or_no_dashboard_preserves_legacy_verification(self):
        self.config.write_text("registered_only = true\n")
        for removed in ("category_provenance", "dashboard", "agent_deck"):
            snapshot = captured_deck()
            if removed == "dashboard":
                snapshot["sessions"][0]["windows"].pop(0)
                record_provenance(snapshot, None, source="auto-save")
            else:
                snapshot.pop(removed)
            if removed == "agent_deck":
                snapshot["version"] = 6
            with self.subTest(removed=removed), patch.object(agent_deck, "inventory", return_value=deck()):
                result = restore_deck(snapshot, snapshot["sessions"])
                self.assertFalse(result.verified)
                self.assertEqual((result.total, result.legacy_retained), (2, 0))

    def test_registry_matches_and_unresolved_open_rows_still_need_verification(self):
        self.config.write_text("registered_only = true\n")
        unresolved = row(id="managed-unknown", native_id=None)
        for saved, live in ((deck(row(open=False)), deck()), (deck(), deck(row(open=False, archived=True)))):
            snapshot = captured_deck(*saved["sessions"], unresolved)
            with self.subTest(saved=saved, live=live), patch.object(agent_deck, "inventory", return_value=live):
                result = restore_deck(snapshot, snapshot["sessions"])
                self.assertFalse(result.verified)
                self.assertEqual((result.total, result.legacy_retained), (2, 1))
                self.assertEqual(result.pending, (unresolved,))

    def test_newly_imported_pending_identity_reconciles_with_current_registry(self):
        self.config.write_text("registered_only = true\n")
        snapshot = captured_deck()
        with patch.object(agent_deck, "inventory", return_value=deck(row())):
            result = restore_deck(snapshot, snapshot["sessions"])
        self.assertTrue(result.verified)
        self.assertEqual((result.total, result.legacy_retained, result.ready), (1, 1, (row(),)))

    def test_unavailable_current_inventory_does_not_downgrade_legacy_to_migration_note(self):
        self.config.write_text("registered_only = true\n")
        snapshot = captured_deck()
        with patch.object(agent_deck, "inventory", side_effect=agent_deck.AgentDeckError("inventory unavailable")):
            result = restore_deck(snapshot, snapshot["sessions"])
        self.assertFalse(result.verified)
        self.assertEqual((result.total, result.legacy_retained, result.detail), (2, 0, "inventory unavailable"))

    def test_pending_restoration_is_carried_until_seen_or_forgotten(self):
        previous = {"agent_deck": deck(row())}
        current = {"agent_deck": deck(row(open=False))}
        pending.carry(current, previous, fresh={"terminals"}, seen=set(), now="now")
        self.assertEqual(current["agent_deck"], previous["agent_deck"])
        storage.validate(current)
        self.assertEqual(pending.forget(current, [pending.deck_item(row())]), {"terminals"})
        self.assertFalse(current["agent_deck"]["sessions"][0]["open"])
        observed = {"agent_deck": deck(row(open=False))}
        pending.carry(observed, previous, fresh={"terminals"}, seen={pending.deck_item(row())}, now="now")
        self.assertNotIn("pending", observed)
        self.assertFalse(observed["agent_deck"]["sessions"][0]["open"])

    def test_explicit_deck_stop_wins_over_previous_open_intent(self):
        previous = {"agent_deck": deck(row())}
        current = {"agent_deck": deck(row(open=False, stopped=True))}
        pending.carry(current, previous, fresh={"terminals"}, seen=set(), now="now")
        self.assertNotIn("pending", current)
        self.assertFalse(current["agent_deck"]["sessions"][0]["open"])

    def test_failed_dashboard_survives_a_capture_with_its_fallback_shell(self):
        previous = {"sessions": sessions(window(0, pane(agent_deck=PANEL), name="Deck"))}
        current = {"sessions": sessions(window(0, pane(), name="Deck"))}
        key = pending.dashboard_item("Example", PANEL)
        pending.carry(current, previous, fresh={"terminals"}, seen=set(), now="now")
        storage.validate(current)
        self.assertEqual(pending.pending_layout(current), (set(), {"Example": {1}}))
        self.assertEqual(agent_deck_restore.scopes(current["sessions"]), [PANEL])
        self.assertEqual(pending.forget(current, [key]), {"terminals"})
        self.assertEqual(current["sessions"], sessions(window(0, pane(), name="Deck")))

    def test_retry_preserves_an_already_open_pending_dashboard(self):
        session = sessions(window(0, pane(agent_deck=PANEL), name="Deck"))[0]
        live = {2: {"panes": {0: {"pid": 1}}}}
        with patch.object(restore, "_tmux_exists", return_value=True), \
                patch.object(restore, "_tmux_state", return_value=live), \
                patch.object(agent_deck, "dashboard", return_value=PANEL), \
                patch.object(restore, "_create_window") as create:
            self.assertEqual(restore._add_pending_windows("Example", session, session["windows"],
                                                          dry_run=False), [])
            self.assertEqual(restore.dashboard_state(session, "Example"),
                             (0, {pending.dashboard_item("Example", PANEL)}))
        create.assert_not_called()

    def test_dry_run_has_no_agent_deck_side_effects(self):
        args = argparse.Namespace(dry_run=True, category="terminals", no_place=True,
                                  workspace=None, session=None, select=False)
        snapshot = {"sessions": sessions(window(0, pane(agent_deck=PANEL))), "agent_deck": deck(row())}
        layout = cli.TerminalRestoreOutcome(1, 0, 0, True)
        with patch.object(cli, "_restore_terminal_layout", return_value=layout) as physical, \
                patch.object(agent_deck, "inventory") as inventory, patch.object(agent_deck, "start_exact") as launch, \
                redirect_stdout(StringIO()):
            outcome = cli._restore_terminals(snapshot, args)
        self.assertEqual(outcome.codex_total, 1)
        self.assertTrue(physical.call_args.kwargs["deck_managed"])
        inventory.assert_not_called()
        launch.assert_not_called()

    def test_deck_terminal_layout_commands_select_the_outer_socket(self):
        args = argparse.Namespace(dry_run=True, no_place=True, workspace=None, session=None, select=False)
        snapshot = {"sessions": [], "agent_deck": deck()}
        command = ["tmux", "list-sessions"]

        def physical(*args, **kwargs):
            self.assertEqual(tmux_server.on_workspace_socket(command), ["tmux", "-L", "default", "list-sessions"])
            return cli.TerminalRestoreOutcome(0, 0, 0, True)

        with patch.object(cli, "_restore_terminal_layout", side_effect=physical), redirect_stdout(StringIO()):
            cli._restore_terminals(snapshot, args)
        self.assertEqual(tmux_server.on_workspace_socket(command), command)


class AgentDeckStartupTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(directory)
        self.enterContext(patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.directory)}))
        self.config = self.directory / "workspace-state/agent-deck.toml"
        self.config.parent.mkdir()
        self.status = self.directory / "status.json"
        self.load = self.enterContext(patch.object(cli, "load"))
        self.update = self.enterContext(patch.object(cli, "update_stage"))
        self.inventory = self.enterContext(patch.object(agent_deck, "inventory"))
        self.seen = self.enterContext(patch.object(cli, "_record_seen"))
        self.mark = self.enterContext(patch.object(agent_deck, "mark_dead_stopped", return_value=(0, "")))
        self.enterContext(patch.object(cli, "status_path", return_value=self.status))
        self.enterContext(patch.object(cli, "_startup_directory", return_value=self.directory))
        self.enterContext(patch.object(cli, "_conversation_deadline", side_effect=lambda: time.monotonic() + 3))
        owner = Mock()
        owner.to_dict.return_value = {"operation": "example"}
        self.enterContext(patch.object(cli, "_marker_context", return_value=owner))
        self.enterContext(patch("workspace_state.workspace_switch.suppress", side_effect=lambda value: value))
        for name in ("_wait_for_shell", "_publish_workspace_attempt_completion", "_publish_workspace_restored",
                     "set_overall", "publish_provider_results", "record_restored_snapshot"):
            self.enterContext(patch.object(cli, name))
        layout = cli.TerminalRestoreOutcome(1, 0, 0, True)
        self.enterContext(patch.object(cli, "_restore_terminal_layout", return_value=layout))
        self.args = argparse.Namespace(
            category="terminals", dry_run=False, force=False, wait=3,
            no_place=False, workspace=None, session=None, select=False,
        )

    def stage(self, name):
        return [call for call in self.update.call_args_list if call.args[0] == name][-1]

    def test_startup_reports_retained_legacy_without_a_false_failed_ai_stage(self):
        self.load.return_value = captured_deck()
        self.inventory.return_value = deck()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        self.assertEqual(self.stage("terminals").args[1], "ready")
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "skipped")
        self.assertEqual(stage.kwargs, {"current": 0, "total": 0})
        self.assertIn("2 unregistered native conversations retained for separate recovery", stage.args[2])
        self.assertIn("2 unregistered native conversations retained for separate recovery", output.getvalue())
        self.assertTrue(all(not call.args[0] for call in self.seen.call_args_list))

    def test_login_leaves_conversations_stopped_without_starting_or_retrying(self):
        managed = row(native_id="00000000-0000-4000-8000-000000000003")
        live = row(id="record-2", native_id=OTHER)
        self.load.return_value = captured_deck(managed, live)
        self.inventory.return_value = deck(row(**{**managed, "open": False}), live)
        self.mark.return_value = (23, "")
        with redirect_stdout(StringIO()), patch.object(agent_deck, "start_exact") as launch:
            self.assertEqual(cli.cmd_startup(self.args), 0)
        launch.assert_not_called()
        self.assertFalse((self.directory / cli.DECK_RETRY_FILE).exists())
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "ready")
        self.assertEqual(stage.kwargs, {"current": 1, "total": 2})
        self.assertIn("1 Agent Deck conversations running; 1 not running (start them from Agent Deck)",
                      stage.args[2])
        self.assertIn("2 unregistered native conversations retained", stage.args[2])
        self.assertIn("23 sessions from the previous boot marked stopped", stage.args[2])

    def test_login_marks_dead_sessions_before_the_dashboards_open(self):
        order = Mock()
        self.mark.side_effect = lambda **kwargs: (order("mark"), (1, ""))[1]
        layout = cli.TerminalRestoreOutcome(1, 0, 0, True)
        self.enterContext(patch.object(cli, "_restore_terminal_layout",
                                       side_effect=lambda *a, **k: (order("layout"), layout)[1]))
        self.load.return_value = captured_deck()
        self.inventory.return_value = deck()
        with redirect_stdout(StringIO()):
            cli.cmd_startup(self.args)
        self.assertEqual([call.args[0] for call in order.call_args_list], ["mark", "layout"])

    def test_exhausted_login_budget_skips_marking_silently(self):
        self.load.return_value = captured_deck()
        self.inventory.return_value = deck()
        with patch.object(cli, "_conversation_deadline", side_effect=lambda: time.monotonic() + 1.5), \
                redirect_stdout(StringIO()):
            cli.cmd_startup(self.args)
        self.mark.assert_not_called()
        self.assertEqual(self.stage("codex").args[1], "skipped")

    def test_legacy_absent_from_agent_deck_is_not_part_of_the_inactive_total(self):
        old = sessions(window(0, pane(agent_deck=PANEL), pane(index=1, claude={"session_id": OTHER})))
        with patch.object(agent_deck, "inventory", return_value=deck(row())):
            result = restore_deck({"agent_deck": deck(row())}, old, start=False)
        self.assertEqual((len(result.ready), len(result.stopped), result.total), (1, 0, 1))

    def test_open_record_without_native_id_counts_as_running(self):
        managed = row(native_id=None)
        self.load.return_value = captured_deck(managed)
        self.inventory.return_value = deck(managed)
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "ready")
        self.assertIn("1 Agent Deck conversations running; 0 not running", stage.args[2])

    def test_login_degrades_with_reason_when_dead_sessions_cannot_be_marked(self):
        self.load.return_value = captured_deck()
        self.inventory.return_value = deck()
        self.mark.return_value = (0, "this Agent Deck build cannot mark sessions")
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "degraded")
        self.assertIn("cannot mark sessions", stage.args[2])

    def test_login_degrades_with_inventory_reason(self):
        self.load.return_value = captured_deck(row())
        self.inventory.side_effect = agent_deck.AgentDeckError("inventory unavailable")
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "degraded")
        self.assertIn("inventory unavailable", stage.args[2])
        self.assertFalse((self.directory / cli.DECK_RETRY_FILE).exists())

    def restore_filtered(self, managed):
        args = argparse.Namespace(**vars(self.args), login_status=False, deck_dashboard=None)
        args.select = True
        with patch.object(cli, "_filtered_terminal_items", return_value=sessions(window(0, pane(agent_deck=PANEL)))), \
                patch.object(agent_deck, "start_exact") as launch, \
                patch.object(agent_deck_restore.conversation_guard, "live_locations", return_value={}), \
                redirect_stdout(StringIO()):
            outcome = cli._restore_terminals(captured_deck(managed), args)
        return outcome, launch

    def test_workspace_on_and_manual_restore_start_nothing_and_verify_by_default(self):
        self.inventory.return_value = deck(row(open=False, stopped=True))
        outcome, launch = self.restore_filtered(row())
        launch.assert_not_called()
        self.assertTrue(outcome.codex_verified)
        self.assertEqual((outcome.codex_ready, outcome.codex_total), (0, 1))
        self.mark.assert_not_called()

    def test_start_conversations_true_restores_guarded_start_and_retry(self):
        self.config.write_text("start_conversations = true\n")
        self.inventory.side_effect = [deck(row(open=False)), deck(row())]
        outcome, launch = self.restore_filtered(row())
        launch.assert_called_once()
        self.assertTrue(outcome.codex_verified)
        self.mark.assert_not_called()

    def test_start_conversations_true_defers_verification_at_login(self):
        self.config.write_text("start_conversations = true\n")
        managed = row(native_id="00000000-0000-4000-8000-000000000003")
        self.load.return_value = captured_deck(managed)
        self.inventory.side_effect = [deck(), deck(managed)]
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        self.mark.assert_not_called()
        self.assertEqual(self.stage("codex").args[1], "waiting")
        receipt = json.loads((self.directory / cli.DECK_RETRY_FILE).read_text())
        self.assertEqual(receipt["legacy_retained"], 2)
        self.status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        with patch.object(pending, "seen_keys", return_value=set()), patch.object(cli, "_checkpoint_login", return_value=None):
            self.assertTrue(cli.finish_deferred_codex())
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "ready")
        self.assertEqual(stage.kwargs, {"current": 1, "total": 1})
        self.assertIn("2 unregistered native conversations retained for separate recovery", stage.args[2])

    def test_start_conversations_true_preserves_registered_only_degradation(self):
        self.config.write_text("registered_only = true\nstart_conversations = true\n")
        managed = row(native_id=None)
        self.load.return_value = captured_deck(managed, standalone=True)
        self.inventory.return_value = deck(managed)
        with redirect_stdout(StringIO()):
            self.assertEqual(cli.cmd_startup(self.args), 0)
        self.assertEqual(self.stage("terminals").args[1], "ready")
        self.assertEqual(self.stage("codex").kwargs, {"current": 0, "total": 1})
        self.status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        with patch.object(pending, "seen_keys", return_value=set()), patch.object(cli, "_checkpoint_login", return_value=None):
            self.assertFalse(cli.finish_deferred_codex())
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "degraded")
        self.assertEqual(stage.kwargs, {"current": 0, "total": 1})
        self.assertIn("3 unregistered native conversations retained for separate recovery", stage.args[2])

    def test_invalid_configuration_stays_inactive_and_degrades_login_with_reason(self):
        self.config.write_text("unknown = true\n")
        self.load.return_value = captured_deck(row())
        self.inventory.return_value = deck(row(open=False))
        error = StringIO()
        with redirect_stdout(StringIO()), patch("sys.stderr", error), \
                patch.object(agent_deck, "start_exact") as launch:
            self.assertEqual(cli.cmd_startup(self.args), 0)
        launch.assert_not_called()
        self.mark.assert_not_called()
        stage = self.stage("codex")
        self.assertEqual(stage.args[1], "degraded")
        self.assertIn("Invalid Agent Deck restore configuration", stage.args[2])
        self.assertIn("Invalid Agent Deck restore configuration", error.getvalue())

    def test_invalid_configuration_does_not_fail_manual_restore(self):
        self.config.write_text("start_conversations = [\n")
        self.inventory.return_value = deck(row(open=False))
        outcome, launch = self.restore_filtered(row())
        launch.assert_not_called()
        self.assertTrue(outcome.codex_verified)
        self.assertIn("Cannot read Agent Deck restore configuration", outcome.codex_alert)

    def test_deferred_retry_follows_the_current_setting(self):
        self.config.write_text("bad = 1\n")
        managed = row()
        self.load.return_value = captured_deck(managed)
        self.status.write_text(json.dumps({"stages": [{"id": "codex", "state": "waiting"}]}))
        receipt = {"operation": {"operation": "example"}, "agent_deck": deck(managed), "total": 1,
                   "ready": 0, "legacy_retained": 0}
        (self.directory / cli.DECK_RETRY_FILE).write_text(json.dumps(receipt))
        self.inventory.return_value = deck(row(open=False))
        with patch.object(pending, "seen_keys", return_value=set()), \
                patch.object(cli, "_checkpoint_login", return_value=None), \
                patch.object(agent_deck, "start_exact") as launch:
            cli.finish_deferred_codex()
        launch.assert_not_called()

    def test_restore_requires_an_explicit_start_policy(self):
        with self.assertRaises(TypeError):
            agent_deck_restore.restore({}, [], deadline=time.monotonic() + 1)

    def test_start_conversations_is_a_boolean_setting(self):
        self.config.write_text('start_conversations = "yes"\n')
        with self.assertRaisesRegex(RuntimeError, "Expected a boolean registered_only or start_conversations"):
            agent_deck_restore.start_conversations()


def tearDownModule():
    # Startup code binds the operation context itself; it must not outlive this module.
    from workspace_state import operations
    operations.bind(None)
