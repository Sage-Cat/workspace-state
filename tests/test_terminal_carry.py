"""A saved Alacritty window whose restore never verified survives the login.

Synthetic data only.
"""
from __future__ import annotations

from argparse import Namespace
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, pending, provider_progress as progress
from workspace_state.provider_results import EvidenceState as S, PhaseEvidence as E, ProviderItemResult
from workspace_state.restore import RestoreResult
from test_pending_items import LATER, NOW, pane, session, snapshot, window
from test_provider_progress import ProviderProgressTests

PLACEMENT = {"workspace": 1, "workspace_name": "Two", "monitor": 0, "state": "normal", "title": "work"}
ARGS = Namespace(workspace=None, session=None, select=False, dry_run=False, no_place=False,
                 verify_codex=False, wait=0, login_status=False)
NATIVE = "native-handoff:token-1"


def saved(name="work"):
    previous = snapshot(session(name, window(0, pane(0))), created_at=NOW)
    previous["terminals"] = [{"session": name, "alacritty_pid": 10, "placement": dict(PLACEMENT)}]
    return previous


def capture(*names, clients=()):
    current = snapshot(*(session(name, window(0, pane(0))) for name in names), created_at=LATER)
    current["terminals"] = [{"session": name, "alacritty_pid": 99, "placement": dict(PLACEMENT)} for name in clients]
    return current


class UnfinishedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory, True)
        for target in ("_seen_directory",):
            patcher = patch.object(cli, target, return_value=self.directory)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(cli, "_checkpoint_login", return_value={"x": "y"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def restore(self, snapshot_, launch, *, clients=None):
        (item,) = [value for value in cli._restore_items(snapshot_) if value["launch_terminal"]]
        with patch.object(cli, "_live_terminal_clients", return_value=clients or {}), patch.object(
                cli, "recreate_tmux", side_effect=lambda value, **kw: (value["name"], [])), patch.object(
                cli, "launch_terminal", side_effect=launch) as launcher, patch.object(
                cli, "_record_seen"), patch.object(cli, "update_stage"):
            try:
                cli._restore_terminal_layout(snapshot_, ARGS, [item])
            except RuntimeError:
                pass
        return launcher

    def checkpoint(self, current, *, final=False):
        cli._carry_pending(current, saved(), {"terminals"}, login={"x": "y"}, final=final)
        return current

    def fail_launch(self):
        self.restore(saved(), lambda *a, **k: RestoreResult("Alacritty for work: exited", False))

    def test_failed_launch_is_kept_by_periodic_and_final_saves(self):
        self.fail_launch()
        for final in (False, True):
            current = self.checkpoint(capture("work"), final=final)
            self.assertEqual([item["placement"]["workspace_name"] for item in current["terminals"]], ["Two"])
        launcher = self.restore(current, lambda *a, **k: RestoreResult("launched work"))
        launcher.assert_called_once()

    def test_verified_launch_leaves_nothing_to_carry(self):
        self.restore(saved(), lambda *a, **k: RestoreResult("launched work; placement verified"))
        self.assertEqual(self.checkpoint(capture("work"))["terminals"], [])

    def test_pending_handoff_stays_until_the_window_vanishes(self):
        self.restore(saved(), lambda *a, **k: RestoreResult("owned by GNOME", False, NATIVE))
        self.assertEqual(len(self.checkpoint(capture("work"))["terminals"]), 1)
        self.assertEqual(self.checkpoint(capture("work", clients=["work"]))["terminals"][0]["alacritty_pid"], 99)
        self.assertEqual(len(self.checkpoint(capture("work", clients=["work"]))["terminals"]), 1)  # no duplicate

    def test_live_window_of_the_session_blocks_the_carry_and_a_later_close_is_the_users(self):
        self.fail_launch()
        current = self.checkpoint(capture("work", clients=["work"]))
        self.assertEqual([item["alacritty_pid"] for item in current["terminals"]], [99])
        self.assertEqual(self.checkpoint(capture("work"))["terminals"], [])  # the user closed it

    def test_window_opened_by_a_later_restore_then_closed_does_not_return(self):
        self.fail_launch()
        self.restore(saved(), lambda *a, **k: RestoreResult("launched work; placement verified"))
        self.checkpoint(capture("work", clients=["work"]))
        self.assertEqual(self.checkpoint(capture("work"))["terminals"], [])

    def test_two_failures_of_the_same_window_give_one_record(self):
        self.fail_launch()
        self.fail_launch()
        self.assertEqual(len(pending._unfinished(self.directory)), 1)
        self.assertEqual(len(self.checkpoint(capture("work"))["terminals"]), 1)

    def test_one_failed_window_does_not_stop_the_other_sessions(self):
        previous = saved("first")
        previous["sessions"].append(session("second", window(0, pane(0))))
        previous["terminals"].append({"session": "second", "alacritty_pid": 11, "placement": dict(PLACEMENT)})
        calls = []

        def launch(item, **kwargs):
            calls.append(item["name"])
            return RestoreResult("exited", False) if item["name"] == "first" else RestoreResult("launched")

        items = [value for value in cli._restore_items(previous) if value["launch_terminal"]]
        with patch.object(cli, "_live_terminal_clients", return_value={}), patch.object(
                cli, "recreate_tmux", side_effect=lambda value, **kw: (value["name"], [])), patch.object(
                cli, "launch_terminal", side_effect=launch), patch.object(cli, "_record_seen"), patch.object(
                cli, "update_stage"):
            outcome = cli._restore_terminal_layout(previous, ARGS, items)
        self.assertEqual(calls, ["first", "second"])
        self.assertEqual([item.success for item in outcome.results], [True, False])
        (failed,) = [item for item in outcome.results if not item.success]
        self.assertEqual(failed.item_id, "first/1")
        self.assertEqual(outcome.restored, 1)
        self.assertEqual([item["session"] for item in pending._unfinished(self.directory).values()], ["first"])

    def test_session_kept_only_for_an_unseen_conversation_gets_no_window(self):
        previous = saved()
        previous["sessions"] = [session("work", window(0, pane(0, codex="11111111-1111-4111-8111-111111111111")))]
        self.fail_launch()
        pending.record_seen(self.directory, {"session:work"})  # restored, then the user killed it
        current = snapshot(created_at=LATER)
        cli._carry_pending(current, previous, {"terminals"}, login={"x": "y"})
        self.assertEqual([item["name"] for item in current["sessions"]], ["work"])
        self.assertEqual(current["terminals"], [])

    def test_missing_or_renamed_session_is_not_carried(self):
        self.fail_launch()
        pending.record_seen(self.directory, {"session:work"})  # it was live in this login, then closed or renamed
        for names in ((), ("renamed",)):
            self.assertEqual(self.checkpoint(capture(*names))["terminals"], [])

    def test_existing_window_is_reused_without_a_record(self):
        client = {"session": "work", "placement": {"id": 5}, "alacritty_pid": 5}
        with patch.object(cli, "place_terminal", return_value=RestoreResult("verified existing")):
            self.restore(saved(), lambda *a, **k: self.fail("no launch"), clients={"work": [client]})
        self.assertEqual(self.checkpoint(capture("work"))["terminals"], [])

    def test_reused_window_settles_the_notes_before_it_is_ever_captured(self):
        self.fail_launch()
        client = {"session": "work", "placement": {"id": 5}, "alacritty_pid": 5}
        with patch.object(cli, "place_terminal", return_value=RestoreResult("verified existing")):
            self.restore(saved(), lambda *a, **k: self.fail("no launch"), clients={"work": [client]})
        self.assertEqual(self.checkpoint(capture("work"))["terminals"], [])  # the user closed it

    def two_windows(self):
        previous = saved()
        previous["terminals"].append({"session": "work", "alacritty_pid": 11,
                                      "placement": {**PLACEMENT, "workspace": 0, "workspace_name": "One"}})
        return previous

    def retry(self, previous, launch, args):
        items = [value for value in cli._filtered_terminal_items(previous, args) if value["launch_terminal"]]
        with patch.object(cli, "_live_terminal_clients", return_value={}), patch.object(
                cli, "recreate_tmux", side_effect=lambda value, **kw: (value["name"], [])), patch.object(
                cli, "launch_terminal", side_effect=launch), patch.object(
                cli, "_record_seen"), patch.object(cli, "update_stage"):
            cli._restore_terminal_layout(previous, args, items)

    def carried(self, previous):
        current = capture("work")
        cli._carry_pending(current, previous, {"terminals"}, login={"x": "y"})
        return sorted(item["placement"]["workspace_name"] for item in current["terminals"])

    def test_filtered_retry_never_overwrites_the_note_of_another_window(self):
        previous = self.two_windows()
        fail = lambda *a, **k: RestoreResult("exited", False)
        self.restore_all(previous, fail)
        self.retry(previous, lambda *a, **k: RestoreResult("launched; placement verified"),
                   Namespace(**{**vars(ARGS), "workspace": "Two"}))
        self.assertEqual(self.carried(previous), ["One"])  # Two was opened and closed; One never appeared

    def test_selected_retry_keeps_the_other_note(self):
        previous = self.two_windows()
        self.restore_all(previous, lambda *a, **k: RestoreResult("exited", False))
        with patch.object(cli, "_select", side_effect=lambda items, names: items[1:]):
            self.retry(previous, lambda *a, **k: RestoreResult("launched; placement verified"),
                       Namespace(**{**vars(ARGS), "select": True}))
        self.assertEqual(self.carried(previous), ["Two"])

    def test_off_on_restore_with_archive_indexes_keeps_the_other_note(self):
        previous = self.two_windows()
        self.restore_all(previous, lambda *a, **k: RestoreResult("exited", False))
        archived = {**previous, "terminals": previous["terminals"][:1]}  # On restores only a part, indexed from 1
        self.retry(archived, lambda *a, **k: RestoreResult("launched; placement verified"), ARGS)
        self.assertEqual(self.carried(previous), ["One"])

    def restore_all(self, previous, launch):
        self.retry(previous, launch, ARGS)

    def test_two_windows_one_live_does_not_carry_the_failed_one(self):
        self.fail_launch()
        current = self.checkpoint(capture("work", clients=["work"]), final=True)
        self.assertEqual(len(current["terminals"]), 1)  # known limit: the failed window is not kept

    def test_unfinished_records_need_no_pending_items(self):
        self.fail_launch()
        current = self.checkpoint(capture("work"))
        self.assertNotIn("pending", current)
        pending.validate(current)


class ObserverClearsTerminalTests(ProviderProgressTests):
    def waiting(self, name="work", token=NATIVE):
        item = ProviderItemResult("alacritty", f"{name}/1", E(S.VERIFIED), E(S.VERIFIED),
                                  E(S.WAITING, "awaiting", True, token))
        self.seed([item], category="terminals")

    def note(self, workspace, request_id):
        directory = cli._seen_directory(cli._checkpoint_login())
        self.assertIsNotNone(directory)
        key = pending.record_unfinished(directory, {"session": "work", "placement": {**PLACEMENT, "workspace_name": workspace}})
        if request_id:
            pending.tag_unfinished(directory, key, request_id)
        return directory

    def observe(self, status):
        with patch.object(progress, "_query_request", return_value={"token": NATIVE, "status": status}):
            progress.reconcile_pending()

    def test_observed_placement_removes_the_note_of_that_request(self):
        directory = self.note("Two", NATIVE)
        self.waiting()
        self.observe("verified")
        self.assertEqual(pending._unfinished(directory), {})

    def test_unverified_placement_keeps_the_note(self):
        directory = self.note("Two", NATIVE)
        self.waiting()
        self.observe("deferred")
        self.assertEqual(len(pending._unfinished(directory)), 1)

    def test_observer_never_removes_the_note_of_another_window(self):
        directory = self.note("One", "native-handoff:other")
        self.note("Two", None)  # a later restore of another window, not yet submitted
        self.waiting()
        self.observe("verified")
        self.assertEqual(sorted(item["record"]["placement"]["workspace_name"]
                                for item in pending._unfinished(directory).values()), ["One", "Two"])


del ProviderProgressTests  # only the subclass runs here

if __name__ == "__main__":
    unittest.main()
