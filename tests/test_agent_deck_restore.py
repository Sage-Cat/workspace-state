import argparse
from contextlib import redirect_stdout
from io import StringIO
import os
import time
import unittest
from unittest.mock import patch

from workspace_state import agent_deck, agent_deck_restore, cli, pending, restore, storage, tmux_server
from workspace_state.checkpoint import category_digest


UUID = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
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
    return agent_deck_restore.restore(
        snapshot, items or [], deadline=time.monotonic() + 3, **options,
    )


class AgentDeckRestoreTest(unittest.TestCase):
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
