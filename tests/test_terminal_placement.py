from __future__ import annotations

import copy
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from workspace_state import restore
from workspace_state.util import CommandError


class TerminalPlacementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.clock = 0.0
        self.target = {
            "workspace": 3, "workspace_name": "Students", "monitor": 1,
            "monitor_identity": {"connector": "HDMI-1", "serial": "left-display"},
            "monitor_geometry": {"x": 0, "y": 1080, "width": 1920, "height": 1080},
            "state": "maximized",
            "geometry": {"x": 0, "y": 1080, "width": 1920, "height": 1080},
        }
        self.window = {
            "id": 42, "pid": 123, "app_ids": ["alacritty"],
            "workspace": 0, "monitor": 0, "state": "normal",
            "geometry": {"x": 1920, "y": 0, "width": 1000, "height": 700},
        }
        self.shell = {"available": True, "active_workspace": 0, "windows": [], "capabilities": ["staged_placement_handoff"],
                      "workspaces": [{"index": 3, "name": "Students"}]}
        self.stack.enter_context(patch.object(restore, "capture_shell", side_effect=lambda: copy.deepcopy(self.shell)))
        self.stack.enter_context(patch.object(restore, "remap_monitor", side_effect=lambda value, **kw: value))
        self.stack.enter_context(patch.object(restore, "remap_workspace", side_effect=lambda value: value))
        self.stack.enter_context(patch.object(restore.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(restore.time, "sleep", side_effect=self.sleep))
        self.launch = self.stack.enter_context(patch.object(restore, "launch_graphical_service", side_effect=self.launch_window))
        self.move = self.stack.enter_context(patch.object(restore, "move_window_result", side_effect=self.move_window))
        # Keep the old implementation's unverified boolean fallbacks isolated
        # too while demonstrating the regression before the fix.
        self.stack.enter_context(patch.object(restore, "place_by_title", return_value=True, create=True))
        self.stack.enter_context(patch.object(restore, "place_by_pid", return_value=True, create=True))

    def sleep(self, seconds):
        self.clock += seconds

    def launch_window(self, command, _label):
        self.window["title"] = command[command.index("--title") + 1]
        self.shell["windows"] = [self.window]

    def move_window(self, window_id, target, **kwargs):
        self.assertEqual(window_id, self.window["id"])
        self.assertTrue(target["stage_before_handoff"])
        self.window.update(copy.deepcopy(target))
        return {"status": "verified", "handoff_owned": True}

    def launch_saved(self, **kwargs):
        return restore.launch_terminal({"name": "work", "placement": self.target}, **kwargs)

    def existing(self):
        self.shell["windows"] = [self.window]
        return {"session": "work", "placement": {"id": 42}, "alacritty_pid": 123}


    def test_locked_native_handoff_returns_exact_receipt_without_consuming_timeout(self):
        self.shell["capabilities"] = ["staged_placement_handoff"]
        self.move.side_effect = None
        self.move.return_value = {"status": "deferred", "token": "owned-42", "handoff_owned": True}
        result = self.launch_saved()
        self.assertFalse(result.success)
        self.assertEqual(result.request_id, "native-handoff:owned-42")
        self.assertEqual(self.clock, 0)
        self.move.assert_called_once_with(42, dict(self.target, stage_before_handoff=True), timeout=10)
        self.assertEqual(self.window["workspace"], 0)

    def test_existing_terminal_rejects_an_unowned_native_receipt(self):
        self.shell["capabilities"] = ["staged_placement_handoff"]
        self.move.side_effect = None
        self.move.return_value = {"status": "deferred", "token": "old-staging"}
        result = restore.place_terminal(self.existing(), self.target)
        self.assertFalse(result.success)
        self.assertIsNone(result.request_id)
        self.assertIn("did not retain", result.message)


    def test_slow_window_query_cannot_submit_after_deadline(self):
        self.existing()
        def capture():
            self.clock += 13
            return copy.deepcopy(self.shell)
        with patch.object(restore, "capture_shell", side_effect=capture):
            with self.assertRaisesRegex(CommandError, "verification timed out"):
                restore._verify_terminal_placement({"id": 42}, self.target)
        self.move.assert_not_called()

    def test_rejected_placement_fails_without_waiting(self):
        self.move.side_effect = None
        self.move.return_value = {"placed": False, "status": "not_found"}
        result = restore.place_terminal(self.existing(), self.target)
        self.assertFalse(result.success)
        self.assertIn("GNOME rejected final Alacritty placement", result.message)
        self.assertEqual(self.clock, 0)


    def test_missing_saved_display_fails_before_launch(self):
        with patch.object(restore, "remap_monitor", side_effect=CommandError("saved display is missing")):
            result = self.launch_saved()
        self.assertFalse(result.success)
        self.assertIn("saved display is missing", result.message)
        self.launch.assert_not_called()

    def test_dry_run_has_no_desktop_side_effects(self):
        self.assertTrue(self.launch_saved(dry_run=True).success)
        self.assertTrue(restore.place_terminal(self.existing(), self.target, dry_run=True).success)
        self.launch.assert_not_called()
        self.move.assert_not_called()

    def test_no_place_launch_does_not_inspect_or_move_windows(self):
        with patch.object(restore, "capture_shell") as capture:
            self.assertTrue(self.launch_saved(place=False).success)
        capture.assert_not_called()
        self.move.assert_not_called()


    def test_ambiguous_pid_never_moves_either_window(self):
        self.shell["windows"] = [self.window, dict(self.window, id=43)]
        result = restore.place_terminal({"session": "work", "alacritty_pid": 123}, self.target)
        self.assertFalse(result.success)
        self.assertIn("ambiguous", result.message)
        self.move.assert_not_called()
        self.launch.assert_not_called()

    def test_closed_window_is_not_retargeted_to_another_terminal(self):
        client = self.existing()
        self.shell["windows"] = [dict(self.window, id=43, pid=456)]
        result = restore.place_terminal(client, self.target)
        self.assertFalse(result.success)
        self.assertIn("window 42 disappeared", result.message)
        self.move.assert_not_called()

    def test_normal_window_size_is_verified_not_just_workspace_and_monitor(self):
        self.target["state"] = "normal"
        self.window.update(self.target)
        self.window["geometry"] = dict(self.target["geometry"], width=1000)
        result = restore.place_terminal(self.existing(), self.target)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.window["geometry"], self.target["geometry"])
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [3])
