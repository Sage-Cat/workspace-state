import threading
import unittest
from unittest.mock import patch

from workspace_state import social_apps as social
from workspace_state.util import CommandError


def placement():
    return {
        "workspace": 1, "workspace_name": "Work", "monitor": 0,
        "monitor_identity": {"connector": "HDMI-1"},
        "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
        "geometry_relative": {"x": 0, "y": 0, "width": 800, "height": 600},
        "state": "normal",
    }


def records(*windowed):
    result = {
        app.id: {"running": False, "mode": "stopped", "windows": []}
        for app in social.APPS
    }
    for app_id in windowed:
        result[app_id] = {"running": True, "mode": "windowed", "windows": [placement()]}
    return result


class ParallelSocialAppsTests(unittest.TestCase):
    def setUp(self):
        shell = patch.object(social, "capture_shell", return_value={"available": True, "windows": []})
        shell.start()
        self.addCleanup(shell.stop)

    def test_two_apps_launch_concurrently_and_join(self):
        records_to_restore = records("slack", "discord")
        entered = threading.Barrier(2)
        active = 0
        peak = 0
        guard = threading.Lock()

        def launch(*_args):
            nonlocal active, peak
            with guard:
                active += 1
                peak = max(peak, active)
            entered.wait(timeout=2)
            with guard:
                active -= 1

        def verify(app, *_args, **_kwargs):
            return 100 if app.id == "slack" else 101

        with patch.object(social, "_target", side_effect=lambda item: item), \
             patch.object(social, "matching_windows", return_value=[]), \
             patch.object(social, "desktop_id", side_effect=lambda app: app.id), \
             patch.object(social, "launch_graphical_service", side_effect=launch), \
             patch.object(social, "expect_window", return_value=None), \
             patch.object(social, "_restore_window", side_effect=verify):
            self.assertEqual(social.restore_social_apps(records_to_restore, timeout=2), 2)
        self.assertGreaterEqual(peak, 2)

    def test_background_and_stopped_apps_never_launch(self):
        records_to_restore = records()
        records_to_restore["slack"] = {"running": True, "mode": "background", "windows": []}
        with patch.object(social, "launch_graphical_service") as launch:
            self.assertEqual(social.restore_social_apps(records_to_restore), 0)
        launch.assert_not_called()

    def test_errors_join_all_workers_and_progress_is_monotonic(self):
        records_to_restore = records("slack", "discord")
        attempted = []
        progress = []

        def launch(args, purpose):
            attempted.append(purpose)
            raise CommandError("launcher unavailable")

        with patch.object(social, "_target", side_effect=lambda item: item), \
             patch.object(social, "matching_windows", return_value=[]), \
             patch.object(social, "desktop_id", side_effect=lambda app: app.id), \
             patch.object(social, "launch_graphical_service", side_effect=launch):
            with self.assertRaisesRegex(CommandError, "Slack:.*Discord:"):
                social.restore_social_apps(records_to_restore, reporter=lambda *_args: progress.append(_args[2]))
        self.assertEqual(sorted(attempted), ["social-discord", "social-slack"])
        self.assertEqual(progress, sorted(progress))


if __name__ == "__main__":
    unittest.main()
