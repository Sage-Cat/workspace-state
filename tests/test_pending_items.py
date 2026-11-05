"""Pending items: saved data that was not seen live stays until restored or forgotten.

Synthetic data only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import checkpoint, cli, pending, restore, shutdown_checkpoint_guard, storage
from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderItemResult
from workspace_state.social_apps import APP_BY_ID

NOW = "2026-11-05T10:00:00+00:00"
LATER = "2026-11-05T11:00:00+00:00"
CODEX = "11111111-1111-4111-8111-111111111111"
CLAUDE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
SERVER = {"boot_id": "boot", "server_pid": "100", "server_start_tick": "5", "session_id": "$1"}


def pane(index, *, codex=None, claude=None, pane_id=None, pid=None, confidence=None):
    value = {"index": index, "id": pane_id or f"%{index}", "cwd": "/synthetic", "command": "bash",
             "codex": None}
    if codex is not None:
        value["codex"] = {"session_id": codex, "confidence": confidence or "open-rollout",
                          "pid": pid or 10 + index, "start_ticks": "7"}
    if claude is not None:
        value["conversation"] = {"provider": "claude", "session_id": claude, "persisted": True,
                                 "confidence": confidence or "session-start-hook",
                                 "pid": pid or 20 + index, "start_ticks": "8"}
    return value


def window(index, *panes, name="w", active=False):
    return {"index": index, "name": name, "layout": "even-horizontal", "active": active,
            "panes": list(panes)}


def session(name, *windows):
    return {"name": name, "tmux_identity": dict(SERVER), "windows": list(windows)}


def social(**windowed):
    records = {app: {"running": False, "mode": "stopped", "windows": []} for app in APP_BY_ID}
    for app, count in windowed.items():
        records[app] = {"running": True, "mode": "windowed", "windows": [{
            "workspace_name": "One", "workspace": 0, "monitor_identity": {"connector": "DP-1"},
            "state": "normal", "geometry": {"x": 0, "y": 0, "width": 10, "height": 10},
            "geometry_relative": {"x": 0, "y": 0, "width": 10, "height": 10}}] * count}
    return records


def nemo(*locations):
    geometry = {"x": 0, "y": 0, "width": 10, "height": 10}
    return {"provider": "nemo", "desktop_id": "nemo.desktop", "windows": [
        {"locations": [location], "active_tab": 0, "title": location.rsplit("/", 1)[-1],
         "placement": {"workspace_name": "One", "workspace": 0, "monitor_identity": {"connector": "DP-1"},
                       "state": "normal", "geometry": geometry, "geometry_relative": geometry}}
        for location in locations]}


def snapshot(*sessions, created_at=NOW, **extra):
    return {"version": 6, "created_at": created_at, "sessions": list(sessions), "terminals": [],
            "desktop": {}, **extra}


def carry(current, previous, *, seen=(), fresh=pending.CATEGORIES):
    warnings = pending.carry(current, previous, fresh=fresh, seen=set(seen), now=current["created_at"])
    pending.validate(current)
    return warnings


class CarryTerminalTests(unittest.TestCase):
    def test_unseen_session_is_carried_with_its_conversations_and_clients(self):
        previous = snapshot(session("main", window(0, pane(0))),
                            session("work", window(1, pane(0, codex=CODEX), pane(1), active=True)))
        previous["terminals"] = [{"session": "work", "placement": {"workspace": 0}}]
        current = snapshot(session("main", window(0, pane(0))), created_at=LATER)
        carry(current, previous)
        work = next(item for item in current["sessions"] if item["name"] == "work")
        self.assertNotIn("id", work["windows"][0]["panes"][0])
        self.assertTrue(work["windows"][0]["active"])
        self.assertEqual(current["terminals"], previous["terminals"])
        self.assertEqual(current["pending"]["session:work"], {"category": "terminals", "since": LATER})
        self.assertEqual(current["pending"][f"codex:{CODEX}"],
                         {"category": "terminals", "since": LATER, "session": "work", "window": 1})

    def test_closed_session_whose_conversations_were_seen_is_dropped(self):
        previous = snapshot(session("work", window(1, pane(0, codex=CODEX))))
        previous["terminals"] = [{"session": "work", "placement": {"workspace": 0}}]
        current = snapshot(created_at=LATER)
        carry(current, previous, seen={"session:work", f"codex:{CODEX}"})
        self.assertEqual((current["sessions"], current["terminals"]), ([], []))
        self.assertNotIn("pending", current)

    def test_closed_session_keeps_only_its_unseen_conversations(self):
        # The login restored `work`, but resuming CODEX failed; the user then
        # closed the session. The conversation stays pending without the
        # session, its shell windows or its terminal client.
        previous = snapshot(session("work", window(1, pane(0, codex=CODEX), pane(1)), window(2, pane(0)),
                                    window(3, pane(0, codex=OTHER))))
        previous["terminals"] = [{"session": "work", "placement": {"workspace": 0}}]
        current = snapshot(created_at=LATER)
        carry(current, previous, seen={"session:work", f"codex:{OTHER}"})
        self.assertEqual([item["index"] for item in current["sessions"][0]["windows"]], [1])
        self.assertEqual(current["terminals"], [])
        self.assertEqual(current["pending"], {f"codex:{CODEX}": {
            "category": "terminals", "since": LATER, "session": "work", "window": 1}})
        self.assertEqual(pending.pending_layout(current), (set(), {"work": {1}}))
        self.assertEqual(pending.live_sessions_only(current), [])

    def test_forgetting_the_last_conversation_of_a_closed_session_removes_it(self):
        previous = snapshot(session("work", window(1, pane(0, codex=CODEX))))
        current = snapshot(created_at=LATER)
        carry(current, previous, seen={"session:work"})
        self.assertEqual(pending.forget(current, [f"codex:{CODEX}"]), {"terminals"})
        self.assertEqual(current["sessions"], [])
        self.assertNotIn("pending", current)
        pending.validate(current)

    def test_renamed_session_keeps_its_unseen_conversation(self):
        previous = snapshot(session("work", window(1, pane(0, codex=CODEX))))
        current = snapshot(session("renamed", window(1, pane(0))), created_at=LATER)
        carry(current, previous, seen={"session:work", *pending.live_keys(current, {"terminals"})})
        self.assertEqual([item["name"] for item in current["sessions"]], ["renamed", "work"])
        self.assertEqual(set(current["pending"]), {f"codex:{CODEX}"})

    def test_carried_session_drops_identities_already_seen(self):
        previous = snapshot(session("work", window(1, pane(0, codex=CODEX), pane(1, claude=CLAUDE))))
        current = snapshot(created_at=LATER)
        carry(current, previous, seen={f"codex:{CODEX}"})
        panes = current["sessions"][0]["windows"][0]["panes"]
        self.assertIsNone(panes[0]["codex"])
        self.assertEqual(panes[1]["conversation"]["session_id"], CLAUDE)
        self.assertEqual(set(current["pending"]), {"session:work", f"claude:{CLAUDE}"})

    def test_unseen_conversation_of_a_live_session_keeps_its_free_window(self):
        previous = snapshot(session("main", window(0, pane(0)), window(2, pane(0, codex=CODEX), pane(1))))
        current = snapshot(session("main", window(0, pane(0))), created_at=LATER)
        carry(current, previous)
        indexes = [item["index"] for item in current["sessions"][0]["windows"]]
        self.assertEqual(indexes, [0, 2])
        carried = current["sessions"][0]["windows"][1]
        self.assertEqual(len(carried["panes"]), 2)
        self.assertFalse(carried["active"])
        self.assertEqual(current["pending"][f"codex:{CODEX}"]["window"], 2)
        self.assertEqual(pending.pending_layout(current), (set(), {"main": {2}}))

    def test_occupied_window_index_moves_only_the_waiting_panes(self):
        previous = snapshot(session("main", window(0, pane(0), pane(1, codex=CODEX))))
        current = snapshot(session("main", window(0, pane(0)), window(3, pane(0))), created_at=LATER)
        carry(current, previous)
        carried = current["sessions"][0]["windows"][-1]
        self.assertEqual((carried["index"], carried["layout"]), (4, "tiled"))
        self.assertEqual([item["codex"]["session_id"] for item in carried["panes"]], [CODEX])
        self.assertNotIn("id", carried["panes"][0])

    def test_live_conversation_is_never_carried(self):
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX))))
        current = snapshot(session("main", window(0, pane(0, codex=CODEX.upper()))), created_at=LATER)
        live = pending.live_keys(current, {"terminals"})
        self.assertIn(f"codex:{CODEX}", live)
        carry(current, previous, seen=live)
        self.assertNotIn("pending", current)

    def test_waiting_launcher_is_not_live_evidence(self):
        current = snapshot(session("main", window(0, pane(0, codex=CODEX, confidence="restore-wrapper"))))
        self.assertEqual(pending.live_keys(current, {"terminals"}), {"session:main"})

    def test_claimed_claude_client_is_not_seen_before_its_hook(self):
        current = snapshot(session("main", window(0, pane(0, claude=CLAUDE, confidence="resume-claim"))))
        self.assertEqual(pending.live_keys(current, {"terminals"}), {"session:main"})

    def test_waiting_launcher_keeps_its_identity_without_a_second_copy(self):
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX))),
                            session("old", window(4, pane(0, codex=CODEX), pane(1, claude=CLAUDE))))
        current = snapshot(session("main", window(0, pane(0, codex=CODEX, confidence="restore-wrapper"))),
                           created_at=LATER)
        carry(current, previous, seen=pending.live_keys(current, {"terminals"}))
        old = next(item for item in current["sessions"] if item["name"] == "old")
        self.assertIsNone(old["windows"][0]["panes"][0]["codex"])
        self.assertEqual(len(current["sessions"][0]["windows"]), 1)
        self.assertEqual(set(current["pending"]), {"session:old", f"claude:{CLAUDE}"})

    def test_first_since_is_kept_and_untouched_categories_keep_their_entries(self):
        previous = snapshot(session("work", window(1, pane(0))))
        first = snapshot(created_at=NOW)
        carry(first, previous)
        first["file_manager"] = nemo("file:///synthetic/a")
        first["pending"][pending.file_manager_key(first["file_manager"]["windows"][0])] = {
            "category": "file-manager", "since": NOW}
        second = snapshot(created_at=LATER, file_manager=deepcopy(first["file_manager"]))
        carry(second, first, fresh={"terminals"})
        self.assertEqual(second["pending"]["session:work"]["since"], NOW)
        self.assertEqual(second["pending"], first["pending"])


class CarryApplicationTests(unittest.TestCase):
    def test_unseen_file_manager_window_is_carried(self):
        previous = snapshot(file_manager=nemo("file:///synthetic/a", "file:///synthetic/b"))
        current = snapshot(file_manager=nemo("file:///synthetic/a"), created_at=LATER)
        seen = pending.live_keys(current, {"file-manager"})
        carry(current, previous, seen=seen)
        self.assertEqual(len(current["file_manager"]["windows"]), 2)
        self.assertEqual([entry["category"] for entry in current["pending"].values()], ["file-manager"])

    def test_window_limit_drops_extra_windows_with_a_warning(self):
        previous = snapshot(file_manager=nemo(*(f"file:///synthetic/{index}" for index in range(3))))
        current = snapshot(file_manager=nemo(), created_at=LATER)
        with patch("workspace_state.file_manager.MAX_WINDOWS", 2):
            warnings = pending.carry(current, previous, fresh={"file-manager"}, seen=set(), now=LATER)
        self.assertEqual(len(current["file_manager"]["windows"]), 2)
        self.assertIn("1 unrestored file-manager window(s)", warnings[0])

    def test_unseen_windowed_app_keeps_its_record(self):
        app = next(iter(APP_BY_ID))
        previous = snapshot(social_apps=social(**{app: 1}))
        current = snapshot(social_apps=social(), created_at=LATER)
        carry(current, previous)
        self.assertEqual(current["social_apps"][app]["mode"], "windowed")
        self.assertIn(f"social-apps:{app}", current["pending"])

    def test_app_closed_during_this_login_is_not_carried(self):
        app = next(iter(APP_BY_ID))
        previous = snapshot(social_apps=social(**{app: 1}))
        current = snapshot(social_apps=social(), created_at=LATER)
        carry(current, previous, seen={f"social-apps:{app}"})
        self.assertEqual(current["social_apps"][app]["mode"], "stopped")


class SeenRecordTests(unittest.TestCase):
    def test_unverified_supervised_resume_is_not_seen(self):
        current = snapshot(session("main", window(0, pane(0, codex=CODEX, confidence="command-line"),
                                                  pane(1, codex=OTHER, confidence="command-line"))))
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "_seen_directory", return_value=Path(directory)), \
             patch("workspace_state.codex_resume.unverified_start_ids", return_value={CODEX}):
            cli._carry_pending(current, None, {"terminals"}, login=None)
            self.assertEqual(pending.seen_keys(Path(directory)), {"session:main", f"codex:{OTHER}"})

    def test_record_round_trip_and_unavailable_login(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(pending.record_seen(root, {"session:a"}))
            self.assertTrue(pending.record_seen(root, {"session:b"}))
            self.assertEqual(pending.seen_keys(root), {"session:a", "session:b"})
            (root / pending.SEEN_FILE).write_text("{broken")
            self.assertEqual(pending.seen_keys(root), set())
        self.assertFalse(pending.record_seen(None, {"session:a"}))
        self.assertEqual(pending.seen_keys(None), set())

    def test_restored_results_map_to_item_keys(self):
        current = snapshot(file_manager=nemo("file:///synthetic/a", "file:///synthetic/b"))
        verified = PhaseEvidence(EvidenceState.VERIFIED)
        results = [ProviderItemResult("nemo", "2", verified),
                   ProviderItemResult("nemo", "1", PhaseEvidence(EvidenceState.FAILED))]
        self.assertEqual(pending.restored_keys("file-manager", current, results),
                         {pending.file_manager_key(current["file_manager"]["windows"][1])})
        app = next(iter(APP_BY_ID))
        self.assertEqual(pending.restored_keys("social-apps", current,
                                               [ProviderItemResult("social-apps", f"{app}:1", verified)]),
                         {f"social-apps:{app}"})


class ValidateAndForgetTests(unittest.TestCase):
    def carried(self):
        previous = snapshot(session("main", window(0, pane(0)), window(2, pane(0, codex=CODEX))),
                            session("work", window(1, pane(0, claude=CLAUDE))),
                            file_manager=nemo("file:///synthetic/a"))
        current = snapshot(session("main", window(0, pane(0))), file_manager=nemo(), created_at=LATER)
        carry(current, previous)
        return current

    def test_validate_rejects_entries_without_their_data(self):
        current = self.carried()
        for broken in ({"session:gone": {"category": "terminals", "since": NOW}},
                       {"session:main": {"category": "vscode", "since": NOW}},
                       {"session:main": {"category": "terminals"}},
                       {f"codex:{CODEX}": {"category": "terminals", "since": NOW, "session": "main", "window": 9}},
                       {"bogus": {"category": "terminals", "since": NOW}}):
            with self.subTest(broken=broken):
                value = deepcopy(current)
                value["pending"] = broken
                with self.assertRaises(ValueError):
                    pending.validate(value)
        storage.validate(deepcopy(current))
        with self.assertRaisesRegex(ValueError, "pending index"):
            storage.validate({**deepcopy(current), "pending": []})

    def test_forget_conversation_removes_its_carried_window(self):
        current = self.carried()
        self.assertEqual(pending.forget(current, [f"codex:{CODEX}"]), {"terminals"})
        self.assertEqual([item["index"] for item in current["sessions"][0]["windows"]], [0])
        pending.validate(current)

    def test_forget_session_removes_its_conversations(self):
        current = self.carried()
        pending.forget(current, ["session:work", f"claude:{CLAUDE}"])
        self.assertEqual([item["name"] for item in current["sessions"]], ["main"])
        self.assertNotIn(f"claude:{CLAUDE}", current["pending"])
        pending.validate(current)

    def test_forget_window_and_unknown_key(self):
        current = self.carried()
        key = next(item for item in current["pending"] if item.startswith("file-manager:"))
        self.assertEqual(pending.forget(current, [key]), {"file-manager"})
        self.assertEqual(current["file_manager"]["windows"], [])
        with self.assertRaises(KeyError):
            pending.forget(current, ["session:unknown"])

    def test_live_sessions_only_hides_pending_data(self):
        current = self.carried()
        self.assertEqual([(item["name"], [w["index"] for w in item["windows"]])
                          for item in pending.live_sessions_only(current)], [("main", [0])])

    def test_seal_comparison_ignores_pending_data(self):
        current = self.carried()
        live = [session("main", window(0, pane(0)))]
        shutdown_checkpoint_guard._verify_saved_sessions(current, live, degraded=False)
        with self.assertRaisesRegex(ValueError, "structure changed"):
            shutdown_checkpoint_guard._verify_saved_sessions(
                {**current, "pending": {}}, live, degraded=False)


class UnchangedClientTests(unittest.TestCase):
    def test_missing_evidence_keeps_the_saved_identity(self):
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX, pid=50))))
        current = snapshot(session("main", window(0, pane(0))))
        current["sessions"][0]["windows"][0]["panes"][0]["codex"] = {
            "session_id": None, "confidence": "unknown", "pid": 50, "start_ticks": "7"}
        self.assertEqual(pending.keep_unchanged_clients(current, previous), set())
        self.assertEqual(current["sessions"][0]["windows"][0]["panes"][0]["codex"]["session_id"], CODEX)

    def test_contradicting_evidence_holds_the_conversation_pending(self):
        previous = snapshot(session("main", window(0, pane(0, claude=CLAUDE, pid=60))))
        current = snapshot(session("main", window(0, pane(0))))
        current["sessions"][0]["windows"][0]["panes"][0]["conversation"] = {
            "provider": "ambiguous", "session_id": None, "pid": 60, "start_ticks": "8"}
        held = pending.keep_unchanged_clients(current, previous)
        self.assertEqual(held, {f"claude:{CLAUDE}"})
        carry(current, previous, seen={f"claude:{CLAUDE}", "session:main"} - held)
        carried = current["sessions"][0]["windows"][-1]
        self.assertEqual(carried["index"], 1)
        self.assertEqual(carried["panes"][0]["conversation"]["session_id"], CLAUDE)

    def test_shutdown_paths_hold_instead_of_carrying(self):
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX, pid=50))))
        current = snapshot(session("main", window(0, pane(0))))
        current["sessions"][0]["windows"][0]["panes"][0]["codex"] = {
            "session_id": None, "confidence": "unknown", "pid": 50, "start_ticks": "7"}
        self.assertEqual(pending.keep_unchanged_clients(current, previous, carry_identity=False),
                         {f"codex:{CODEX}"})
        self.assertIsNone(current["sessions"][0]["windows"][0]["panes"][0]["codex"]["session_id"])

    def test_replaced_client_or_another_server_is_not_the_same_client(self):
        for change in ({"pid": 51}, {"start_ticks": "9"}):
            with self.subTest(change=change):
                previous = snapshot(session("main", window(0, pane(0, codex=CODEX, pid=50))))
                current = snapshot(session("main", window(0, pane(0))))
                current["sessions"][0]["windows"][0]["panes"][0]["codex"] = {
                    "session_id": None, "confidence": "unknown", "pid": 50, "start_ticks": "7", **change}
                self.assertEqual(pending.keep_unchanged_clients(current, previous), set())
                self.assertIsNone(current["sessions"][0]["windows"][0]["panes"][0]["codex"]["session_id"])
        previous = snapshot(session("main", window(0, pane(0, codex=CODEX, pid=50))))
        current = snapshot(session("main", window(0, pane(0))))
        current["sessions"][0]["tmux_identity"]["server_pid"] = "101"
        current["sessions"][0]["windows"][0]["panes"][0]["codex"] = {
            "session_id": None, "confidence": "unknown", "pid": 50, "start_ticks": "7"}
        self.assertEqual(pending.keep_unchanged_clients(current, previous), set())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_DATA_HOME": str(root / "data"), "XDG_RUNTIME_DIR": str(root / "runtime"),
            "WSCTL_OPERATION_CONTEXT": ""}))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.out))
        self.stack.enter_context(redirect_stderr(self.err))
        self.stack.enter_context(patch.object(shutdown_checkpoint_guard, "check_manual_save_allowed"))
        self.stack.enter_context(patch.object(shutdown_checkpoint_guard, "protected", return_value=False))
        previous = snapshot(session("main", window(0, pane(0)), window(2, pane(0, codex=CODEX))),
                            session("work", window(1, pane(0))))
        current = snapshot(session("main", window(0, pane(0))), created_at=LATER)
        carry(current, previous)
        checkpoint.record_provenance(current, None, source="manual-save", owner=None, problems={})
        storage.save(current)

    def test_show_lists_pending_items(self):
        self.assertEqual(cli.cmd_show(argparse.Namespace(json=False, details=False)), 0)
        text = self.out.getvalue()
        self.assertIn("session:work", text)
        self.assertIn(f"codex:{CODEX}", text)

    def test_forget_drops_items_and_keeps_provenance_consistent(self):
        self.assertEqual(cli.cmd_forget(argparse.Namespace(keys=["session:work"])), 0)
        saved = storage.load()
        self.assertEqual([item["name"] for item in saved["sessions"]], ["main"])
        record = saved["category_provenance"]["terminals"]
        self.assertEqual(record["content_digest"], checkpoint.category_digest(saved, "terminals"))
        self.assertEqual(record["source"], "manual-save")
        self.assertEqual(cli.cmd_forget(argparse.Namespace(keys=["session:work"])), 2)
        self.assertIn("not pending", self.err.getvalue())

    def test_parser_accepts_forget(self):
        self.assertEqual(cli.parser().parse_args(["forget", "session:a", "session:b"]).keys,
                         ["session:a", "session:b"])


class RestoreSplitTests(unittest.TestCase):
    def test_pending_windows_join_the_live_session_without_a_copy(self):
        saved = session("main", window(0, pane(0)), window(2, pane(0, codex=CODEX)))
        created = []
        with patch.object(restore, "_tmux_exists", side_effect=lambda name: name == "main"), \
                patch.object(restore, "_tmux_state", return_value={0: {"id": "@1", "name": "w", "panes": {}}}), \
                patch.object(restore.conversation_guard, "live_locations", return_value={}), \
                patch.object(restore, "_create_window",
                             side_effect=lambda name, item, **_: created.append((name, item["index"]))), \
                patch.object(restore, "recreate_tmux", wraps=restore.recreate_tmux) as recreate, \
                patch.object(restore, "_same_tmux_session", return_value=True), \
                patch.object(restore, "_reconcile_tmux", return_value=["reconciled"]), \
                patch.object(restore, "_tagged_restore_session", return_value=None), \
                patch.object(restore, "_restore_fingerprint", return_value=None):
            name, actions = restore.recreate_tmux(saved, pending_windows={2})
        self.assertEqual(name, "main")
        self.assertEqual(created, [("main", 2)])
        self.assertIn("reconciled", actions)
        self.assertEqual([len(call.args[0]["windows"]) for call in recreate.call_args_list], [2, 1])

    def test_pending_window_already_running_elsewhere_is_not_recreated(self):
        saved = session("main", window(0, pane(0)), window(2, pane(0, codex=CODEX)))
        with patch.object(restore, "_tmux_exists", return_value=True), \
                patch.object(restore, "_tmux_state", return_value={0: {"id": "@1", "name": "w", "panes": {}}}), \
                patch.object(restore.conversation_guard, "live_locations",
                             return_value={f"codex:{CODEX}": ["other:1.0"]}), \
                patch.object(restore.conversation_guard, "matching", return_value=["other:1.0"]), \
                patch.object(restore, "_create_window") as create:
            actions = restore._add_pending_windows("main", saved, [saved["windows"][1]], dry_run=False)
        create.assert_not_called()
        self.assertTrue(any("already runs" in action for action in actions))


if __name__ == "__main__":
    unittest.main()
