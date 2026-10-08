from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli, login_status, social_apps as social, storage
from workspace_state.util import CommandError

REAL_RUNNING_APPS = social.running_apps

def placement():
    return {"workspace": 1, "workspace_name": "Work", "monitor": 2,
            "monitor_identity": {"connector": "HDMI-2", "serial": "display-1"},
            "monitor_geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080},
            "geometry": {"x": 1920, "y": 0, "width": 1920, "height": 1080},
            "geometry_relative": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "state": "maximized"}


def records():
    return {app.id: {"running": False, "mode": "stopped", "windows": []} for app in social.APPS}


def visible(app="slack", window_id=1):
    return dict(placement(), id=window_id, pid=123, app_ids=[app])


class SocialAppTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.shell = {"available": True, "active_workspace": 0,
                      "workspaces": [{"index": 1, "name": "Work"}],
                      "monitors": [{"index": 2}], "windows": []}
        self.stack.enter_context(patch.object(social, "capture_shell", side_effect=lambda: self.shell))
        self.stack.enter_context(patch.object(social, "running_apps", return_value=set()))
        self.launch = self.stack.enter_context(patch.object(social, "launch_graphical_service"))
        self.desktop = self.stack.enter_context(patch.object(social, "desktop_id", return_value="slack_slack"))
        self.expect = self.stack.enter_context(patch.object(social, "expect_window", return_value="token"))
        self.cancel = self.stack.enter_context(patch.object(social, "cancel_expected_window"))
        self.stack.enter_context(patch("workspace_state.desktop.remap_monitor", side_effect=lambda item, **kw: item))
        self.stack.enter_context(patch("workspace_state.desktop.remap_workspace", side_effect=lambda item, **kwargs: item))
        self.clock = 0.0
        self.stack.enter_context(patch.object(social.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(social.time, "sleep", side_effect=self.sleep))

    def sleep(self, seconds):
        self.clock += seconds

    def test_capture_distinguishes_windows_background_minimized_and_stopped(self):
        self.shell["windows"] = [visible(), dict(visible("discord", 2), state="minimized")]
        with patch.object(social, "running_apps", return_value={"telegram"}):
            saved = social.capture_social_apps(self.shell)
        self.assertEqual(saved["slack"]["mode"], "windowed")
        self.assertEqual(saved["slack"]["windows"][0]["workspace_name"], "Work")
        self.assertEqual(saved["discord"], {"running": True, "mode": "background", "windows": []})
        self.assertEqual(saved["telegram"]["mode"], "background")
        self.assertEqual(saved["viber"]["mode"], "stopped")

    def test_process_probe_uses_executable_not_bot_name_or_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for pid, executable in ((1, "slack"), (2, "Discord"), (3, "telegram-desktop"), (4, "python3")):
                target = root / "bin" / executable
                target.parent.mkdir(exist_ok=True)
                target.touch()
                process = root / str(pid)
                process.mkdir()
                (process / "exe").symlink_to(target)
                (process / "cmdline").write_bytes(b"viber discord-server-bot")
            self.assertEqual(REAL_RUNNING_APPS(root), {"slack", "discord", "telegram"})

    def test_unavailable_shell_is_not_misrecorded_as_everything_background(self):
        with self.assertRaises(CommandError):
            social.capture_social_apps({"available": False})

    def test_background_and_stopped_never_launch_or_place_even_if_running_now(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "background", "windows": []}
        self.shell["windows"] = [visible()]
        with patch.object(social, "move_window_result") as move:
            self.assertEqual(social.restore_social_apps(saved), 0)
        self.launch.assert_not_called()
        self.desktop.assert_not_called()
        move.assert_not_called()

    def test_legacy_snapshot_never_guesses_apps_to_launch(self):
        self.assertEqual(social.restore_social_apps(None), 0)
        self.launch.assert_not_called()

    def test_existing_correct_window_is_reused_without_moving(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [visible()]
        with patch.object(social, "move_window_result") as move:
            self.assertEqual(social.restore_social_apps(saved), 1)
            self.assertEqual(social.restore_social_apps(saved), 1)
        self.launch.assert_not_called()
        move.assert_not_called()

    def test_missing_visible_app_launches_once_and_verifies_window(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.launch.side_effect = lambda *_args: self.shell["windows"].append(visible())
        self.assertEqual(social.restore_social_apps(saved), 1)
        self.launch.assert_called_once_with(["/usr/bin/gtk-launch", "slack_slack"], "social-slack")
        self.expect.assert_called_once()
        self.cancel.assert_called_once_with("token")

    def test_wrong_workspace_is_staged_then_placed_at_saved_destination(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [dict(visible(), workspace=3, monitor=0)]
        def move(_window_id, target):
            self.shell["windows"][0].update(target)
            return {"placed": True}
        with patch.object(social, "move_window_result", side_effect=move) as moved:
            self.assertEqual(social.restore_social_apps(saved), 1)
        self.assertEqual([call.args[1]["workspace"] for call in moved.call_args_list], [0, 1])
        self.launch.assert_not_called()

    def test_maximized_frame_waits_for_resize_before_inactive_handoff(self):
        saved = records()
        saved["viber"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        window = dict(visible("viber"), geometry={"x": 1920, "y": 0, "width": 3840, "height": 2030})
        self.shell["windows"] = [window]
        def move(_window_id, target):
            window.update(workspace=target["workspace"], monitor=target["monitor"], state=target["state"])
            if target["workspace"] == 1:
                self.assertGreaterEqual(self.clock, 1.1)
                self.assertEqual(window["geometry"], placement()["geometry"])
            return {"placed": False, "status": "applied", "token": "resize-1"}
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= .8:
                window["geometry"] = placement()["geometry"]
        with patch.object(social, "move_window_result", side_effect=move) as moved, patch.object(
            social.time, "sleep", side_effect=sleep,
        ):
            self.assertEqual(social.restore_social_apps(saved, timeout=5), 1)
        self.assertEqual([call.args[1]["workspace"] for call in moved.call_args_list], [0, 1])
        self.launch.assert_not_called()

    def test_pending_resize_retries_only_staging_within_fixed_deadline(self):
        saved = records()
        saved["viber"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        window = dict(visible("viber"), geometry={"x": 1920, "y": 0, "width": 3840, "height": 2030})
        self.shell["windows"] = [window]
        def move(_window_id, target):
            window["workspace"] = target["workspace"]
            return {"placed": False, "status": "applied", "token": "resize-1"}
        with patch.object(social, "move_window_result", side_effect=move) as moved:
            with self.assertRaises(social.ProviderRestoreError) as raised:
                social.restore_social_apps(saved, timeout=20)
        self.assertEqual(raised.exception.results[0].placement.state, social.EvidenceState.FAILED)
        self.assertIsNone(raised.exception.results[0].placement.request_id)
        self.assertGreaterEqual(moved.call_count, 2)
        self.assertLessEqual(moved.call_count, 20)
        self.assertTrue(all(call.args[1]["workspace"] == 0 for call in moved.call_args_list))
        self.assertGreaterEqual(self.clock, 20)
        self.assertLessEqual(self.clock, 20.3)

    def test_late_frame_releases_gate_then_completes_within_app_budget(self):
        saved = records()
        saved["viber"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        window = dict(visible("viber", 25), workspace=0, monitor=0)
        self.shell["windows"] = [window]
        def move(_window_id, target):
            window["workspace"] = target["workspace"]
            return {"status": "applied", "token": "stage-or-final"}
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= 4.8:
                window.update(monitor=2, state="maximized", geometry=placement()["geometry"])
        with patch.object(social, "move_window_result", side_effect=move) as moved, patch.object(
            social.time, "sleep", side_effect=sleep
        ), patch.object(social, "placement_lock") as gate:
            gate.acquire.return_value = True
            self.assertEqual(social.restore_social_apps(saved, timeout=12), 1)
        self.assertEqual(gate.acquire.call_count, 2)
        self.assertEqual(gate.release.call_count, 2)
        self.assertEqual(moved.call_args_list[-1].args[1]["workspace"], 1)
        self.assertGreater(self.clock, 6)
        self.assertLess(self.clock, 12)

    def test_late_social_startup_frame_is_reapplied_before_handoff(self):
        for app, state in (("slack", "maximized"), ("viber", "normal")):
            with self.subTest(app=app):
                self.clock = 0
                target = dict(placement(), state=state)
                saved = records()
                saved[app] = {"running": True, "mode": "windowed", "windows": [target]}
                window = dict(visible(app), workspace=3, state=state,
                              geometry=dict(target["geometry"], x=1940))
                self.shell["windows"] = [window]
                stages = []
                def move(_window_id, destination):
                    if destination["workspace"] == 0:
                        stages.append(self.clock)
                        window["workspace"] = 0
                        # First placement is overwritten by the app's startup.
                        if len(stages) >= 2:
                            window.update(destination)
                    else:
                        self.assertGreaterEqual(self.clock - stages[-1], .4)
                        self.assertEqual(window["geometry"], target["geometry"])
                        window.update(destination)
                    return {"status": "applied", "token": "stage-request"}
                with patch.object(social, "move_window_result", side_effect=move) as moved:
                    self.assertEqual(social.restore_social_apps(saved, timeout=8), 1)
                self.assertEqual([call.args[1]["workspace"] for call in moved.call_args_list], [0, 0, 1])
                self.assertGreaterEqual(stages[1] - stages[0], 1)
                self.assertLess(self.clock, 5)

    def test_stage_reapply_cannot_extend_short_app_deadline(self):
        self.shell["windows"] = [dict(visible("viber", 25), workspace=3, monitor=0)]
        with patch.object(social, "move_window_result", return_value={"status": "applied"}) as moved:
            with self.assertRaisesRegex(social._StagingIncomplete, "staging did not settle"):
                social._place_social_window(social.APP_BY_ID["viber"], 25, placement(), 0, deadline=1.5)
        self.assertEqual(moved.call_count, 2)
        self.assertLessEqual(self.clock, 1.6)
        self.assertTrue(all(call.args[1]["workspace"] == 0 for call in moved.call_args_list))

    def test_slow_stage_observation_cannot_authorize_late_retry_or_handoff(self):
        for settled in (False, True):
            with self.subTest(settled=settled):
                self.clock = 0
                window = dict(visible("viber", 25), workspace=3, monitor=0)
                self.shell["windows"] = [window]
                captures = 0
                def capture():
                    nonlocal captures
                    captures += 1
                    if captures > 1:
                        self.clock += .5 if settled and captures == 2 else 2
                        if settled:
                            window.update(placement(), workspace=0)
                    return self.shell
                with patch.object(social, "capture_shell", side_effect=capture), patch.object(
                    social, "move_window_result", return_value={"status": "applied"}
                ) as moved:
                    with self.assertRaisesRegex(social._StagingIncomplete, "staging did not settle"):
                        social._place_social_window(social.APP_BY_ID["viber"], 25, placement(), 0, deadline=1.5)
                moved.assert_called_once()
                self.assertEqual(moved.call_args.args[1]["workspace"], 0)

    def test_maximized_work_area_ignores_stale_saved_rectangle(self):
        target = placement()
        window = visible()
        area = {"x": 1920, "y": 30, "width": 1920, "height": 1050}
        window["monitor_work_area"] = area
        self.assertFalse(social._placement_matches(window, target))
        window["geometry"] = area
        self.assertTrue(social._placement_matches(window, target))

    def test_busy_placement_gate_uses_remaining_app_deadline(self):
        self.clock = 4
        self.shell["windows"] = [dict(visible("viber", 25), monitor=0)]
        with patch.object(social, "placement_lock") as gate, patch.object(social, "move_window_result") as move:
            gate.acquire.return_value = False
            with self.assertRaisesRegex(CommandError, "deadline elapsed while waiting"):
                social._place_social_window(social.APP_BY_ID["viber"], 25, placement(), 0, deadline=7)
        gate.acquire.assert_called_once_with(timeout=3)
        gate.release.assert_not_called()
        move.assert_not_called()

    def test_placement_completed_while_queued_is_not_staged_again(self):
        window = dict(visible("viber", 25), monitor=0)
        self.shell["windows"] = [window]
        def acquire(**_kwargs):
            window.update(placement())
            return True
        with patch.object(social, "placement_lock") as gate, patch.object(social, "move_window_result") as move:
            gate.acquire.side_effect = acquire
            self.assertFalse(social._place_social_window(
                social.APP_BY_ID["viber"], 25, placement(), 0, deadline=7))
        gate.release.assert_called_once()
        move.assert_not_called()

    def test_launch_expectation_final_destination_completes_staging(self):
        saved = records()
        saved["viber"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        window = dict(visible("viber", 25), monitor=0)
        self.shell["windows"] = [window]
        def move(_window_id, target):
            window.update(target, state="normal")
            return {"placed": False, "status": "applied"}
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= .3:
                window.update(placement())
        with patch.object(social, "move_window_result", side_effect=move) as moved, patch.object(
            social.time, "sleep", side_effect=sleep,
        ):
            self.assertEqual(social.restore_social_apps(saved, timeout=5), 1)
        moved.assert_called_once()
        self.assertGreaterEqual(self.clock, 1.3)

    def test_discord_splash_is_replaced_by_main_window_without_relaunch(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        splash = dict(visible("discord", 19), workspace=4, state="normal")
        main = dict(visible("discord", 20), workspace=4)
        self.launch.side_effect = lambda *_args: self.shell["windows"].append(splash)
        def sleep(seconds):
            self.sleep(seconds)
            # The updater has a NORMAL GNOME window, but cannot be maximized.
            # It goes away before the real main window appears.
            if self.clock >= .8:
                self.shell["windows"] = [main]
            elif self.clock >= .4:
                self.shell["windows"] = []
        def move(window_id, target):
            if window_id == 20:
                main.update(target)
            return {"placed": True}
        messages = []
        with patch.object(social.time, "sleep", side_effect=sleep), patch.object(
            social, "move_window_result", side_effect=move
        ):
            self.assertEqual(social.restore_social_apps(saved, timeout=8,
                reporter=lambda *event: messages.append(event)), 1)
        self.assertEqual(main["workspace"], 1)
        self.assertTrue(any("replacement" in event[1] for event in messages))
        self.assertLess(self.clock, 8)
        self.launch.assert_called_once()
        self.cancel.assert_called_once_with("token")

    def test_newly_launched_splash_cannot_report_ready_before_main_window(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        splash = visible("discord", 19)
        main = dict(visible("discord", 20), workspace=4)
        self.launch.side_effect = lambda *_args: self.shell["windows"].append(splash)
        def sleep(seconds):
            self.sleep(seconds)
            if self.clock >= 1.6:
                self.shell["windows"] = [main]
        def move(_window_id, target):
            self.shell["windows"][0].update(target)
            return {"placed": True}
        with patch.object(social.time, "sleep", side_effect=sleep), patch.object(
            social, "move_window_result", side_effect=move
        ):
            self.assertEqual(social.restore_social_apps(saved, timeout=8), 1)
        self.assertEqual(self.shell["windows"][0]["id"], 20)
        self.assertEqual(main["workspace"], 1)

    def test_disappeared_window_times_out_with_useful_error(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [visible("discord", 19)]
        def sleep(seconds):
            self.sleep(seconds)
            self.shell["windows"] = []
        with patch.object(social.time, "sleep", side_effect=sleep):
            with self.assertRaisesRegex(CommandError, "window 19 disappeared.*replacement"):
                social.restore_social_apps(saved, timeout=2)
        self.launch.assert_not_called()

    def test_window_replacement_during_move_is_reacquired(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [dict(visible("discord", 19), workspace=4)]
        def move(window_id, target):
            if window_id == 19:
                self.shell["windows"] = [dict(visible("discord", 20), workspace=4)]
                return {"placed": False, "status": "not-found"}
            self.shell["windows"][0].update(target)
            return {"placed": True}
        with patch.object(social, "move_window_result", side_effect=move):
            self.assertEqual(social.restore_social_apps(saved, timeout=4), 1)
        self.assertEqual(self.shell["windows"][0]["id"], 20)
        self.assertEqual(self.shell["windows"][0]["workspace"], 1)
        self.launch.assert_not_called()

    def test_living_window_placement_rejection_remains_failed(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [dict(visible("discord", 20), workspace=4)]
        with patch.object(social, "move_window_result", return_value={"placed": False, "status": "rejected"}):
            with self.assertRaisesRegex(CommandError, "GNOME rejected placement of window 20"):
                social.restore_social_apps(saved)

    def test_replacement_never_steals_an_already_claimed_window(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement(), placement()]}
        self.shell["windows"] = [visible("discord", 18), dict(visible("discord", 19), workspace=4)]
        def move(window_id, target):
            if window_id == 19:
                self.shell["windows"] = [self.shell["windows"][0], dict(visible("discord", 20), workspace=4)]
                return {"placed": False, "status": "not-found"}
            self.shell["windows"][1].update(target)
            return {"placed": True}
        with patch.object(social, "move_window_result", side_effect=move):
            self.assertEqual(social.restore_social_apps(saved, timeout=5), 2)
        self.assertEqual([item["id"] for item in self.shell["windows"]], [18, 20])
        self.assertTrue(all(item["workspace"] == 1 for item in self.shell["windows"]))
        self.launch.assert_not_called()

    def test_wrong_placement_timeout_reports_expected_and_observed_state(self):
        saved = records()
        saved["discord"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [dict(visible("discord", 20), workspace=4)]
        with patch.object(social, "move_window_result", return_value={"placed": True}):
            with self.assertRaisesRegex(CommandError, "expected workspace Work.*observed workspace 4"):
                social.restore_social_apps(saved, timeout=2)

    def test_launch_failure_does_not_skip_other_apps_and_remains_failed(self):
        saved = records()
        for app in ("slack", "discord"):
            saved[app] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.shell["windows"] = [visible("discord", 2)]
        self.launch.side_effect = CommandError("not installed")
        messages = []
        with self.assertRaisesRegex(CommandError, "not installed"):
            social.restore_social_apps(saved, reporter=lambda *args: messages.append(args))
        self.assertTrue(any("Discord: restored" in event[1] for event in messages))
        self.assertEqual(messages[-1][0], "failed")

    def test_missing_display_fails_before_launch_instead_of_using_another(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        with patch("workspace_state.desktop.remap_monitor", side_effect=CommandError("display missing")):
            with self.assertRaisesRegex(CommandError, "display missing"):
                social.restore_social_apps(saved)
        self.launch.assert_not_called()

    def test_dry_run_does_not_launch_or_place(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.assertEqual(social.restore_social_apps(saved, dry_run=True), 1)
        self.launch.assert_not_called()
        self.expect.assert_not_called()

    def test_inconsistent_background_record_is_rejected(self):
        saved = records()
        saved["slack"] = {"running": True, "mode": "background", "windows": [placement()]}
        with self.assertRaises(ValueError):
            storage.validate({"sessions": [], "social_apps": saved})

    def test_terminal_autosave_retains_social_checkpoint(self):
        saved = records()
        previous = {"sessions": [], "social_apps": saved}
        with patch.object(cli, "load", return_value=previous), patch.object(cli, "capture", return_value={"sessions": []}), patch.object(
            cli, "_terminal_problems", return_value=[]
        ), patch.object(cli, "state_lock") as lock, patch.object(cli, "save") as save:
            cli._autosave_from_tmux()
        self.assertEqual(save.call_args.args[0]["social_apps"], saved)

    def test_hud_exposes_separate_startup_and_shutdown_jobs(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            login_status.initialize("social-test", show_startup_hud=False)
            payload = json.loads(login_status.status_path().read_text())
            self.assertIn("social-apps", [stage["id"] for stage in payload["stages"]])
            login_status.initialize_shutdown("social-test", "a" * 32)
            for index, app in enumerate(social.APPS, 1):
                login_status.update_stage("social-apps-save", "running", f"{app.label}: background; not launching", current=index, total=4)
            login_status.update_stage("social-apps-save", "ready", "Saved", current=4, total=4)
            payload = json.loads(login_status.status_path().read_text())
            stage = next(item for item in payload["stages"] if item["id"] == "social-apps-save")
            self.assertEqual(stage["current"], 4)
            self.assertEqual(len(stage["events"]), 5)

    def test_new_shutdown_job_works_with_an_already_running_old_coordinator(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}):
            login_status.initialize_shutdown("social-test", "b" * 32)
            payload = json.loads(login_status.status_path().read_text())
            payload["stages"] = [item for item in payload["stages"] if item["id"] != "social-apps-save"]
            login_status.status_path().write_text(json.dumps(payload))
            login_status.update_stage("social-apps-save", "ready", "Saved", current=4, total=4)
            payload = json.loads(login_status.status_path().read_text())
            ids = [stage["id"] for stage in payload["stages"]]
            self.assertNotIn("checkpoint-proof", ids)
            self.assertEqual(payload["stages"][ids.index("social-apps-save")]["label"], "Desktop app visibility and placement")
