from __future__ import annotations

import importlib
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from workspace_state import storage
from workspace_state.util import CommandError

capture = importlib.import_module("workspace_state.capture")


class CaptureFixture:
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(capture, "capture_shell", return_value={
            "available": True, "windows": [], "workspaces": [], "monitors": [],
        }))
        self.stack.enter_context(patch.object(capture, "workspace_names", return_value=[]))
        self.stack.enter_context(patch.object(capture, "codex_for_pane", return_value=None))
        self.identity = {"server_pid": "99", "server_start_tick": "100",
                         "session_id": "$4", "boot_id": "11111111-1111-4111-8111-111111111111"}
        self.identity_probe = self.stack.enter_context(patch.object(
            capture, "tmux_runtime_identity", return_value=self.identity))
        self.window_probe = self.stack.enter_context(patch.object(capture, "read_window_names",
            return_value={"name": "study\tguide\n#{pane_id};", "automatic_rename": False}))
        self.stack.enter_context(patch.object(capture, "read_pane_names", return_value={
            "title": "app title", "label": "saved work name",
        }))
        self.stack.enter_context(patch.object(capture, "read_pane_rename_policy", return_value=False))
        self.rows_probe = self.stack.enter_context(patch.object(capture, "_rows", side_effect=[[], [
            ["work", "2", "@7", "layout", "1", "1", "%10", "11", "/tmp", "sh", "1", "@7"],
            ["work", "2", "@7", "layout", "1", "2", "%12", "13", "/tmp", "sh", "0", "@7"],
        ]]))



class TmuxCaptureMetadataTests(CaptureFixture, unittest.TestCase):
    def test_delimited_inventory_excludes_names_and_capture_preserves_policies(self):
        snapshot = capture.capture()
        storage.validate(snapshot)
        self.assertEqual(snapshot["capture_errors"]["tmux"], [])
        session = snapshot["sessions"][0]
        self.assertEqual(session["tmux_identity"], self.identity)
        window = session["windows"][0]
        self.assertEqual(window["name"], "study\tguide\n#{pane_id};")
        self.assertFalse(window["automatic_rename"])
        self.assertEqual([p["label"] for p in window["panes"]], ["saved work name"] * 2)
        self.assertTrue(all(p["allow_rename"] is False for p in window["panes"]))
        self.window_probe.assert_called_once_with("@7")
        self.identity_probe.assert_called_once_with("work")
        self.assertNotIn("#{window_name}", self.rows_probe.call_args.args[0][-1])

    def test_lost_window_name_is_a_capture_error_instead_of_guessed_name(self):
        self.window_probe.side_effect = CommandError("window disappeared")
        snapshot = capture.capture()
        self.assertEqual(snapshot["sessions"][0]["windows"], [])
        self.assertTrue(all("window names for @7: window disappeared" == error
                            for error in snapshot["capture_errors"]["tmux"]))

    def test_optional_metadata_is_validated_without_breaking_legacy_recipes(self):
        snapshot = capture.capture()
        session = snapshot["sessions"][0]
        window = session["windows"][0]
        for key, invalid, message in (("automatic_rename", "off", "automatic_rename"),
                                      ("name", "bad\0name", "window name")):
            old = window[key]
            window[key] = invalid
            with self.assertRaisesRegex(ValueError, message):
                storage.validate(snapshot)
            window[key] = old
        session["tmux_identity"] = {"session_id": "$4"}
        with self.assertRaisesRegex(ValueError, "runtime identity"):
            storage.validate(snapshot)
        session.pop("tmux_identity")
        window.pop("automatic_rename")
        for pane in window["panes"]:
            pane.pop("allow_rename")
        storage.validate(snapshot)


class TabIdentityCaptureTests(CaptureFixture, unittest.TestCase):
    TAB = "0123456789abcdef0123456789abcdef"

    def captured(self, tabs, *, start_ticks=None):
        start_ticks = start_ticks or {}

        class Process:
            def __init__(self, ticks):
                self.start_ticks = ticks

        self.rows_probe.side_effect = [[], [
            ["work", "2", "@7", "layout", "1", "1", "%10", "11", "/tmp", "sh", "1", "@7"],
            ["work", "2", "@7", "layout", "1", "2", "%12", "13", "/tmp", "sh", "0", "@7"],
        ]]
        self.stack.enter_context(patch.object(
            capture, "read_pane_tab_id", side_effect=lambda pane_id, **_: tabs.get(pane_id)))
        self.stack.enter_context(patch.object(
            capture, "_process_identity",
            side_effect=lambda pid, _root: Process(start_ticks.get(pid, pid))))
        return capture.capture()["sessions"][0]["windows"][0]["panes"]

    def test_pane_records_the_tab_id_its_shell_published(self):
        panes = self.captured({"%10": self.TAB})
        self.assertEqual(panes[0]["tab_id"], self.TAB)
        self.assertNotIn("tab_id", panes[1])
        self.assertEqual(panes[0]["command"], "sh")
        self.assertNotIn("conversation", panes[0])
        self.assertIsNone(panes[0]["codex"])

    def test_a_pane_started_from_a_tab_does_not_claim_the_same_id(self):
        # Pane %12 runs a process that started later, yet inherited the ID.
        panes = self.captured({"%10": self.TAB, "%12": self.TAB}, start_ticks={11: 500, 13: 900})
        self.assertEqual([pane.get("tab_id") for pane in panes], [self.TAB, None])
        panes = self.captured({"%10": self.TAB, "%12": self.TAB}, start_ticks={11: 900, 13: 500})
        self.assertEqual([pane.get("tab_id") for pane in panes], [None, self.TAB])

    def test_a_pane_that_runs_a_conversation_or_dashboard_keeps_no_tab_id(self):
        with patch(
                "workspace_state.conversations.capture_fields",
                return_value={"codex": {"pid": 11, "session_id": "0" * 8 + "-0000-4000-8000-" + "0" * 12}}):
            panes = self.captured({"%10": self.TAB})
        self.assertNotIn("tab_id", panes[0])

    def test_reading_the_tab_id_accepts_only_32_lowercase_hexadecimal_characters(self):
        from workspace_state import tmux_names
        for value, expected in ((self.TAB, self.TAB), (self.TAB + "\n", self.TAB), ("", None),
                                (self.TAB.upper(), None), (self.TAB[:-1], None), (self.TAB + "0", None),
                                ("not an id", None), (self.TAB + "\n" + self.TAB, None)):
            with self.subTest(value=value):
                result = tmux_names.read_pane_tab_id("%1", command_runner=lambda argv, value=value: value)
                self.assertEqual(result, expected)

    def test_snapshot_with_tab_ids_stays_schema_7_and_validates(self):
        from workspace_state import checkpoint
        snapshot = {**checkpoint.migrate({"version": 7, "sessions": []}), "version": 7}
        panes = self.captured({"%10": self.TAB})
        snapshot["sessions"] = [{"name": "work", "windows": [{
            "index": 2, "name": "x", "layout": "l", "panes": panes}]}]
        storage.validate(snapshot)
        self.assertEqual(checkpoint.CURRENT_VERSION, 7)
        self.assertEqual(checkpoint.migrate(snapshot)["sessions"][0]["windows"][0]["panes"][0]["tab_id"], self.TAB)
