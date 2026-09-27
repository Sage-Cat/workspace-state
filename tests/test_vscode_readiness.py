import unittest
from unittest.mock import patch

from workspace_state import vscode
from workspace_state.util import CommandError


class _Clock:
    def __init__(self, minimum_sleep=0.0):
        self.now = 0.0
        self.minimum_sleep = minimum_sleep

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, self.minimum_sleep)


class _Live:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, deadline, *, strict=True):
        self.calls.append((deadline, strict))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class VscodeReadinessTests(unittest.TestCase):
    @staticmethod
    def _record():
        return {
            "provider": "vscode",
            "version": 1,
            "windows": [{
                "kind": "folder",
                "workspace_file": None,
                "folders": [{"uri": "file:///tmp/demo", "name": "demo"}],
                "editor_uris": [],
                "dirty_count": 0,
                "profile": {"id": "default", "name": "Default"},
                "user_data_dir": "/tmp/code-data",
                "remote_name": None,
                "placement": {
                    "workspace": 0,
                    "workspace_name": "Work",
                    "monitor": 0,
                    "monitor_intent": {"connector": "DP-1"},
                    "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
                    "geometry_relative": {"x": 0, "y": 0, "width": 800, "height": 600},
                    "state": "normal",
                },
            }],
        }

    def test_delayed_activation_after_previous_eight_second_cutoff_is_retried(self):
        clock = _Clock(minimum_sleep=1.0)
        live = _Live([vscode.CompanionNotReady("companion missing")] * 9 + [[
            {"instance": "one", "shell": {"id": 1}},
        ]])
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            result = vscode._wait_for_live_windows(live, 15.0)
        self.assertEqual(result[0]["instance"], "one")
        self.assertGreaterEqual(len(live.calls), 10)

    def test_preexisting_window_waits_for_its_delayed_companion(self):
        clock = _Clock()
        ready = {"instance": "one", "user_data_dir": "/code", "shell": {"id": 1}}
        live = _Live([vscode.CompanionNotReady("companion missing")] + [[ready]] * 8)
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            result = vscode._wait_for_live_windows(live, 3.0)
        self.assertEqual(result[0]["instance"], "one")
        self.assertGreater(len(live.calls), 1)

    def test_missing_companion_times_out_without_launching_editor(self):
        clock = _Clock(minimum_sleep=0.2)
        live = _Live([vscode.CompanionNotReady("companion missing")] * 20)
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ), patch.object(vscode, "launch_graphical_service") as launch:
            with self.assertRaisesRegex(CommandError, "startup readiness timed out"):
                vscode._wait_for_live_windows(live, 1.0)
        launch.assert_not_called()

    def test_restore_does_not_launch_while_initial_companion_readiness_times_out(self):
        clock = _Clock()
        live = _Live([vscode.CompanionNotReady("companion missing")] * 20)
        with patch.object(vscode, "_LiveWindows", return_value=live), patch.object(
            vscode, "launch_graphical_service",
        ) as launch, patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            with self.assertRaisesRegex(CommandError, "startup readiness timed out"):
                vscode.restore_vscode(self._record(), no_place=True, timeout=1)
        launch.assert_not_called()

    def test_bootstrap_waits_past_eight_seconds_before_project_restore(self):
        record = self._record()
        item = record["windows"][0]
        live_item = {
            **item,
            "instance": "one",
            "endpoint": "/tmp/one.sock",
            "shell": {"id": 7, "workspace": 0, "monitor": 0, "state": "normal"},
        }
        clock = _Clock(minimum_sleep=1.0)
        live = _Live([[]] + [vscode.CompanionNotReady("companion starting")] * 9 + [[live_item]] * 2)

        def request(_endpoint, method, **_kwargs):
            return {"ready": True} if method == "probe" else item

        with patch.object(vscode, "_LiveWindows", return_value=live), patch.object(
            vscode, "launch_graphical_service",
        ) as launch, patch.object(vscode, "_verify_profile"), patch.object(
            vscode, "_check_resource",
        ), patch.object(vscode, "_request", side_effect=request), patch.object(
            vscode.time, "monotonic", side_effect=clock.monotonic,
        ), patch.object(vscode.time, "sleep", side_effect=clock.sleep):
            self.assertEqual(vscode.restore_vscode(record, no_place=True), 1)

        self.assertGreater(clock.now, 8)
        self.assertTrue(all(strict for _, strict in live.calls))
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "vscode-native-recovery")

    def test_bootstrap_timeout_does_not_launch_a_duplicate_project(self):
        clock = _Clock()
        live = _Live([[]] + [vscode.CompanionNotReady("companion starting")] * 20)
        with patch.object(vscode, "_LiveWindows", return_value=live), patch.object(
            vscode, "launch_graphical_service",
        ) as launch, patch.object(vscode, "_verify_profile"), patch.object(
            vscode, "_check_resource",
        ), patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            with self.assertRaisesRegex(CommandError, "startup readiness timed out"):
                vscode.restore_vscode(self._record(), no_place=True, timeout=1)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "vscode-native-recovery")

    def test_late_native_project_is_rechecked_before_launching_another_window(self):
        clock = _Clock()
        record = self._record()
        item = record["windows"][0]
        unrelated = {**item, "folders": [{"uri": "file:///tmp/other", "name": "other"}],
                     "instance": "other", "shell": {"id": 2}, "endpoint": "/tmp/other.sock"}
        ready = {**item, "instance": "saved", "shell": {"id": 1}, "endpoint": "/tmp/saved.sock"}
        live = _Live([[unrelated], vscode.CompanionNotReady("native recovery still activating"), [unrelated, ready]])
        with patch.object(vscode, "_LiveWindows", return_value=live), patch.object(
            vscode, "launch_graphical_service",
        ) as launch, patch.object(vscode, "_request", side_effect=lambda _, method, **kw: {"ready": True} if method == "probe" else item), patch.object(
            vscode.time, "monotonic", side_effect=clock.monotonic,
        ), patch.object(vscode.time, "sleep", side_effect=clock.sleep):
            self.assertEqual(vscode.restore_vscode(record, no_place=True), 1)
        launch.assert_not_called()
        self.assertTrue(all(strict for _, strict in live.calls))

    def test_project_launch_waits_for_busy_companion_without_duplicate_launch(self):
        clock = _Clock()
        record = self._record()
        item = record["windows"][0]
        unrelated = {**item, "folders": [{"uri": "file:///tmp/other", "name": "other"}],
                     "instance": "other", "shell": {"id": 2}, "endpoint": "/tmp/other.sock"}
        ready = {**item, "instance": "saved", "shell": {"id": 1}, "endpoint": "/tmp/saved.sock"}
        live = _Live([[unrelated], [unrelated], vscode.CompanionNotReady("busy host"), [unrelated, ready]])
        with patch.object(vscode, "_LiveWindows", return_value=live), patch.object(
            vscode, "launch_graphical_service",
        ) as launch, patch.object(vscode, "_request", side_effect=lambda _, method, **kw: {"ready": True} if method == "probe" else item), patch.object(
            vscode, "_verify_profile",
        ), patch.object(vscode, "_check_resource"), patch.object(
            vscode.time, "monotonic", side_effect=clock.monotonic,
        ), patch.object(vscode.time, "sleep", side_effect=clock.sleep):
            self.assertEqual(vscode.restore_vscode(record, no_place=True), 1)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.args[1], "vscode-project")

    def test_native_window_created_during_discovery_is_not_treated_as_ready(self):
        item = self._record()["windows"][0]
        first = {"id": 1, "app_id": "code"}
        second = {"id": 2, "app_id": "code"}
        live = vscode._LiveWindows()
        live.ids["one"] = 1
        with patch.object(vscode, "capture_shell", side_effect=[
            {"available": True, "windows": [first]},
            {"available": True, "windows": [first, second]},
        ]), patch.object(vscode, "_states", return_value=[{
            **item, "instance": "one", "endpoint": "/tmp/one.sock",
        }]):
            with self.assertRaises(vscode.CompanionNotReady):
                live.get(30)

    def test_bootstrap_waits_for_every_native_window_not_only_first_companion(self):
        clock = _Clock()
        item = self._record()["windows"][0]
        natives = [{"id": i, "app_id": "code"} for i in (1, 2)]
        states = [{**item, "instance": str(i), "endpoint": f"/tmp/{i}.sock"} for i in (1, 2)]
        live = vscode._LiveWindows()
        live.ids = {"1": 1, "2": 2}
        with patch.object(vscode, "capture_shell", return_value={"available": True, "windows": natives}), patch.object(
            vscode, "_states", side_effect=lambda deadline: states[:1] if clock.now < 9 else states,
        ), patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            result = vscode._wait_for_live_windows(live, 15, scope=item["user_data_dir"])
        self.assertEqual(len(result), 2)
        self.assertGreaterEqual(clock.now, 10)

    def test_hud_reports_waits_without_flooding_log(self):
        clock = _Clock()
        live = _Live([vscode.CompanionNotReady("companion starting")] * 100)
        messages = []
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            with self.assertRaises(CommandError):
                vscode._wait_for_live_windows(live, 5, reporter=messages.append)
        self.assertEqual(len(messages), 3)
        self.assertTrue(all('companion starting' in message for message in messages))

    def test_partial_native_readiness_retries_until_all_windows_are_represented(self):
        clock = _Clock()
        live = _Live([
            vscode.CompanionNotReady("companion missing for an open window"),
            [
                {"instance": "one", "shell": {"id": 1}},
                {"instance": "two", "shell": {"id": 2}},
            ],
        ])
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            result = vscode._wait_for_live_windows(live, 3.0)
        self.assertEqual({item["instance"] for item in result}, {"one", "two"})

    def test_fatal_companion_error_is_not_retried(self):
        clock = _Clock()
        fatal = CommandError("Unsafe VS Code companion endpoint permissions")
        live = _Live([fatal])
        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep,
        ):
            with self.assertRaisesRegex(CommandError, "permissions"):
                vscode._wait_for_live_windows(live, 10.0)
        self.assertEqual(len(live.calls), 1)


if __name__ == "__main__":
    unittest.main()
