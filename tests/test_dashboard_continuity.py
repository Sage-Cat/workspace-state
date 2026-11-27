"""A dashboard tab keeps its scope across captures and is never reported as restored by a shell."""
from __future__ import annotations

import argparse
import copy
import unittest
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from unittest.mock import patch

from workspace_state import agent_deck, agent_deck_restore, cli, provider_progress, restore, storage
from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderItemResult

SCOPE = {"profile": "work", "group": "project"}
OTHER = {"profile": "work", "group": "other"}


def pane(index=0, **extra):
    return {"index": index, "id": f"%{index}", "cwd": "/tmp", "command": "zsh", **extra}


def window(index, name, *panes):
    return {"index": index, "name": name, "layout": "", "panes": list(panes) or [pane()]}


def snapshot(*windows, session="Example", deck=True):
    value = {"version": 7, "sessions": [{"name": session, "windows": list(windows)}]}
    if deck:
        value["agent_deck"] = {"version": 1, "sessions": []}
    return value


class CarryDashboardScopeTests(unittest.TestCase):
    def previous(self):
        return snapshot(window(1, "work", pane()), window(3, "DECK", pane(0, agent_deck=dict(SCOPE))))

    def test_scope_unread_by_the_new_capture_is_kept_for_the_same_tab(self):
        # An older release, or a failed read, saved the tab as a plain shell.
        current = snapshot(window(1, "work", pane()), window(3, "DECK", pane(0)))
        carried = agent_deck_restore.carry_dashboards(current, self.previous())
        self.assertEqual(carried, ["Example:3.0"])
        self.assertEqual(current["sessions"][0]["windows"][1]["panes"][0]["agent_deck"], SCOPE)
        self.assertNotIn("agent_deck", current["sessions"][0]["windows"][0]["panes"][0])
        storage.validate(current)
        self.assertEqual(agent_deck_restore.scopes(current["sessions"]), [SCOPE])

    def test_the_kept_scope_is_a_copy_and_a_second_pass_changes_nothing(self):
        previous = self.previous()
        current = snapshot(window(3, "DECK", pane(0)))
        agent_deck_restore.carry_dashboards(current, previous)
        current["sessions"][0]["windows"][0]["panes"][0]["agent_deck"]["group"] = "changed"
        self.assertEqual(previous["sessions"][0]["windows"][1]["panes"][0]["agent_deck"], SCOPE)
        again = copy.deepcopy(current)
        self.assertEqual(agent_deck_restore.carry_dashboards(again, previous), [])

    def test_a_tab_is_identified_by_session_window_index_pane_index_and_name(self):
        previous = self.previous()
        for label, current in (
            ("another session", snapshot(window(3, "DECK", pane(0)), session="Other")),
            ("another window index", snapshot(window(4, "DECK", pane(0)))),
            ("another pane index", snapshot(window(3, "DECK", pane(1)))),
            ("renamed window", snapshot(window(3, "notes", pane(0)))),
        ):
            with self.subTest(label):
                self.assertEqual(agent_deck_restore.carry_dashboards(current, previous), [])
                self.assertNotIn("agent_deck", current["sessions"][0]["windows"][0]["panes"][0])

    def test_a_tab_that_runs_something_else_is_not_turned_back_into_a_dashboard(self):
        previous = self.previous()
        for label, extra in (
            ("conversation", {"conversation": {"provider": "claude", "session_id": None}}),
            ("codex", {"codex": {"session_id": "x"}}),
            ("unreadable dashboard", {"command": "agent-deck"}),
            ("live dashboard", {"agent_deck": dict(OTHER)}),
        ):
            with self.subTest(label):
                current = snapshot(window(3, "DECK", pane(0, **extra)))
                self.assertEqual(agent_deck_restore.carry_dashboards(current, previous), [])
                self.assertEqual(current["sessions"][0]["windows"][0]["panes"][0].get("agent_deck"),
                                 extra.get("agent_deck"))

    def test_a_scope_that_is_read_elsewhere_is_not_duplicated(self):
        previous = self.previous()
        current = snapshot(window(1, "work", pane(0, agent_deck=dict(SCOPE))), window(3, "DECK", pane(0)))
        self.assertEqual(agent_deck_restore.carry_dashboards(current, previous), [])

    def test_without_agent_deck_inventory_nothing_is_carried(self):
        current = snapshot(window(3, "DECK", pane(0)), deck=False)
        self.assertEqual(agent_deck_restore.carry_dashboards(current, self.previous()), [])
        self.assertEqual(agent_deck_restore.carry_dashboards(snapshot(window(3, "DECK", pane(0))), None), [])

    def checkpoint(self, current, previous, seen_dir, *, final=False):
        """The real recording and carrying of a checkpoint, as cli runs them."""
        from workspace_state import codex_resume
        dashboards = []
        with patch.object(cli, "_seen_directory", return_value=seen_dir), \
                patch.object(codex_resume, "unverified_start_ids", return_value=set()):
            cli._carry_pending(current, previous, {"terminals"}, login={"x": "y"}, held=set(),
                               final=final, dashboards=dashboards)
        return dashboards

    @staticmethod
    def tabs(value, name="DECK"):
        return [(w["index"], any("agent_deck" in p for p in w["panes"]))
                for w in value["sessions"][0]["windows"] if w["name"] == name]

    def test_a_dashboard_that_never_starts_keeps_one_deck_tab_across_many_logins(self):
        import tempfile
        from pathlib import Path
        for final in (False, True):
            previous = self.previous()
            with tempfile.TemporaryDirectory() as seen_dir:
                for login in range(4):
                    current = snapshot(window(1, "work", pane()), window(3, "DECK", pane(0)))
                    carried = self.checkpoint(current, previous, Path(seen_dir), final=final)
                    with self.subTest(final=final, login=login):
                        self.assertEqual(carried, ["Example:3.0"])
                        self.assertEqual(self.tabs(current), [(3, True)])
                        self.assertEqual([w["index"] for w in current["sessions"][0]["windows"]], [1, 3])
                        self.assertEqual(current["sessions"][0]["windows"][1]["name"], "DECK")
                        storage.validate(current)
                    previous = current

    def test_a_dashboard_seen_live_then_closed_is_not_brought_back_by_any_path(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as seen_dir:
            live = snapshot(window(1, "work", pane()), window(3, "DECK", pane(0, agent_deck=dict(SCOPE))))
            self.checkpoint(live, None, Path(seen_dir))
            closed = snapshot(window(1, "work", pane()), window(3, "DECK", pane(0)))
            self.assertEqual(self.checkpoint(closed, live, Path(seen_dir)), [])
            self.assertEqual(self.tabs(closed), [(3, False)])
            self.assertEqual([w["index"] for w in closed["sessions"][0]["windows"]], [1, 3])

    def test_a_kept_scope_is_never_recorded_as_seen(self):
        import tempfile
        from pathlib import Path
        from workspace_state import pending
        with tempfile.TemporaryDirectory() as seen_dir:
            current = snapshot(window(3, "DECK", pane(0)))
            self.checkpoint(current, self.previous(), Path(seen_dir))
            self.assertNotIn(pending.dashboard_item("Example", SCOPE), pending.seen_keys(Path(seen_dir)))
            self.assertEqual(self.tabs(current), [(3, True)])

    def test_a_dashboard_seen_live_this_login_and_gone_was_closed_and_is_not_brought_back(self):
        from workspace_state import pending
        key = pending.dashboard_item("Example", SCOPE)
        current = snapshot(window(3, "DECK", pane(0)))
        self.assertEqual(agent_deck_restore.carry_dashboards(current, self.previous(), {key}), [])
        self.assertNotIn("agent_deck", current["sessions"][0]["windows"][0]["panes"][0])
        # Never seen this login (the cross-release case): kept.
        current = snapshot(window(3, "DECK", pane(0)))
        self.assertEqual(agent_deck_restore.carry_dashboards(current, self.previous(), {"other"}), ["Example:3.0"])

    def test_a_kept_scope_is_not_an_observation(self):
        from workspace_state import pending
        current = snapshot(window(3, "DECK", pane(0)))
        before = pending.live_keys(current, {"terminals"})
        agent_deck_restore.carry_dashboards(current, self.previous(), set())
        # The carry itself writes nothing to the seen record; callers run it after recording.
        with patch.object(pending, "record_seen") as record:
            agent_deck_restore.carry_dashboards(snapshot(window(3, "DECK", pane(0))), self.previous(), set())
        record.assert_not_called()
        self.assertNotIn(pending.dashboard_item("Example", SCOPE), before)

    def test_a_carried_tab_restores_as_a_dashboard_at_its_saved_index_in_home(self):
        current = snapshot(window(1, "work", pane()), window(3, "DECK", pane(0)))
        agent_deck_restore.carry_dashboards(current, self.previous())
        saved = current["sessions"][0]["windows"][1]["panes"][0]
        with patch.object(agent_deck, "dashboard_command", return_value=["agent-deck", "-p", "work"]), \
                patch.dict("os.environ", {"HOME": "/home/example", "SHELL": "/bin/zsh"}):
            self.assertEqual(restore._pane_cwd(saved), "/home/example")
            self.assertEqual(restore._pane_tmux_command(saved),
                             ["agent-deck -p work; exec /bin/zsh"])


class DashboardStatusTests(unittest.TestCase):
    def failed(self, tab):
        with patch.object(restore, "_tmux_state", return_value={}), patch.object(restore, "_dashboard_scopes", return_value=[]):
            return restore.dashboard_tabs(
                {"name": "Example", "windows": [window(3, "DECK", pane(0, agent_deck=dict(SCOPE)))]}, "Example")[0]

    def test_missing_dashboards_are_named_by_session_and_window(self):
        self.assertEqual(self.failed("x"), ["Example:3"])

    def test_a_live_dashboard_is_matched_once_per_saved_tab(self):
        session = {"name": "Example", "windows": [
            window(1, "a", pane(0, agent_deck=dict(SCOPE))), window(2, "b", pane(0, agent_deck=dict(SCOPE)))]}
        with patch.object(restore, "_tmux_state", return_value={}), patch.object(
                restore, "_dashboard_scopes", return_value=[dict(SCOPE)]):
            missing, seen = restore.dashboard_tabs(session, "Example")
        self.assertEqual(missing, ["Example:2"])
        self.assertEqual(len(seen), 1)
        with patch.object(restore, "_tmux_state", return_value={}), patch.object(
                restore, "_dashboard_scopes", return_value=[dict(SCOPE)]):
            self.assertEqual(restore.dashboard_state(session, "Example")[0], 1)

    def run_layout(self, missing):
        session = {"name": "Example", "launch_terminal": False,
                   "windows": [window(3, "DECK", pane(0, agent_deck=dict(SCOPE)))]}
        args = argparse.Namespace(dry_run=False, no_place=True, workspace=None, session=None,
                                  select=False, login_status=False, verify_codex=False, wait=0)
        with patch.object(cli, "_live_terminal_clients", return_value={}), \
                patch.object(cli, "recreate_tmux", return_value=("Example", [])), \
                patch.object(cli, "_await_dashboards", return_value=(missing, set())), \
                patch.object(cli, "_record_seen"), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            return cli._restore_terminal_layout({}, args, [session], deck_managed=True)

    def test_a_tab_restored_as_a_shell_is_an_attention_item_and_not_verified(self):
        outcome = self.run_layout(["Example:3"])
        failed = [item for item in outcome.results if item.provider == "agent-deck"]
        self.assertEqual([item.item_id for item in failed], ["Example:3"])
        self.assertFalse(failed[0].success)
        self.assertTrue(failed[0].attention)
        self.assertIn("shell", failed[0].attention[0])
        self.assertTrue(failed[0].to_dict()["attention"])

    def test_a_present_dashboard_adds_no_attention(self):
        outcome = self.run_layout([])
        self.assertTrue(all(item.success for item in outcome.results))
        self.assertFalse([item for item in outcome.results if item.provider == "agent-deck"])

    def test_the_status_uses_existing_states_and_names_the_tab(self):
        failed = next(item for item in self.run_layout(["Example:3"]).results if item.provider == "agent-deck")
        verified = ProviderItemResult("alacritty", "Example/1", PhaseEvidence(EvidenceState.VERIFIED),
                                      PhaseEvidence(EvidenceState.VERIFIED), PhaseEvidence(EvidenceState.VERIFIED))
        for items, state in (([verified, failed], "degraded"), ([failed], "failed")):
            with self.subTest(state=state):
                document = {"stages": [{"id": "terminals", "provider_results": [i.to_dict() for i in items]}]}
                provider_progress.refresh_stage_evidence(document)
                stage = document["stages"][0]
                self.assertEqual(stage["state"], state)
                self.assertIn("Example:3", stage["message"])
                self.assertIn("shell", stage["message"])
                self.assertIn(stage["state"], {"ready", "degraded", "failed", "waiting", "skipped"})


class UnresolvedRecordTests(unittest.TestCase):
    ROW = {"profile": "work", "group": "project", "id": "record-1", "tool": "claude",
           "native_id": None, "account": "example", "title": "Draft", "open": True,
           "archived": False, "stopped": False}

    def test_a_record_without_a_native_id_is_never_ready_or_verified(self):
        value = {"version": 7, "created_at": "now",
                 "sessions": [{"name": "Example", "windows": [window(0, "DECK", pane(0, agent_deck=dict(SCOPE)))]}],
                 "agent_deck": {"version": 1, "sessions": [self.ROW]}}
        import time
        with patch.object(agent_deck, "inventory", return_value={"version": 1, "sessions": [self.ROW]}), \
                patch.object(agent_deck_restore, "_registered_only", return_value=True):
            result = agent_deck_restore.restore(value, value["sessions"], deadline=time.monotonic() + 3)
        self.assertEqual(result.ready, ())
        self.assertFalse(result.verified)
        self.assertEqual(result.unresolved, (self.ROW,))
        self.assertEqual(result.pending, (self.ROW,))

    def test_it_is_reported_for_attention_and_not_counted_as_restored(self):
        outcome = agent_deck_restore.Outcome((), 1, "", (self.ROW,), unresolved=(self.ROW,))
        self.assertFalse(outcome.verified)
        resolved = agent_deck_restore.Outcome((), 0)
        self.assertTrue(resolved.verified)


if __name__ == "__main__":
    unittest.main()
