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
        self.shell = {"available": True, "active_workspace": 0, "windows": [],
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

    def move_window(self, window_id, target):
        self.assertEqual(window_id, self.window["id"])
        self.window["workspace"] = target["workspace"]
        deferred = target["workspace"] != self.shell["active_workspace"]
        if not deferred:
            self.window.update(copy.deepcopy(target))
        return {"placed": True, "deferred": deferred}

    def launch_saved(self, **kwargs):
        return restore.launch_terminal({"name": "work", "placement": self.target}, **kwargs)

    def existing(self):
        self.shell["windows"] = [self.window]
        return {"session": "work", "placement": {"id": 42}, "alacritty_pid": 123}

    def test_new_window_is_staged_and_verified_before_inactive_handoff(self):
        result = self.launch_saved()
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.window["workspace"], 3)
        self.assertEqual(self.window["monitor"], 1)
        self.assertEqual(self.window["state"], "maximized")
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [0, 3])
        self.assertGreaterEqual(self.clock, 1)
        self.launch.assert_called_once()

    def test_accepted_but_unapplied_request_is_not_success(self):
        self.move.side_effect = None
        self.move.return_value = {"placed": True, "deferred": True}
        result = restore.place_terminal(self.existing(), self.target)
        self.assertFalse(result.success)
        self.assertIn("observed", result.message)
        self.assertIn("monitor 0", result.message)
        self.assertLessEqual(self.clock, 13)
        self.launch.assert_not_called()

    def test_async_placement_replies_are_observed_through_final_handoff(self):
        pending = {}
        def move(window_id, target):
            pending.update(copy.deepcopy(target))
            return {"ok": True, "placed": False, "status": "applied", "token": "placement-1"}
        def sleep(seconds):
            self.sleep(seconds)
            if pending:
                self.window.update(pending)
                pending.clear()
        self.move.side_effect = move
        with patch.object(restore.time, "sleep", side_effect=sleep):
            result = self.launch_saved()
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.window["workspace"], 3)
        self.assertEqual(self.window["monitor"], 1)
        self.assertEqual(self.window["state"], "maximized")
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [0, 3])
        self.assertGreaterEqual(self.clock, 1.5)

    def test_async_acceptance_without_observed_placement_times_out(self):
        self.move.side_effect = None
        self.move.return_value = {"placed": False, "status": "accepted", "token": "placement-1"}
        result = restore.place_terminal(self.existing(), self.target)
        self.assertFalse(result.success)
        self.assertIn("verification timed out", result.message)
        self.assertIn("observed workspace 0, monitor 0", result.message)
        self.assertLessEqual(self.clock, 13)
        self.assertGreater(self.move.call_count, 1)
        self.assertLessEqual(self.move.call_count, 6)
        self.assertTrue(all(call.args[1]["workspace"] == 0 for call in self.move.call_args_list))

    def test_accepted_stage_drift_is_retried_within_original_deadline(self):
        self.target["state"] = "normal"
        staged_at = []
        def move(window_id, target):
            if target["workspace"] == 0:
                staged_at.append(self.clock)
                self.window.update(copy.deepcopy(target))
                if len(staged_at) == 1:
                    # Monitor recovery gets the display right but leaves the
                    # launch rectangle until the full placement is retried.
                    self.window["geometry"]["x"] += 90
            else:
                self.assertGreaterEqual(self.clock - staged_at[-1], .4)
                return self.move_window(window_id, target)
            return {"status": "applied", "token": "placement-1"}
        self.move.side_effect = move
        result = self.launch_saved()
        self.assertTrue(result.success, result.message)
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [0, 0, 3])
        self.assertGreaterEqual(staged_at[1] - staged_at[0], 2.2)
        self.assertLess(self.clock, 5)

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
        self.assertIn("GNOME rejected Alacritty placement", result.message)
        self.assertEqual(self.clock, 0)

    def test_existing_correct_window_is_not_moved_or_relaunched(self):
        self.window.update(self.target)
        result = restore.place_terminal(self.existing(), self.target)
        self.assertTrue(result.success, result.message)
        self.move.assert_not_called()
        self.launch.assert_not_called()
        self.assertGreaterEqual(self.clock, 1)

    def test_staging_waits_for_async_resize_before_final_workspace(self):
        pending = {}
        def move(window_id, target):
            if target["workspace"] == 0:
                pending.update(copy.deepcopy(target))
                self.window["workspace"] = 0
                return {"placed": True, "deferred": False}
            self.assertEqual(self.window["monitor"], 1)
            self.assertGreaterEqual(self.clock, .5)
            return self.move_window(window_id, target)
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= .5 and pending:
                self.window.update(pending)
                pending.clear()
        self.move.side_effect = move
        with patch.object(restore.time, "sleep", side_effect=sleep):
            result = self.launch_saved()
        self.assertTrue(result.success, result.message)

    def test_maximized_flags_do_not_complete_staging_before_frame_resize(self):
        self.window.update(self.target)
        self.window["geometry"] = {"x": 0, "y": 1080, "width": 3840, "height": 2030}
        def move(window_id, target):
            self.window["workspace"] = target["workspace"]
            if target["workspace"] == 3:
                self.assertGreaterEqual(self.clock, 1.1)
                self.assertEqual(self.window["geometry"], self.target["geometry"])
            return {"placed": False, "status": "applied", "token": "resize"}
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= .8:
                self.window["geometry"] = self.target["geometry"]
        self.move.side_effect = move
        with patch.object(restore.time, "sleep", side_effect=sleep):
            result = restore.place_terminal(self.existing(), self.target)
        self.assertTrue(result.success, result.message)
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [0, 3])

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

    def test_transient_correct_sample_then_monitor_drift_is_repaired(self):
        self.window.update(self.target)
        client = self.existing()
        drifted = False
        def sleep(seconds):
            nonlocal drifted
            self.sleep(seconds)
            if self.clock >= .5 and not drifted:
                drifted = True
                self.window.update(monitor=0, state="normal")
        with patch.object(restore.time, "sleep", side_effect=sleep):
            result = restore.place_terminal(client, self.target)
        self.assertTrue(result.success, result.message)
        self.assertEqual(self.window["monitor"], 1)
        self.assertGreaterEqual(self.clock, 1.9)

    def test_ambiguous_pid_never_moves_either_window(self):
        self.shell["windows"] = [self.window, dict(self.window, id=43)]
        result = restore.place_terminal({"session": "work", "alacritty_pid": 123}, self.target)
        self.assertFalse(result.success)
        self.assertIn("ambiguous", result.message)
        self.move.assert_not_called()
        self.launch.assert_not_called()

    def test_closed_window_is_not_retargeted_to_another_terminal(self):
        self.window.update(self.target)
        client = self.existing()
        def sleep(seconds):
            self.sleep(seconds)
            self.shell["windows"] = [dict(self.window, id=43, pid=456)]
        with patch.object(restore.time, "sleep", side_effect=sleep):
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
        self.assertEqual([call.args[1]["workspace"] for call in self.move.call_args_list], [0, 3])
