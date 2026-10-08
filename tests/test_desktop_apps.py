import copy
import io
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import patch

from workspace_state import social_apps as social, storage
from workspace_state.util import CommandError


CONFIG = '''[[apps]]
id = "notes"
label = "Notes"
aliases = ["org.example.Notes", "notes-window"]
desktop_ids = ["org.example.Notes"]
executables = ["notes-bin"]
'''


def placement():
    return {
        "workspace": 1, "workspace_name": "Work", "monitor": 0,
        "monitor_identity": {"connector": "DP-1"},
        "monitor_intent": {"connector": "DP-1"},
        "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
        "geometry_relative": {"x": 0, "y": 0, "width": 800, "height": 600},
        "state": "normal",
    }


def records():
    return {app.id: {"running": False, "mode": "stopped", "windows": []} for app in social.APPS}


class DesktopAppTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_DATA_DIRS": str(self.root / "system-data"),
        }))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.path = self.root / "config/workspace-state/desktop-apps.toml"
        self.path.parent.mkdir(parents=True)

    def configure(self, contents=CONFIG):
        self.path.write_text(contents)

    def test_configuration_is_optional_and_read_for_each_operation(self):
        self.assertEqual(social.configured_apps(), social.APPS)
        self.configure()
        app = social.configured_apps()[-1]
        self.assertEqual(app.id, "notes")
        self.assertEqual(app.aliases, ("org.example.notes", "notes-window"))
        self.assertEqual(app.desktop_ids, ("org.example.Notes",))
        self.path.unlink()
        self.assertEqual(social.configured_apps(), social.APPS)

    def test_custom_app_capture_uses_exact_window_and_executable_identity(self):
        self.configure()
        shell = {"available": True, "workspaces": [{"index": 1, "name": "Work"}],
                 "windows": [dict(placement(), id=4, app_ids=["org.example.Notes"])]}
        with patch.object(social, "running_apps", return_value=set()):
            saved = social.capture_social_apps(shell)
        self.assertEqual(saved["notes"], {"running": True, "mode": "windowed", "windows": [placement()]})
        proc = self.root / "proc"
        executable = self.root / "notes-bin"
        executable.touch()
        process = proc / "1234"
        process.mkdir(parents=True)
        (process / "exe").symlink_to(executable)
        self.assertEqual(social.running_apps(proc), {"notes"})

    def test_custom_app_uses_installed_launcher_then_reuses_verified_window(self):
        self.configure()
        launcher = self.root / "data/applications/org.example.Notes.desktop"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("[Desktop Entry]\nName=Notes\n")
        saved = records()
        saved["notes"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        window = dict(placement(), id=4, app_ids=["notes-window"])
        shell = {"available": True, "active_workspace": 1,
                 "workspaces": [{"index": 1, "name": "Work"}],
                 "monitors": [{"index": 0}], "windows": []}
        clock = [0.0]
        with patch.object(social, "capture_shell", return_value=shell), \
             patch("workspace_state.desktop.remap_workspace", side_effect=lambda value, **kwargs: value), \
             patch("workspace_state.desktop.remap_monitor", side_effect=lambda value, **_kwargs: value), \
             patch.object(social, "expect_window", return_value="launch-token") as expect, \
             patch.object(social, "cancel_expected_window") as cancel, \
             patch.object(social, "move_window_result") as move, \
             patch.object(social.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(social.time, "sleep", side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(social, "launch_graphical_service", side_effect=lambda *_args: shell["windows"].append(window)) as launch:
            result = social.restore_social_apps(saved)
            self.assertEqual(result, 1)
            launch.assert_called_once_with(["/usr/bin/gtk-launch", "org.example.Notes"], "social-notes")
            expect.assert_called_once_with("org.example.notes", placement())
            cancel.assert_called_once_with("launch-token")
            self.assertEqual(social.restore_social_apps(saved), 1)
            self.assertEqual(launch.call_count, 1)
            move.assert_not_called()

    def test_legacy_recipe_stays_valid_and_new_app_without_saved_state_never_launches(self):
        self.configure()
        saved = records()
        before = copy.deepcopy(saved)
        storage.validate({"sessions": [], "social_apps": saved})
        progress = []
        with patch.object(social, "launch_graphical_service") as launch:
            self.assertEqual(social.restore_social_apps(saved, reporter=lambda *args: progress.append(args)), 0)
        launch.assert_not_called()
        self.assertEqual(saved, before)
        self.assertTrue(any("Notes: no saved state; not launching" in item[1] for item in progress))
        self.assertEqual(progress[-1][2:], (5, 5))

    def test_validation_does_not_depend_on_local_configuration(self):
        saved = records()
        saved["notes"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        self.configure("malformed = [")
        storage.validate({"sessions": [], "social_apps": saved})
        saved["../command"] = saved.pop("notes")
        with self.assertRaisesRegex(ValueError, "valid extra app identifiers"):
            social.validate_social_apps(saved)

    def test_unconfigured_saved_app_fails_without_blocking_configured_app(self):
        self.configure()
        saved = records()
        saved["notes"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        saved["calendar"] = {"running": True, "mode": "windowed", "windows": [placement()]}
        progress = []
        with patch.object(social, "capture_shell", return_value={"available": True, "windows": []}), \
             patch.object(social, "_target", side_effect=lambda value, **kwargs: value), \
             patch.object(social, "desktop_id", side_effect=lambda app: app.desktop_ids[0]) as desktop, \
             patch.object(social, "expect_window", return_value=None), \
             patch.object(social, "_restore_window", return_value=4) as restore, \
             patch.object(social, "launch_graphical_service") as launch:
            with self.assertRaisesRegex(social.ProviderRestoreError, "calendar: saved desktop app is not configured") as error:
                social.restore_social_apps(saved, reporter=lambda *args: progress.append(args))
        launch.assert_called_once_with(["/usr/bin/gtk-launch", "org.example.Notes"], "social-notes")
        self.assertEqual(desktop.call_args.args[0].id, "notes")
        self.assertEqual(restore.call_args.args[0].id, "notes")
        evidence = {item.item_id: item for item in error.exception.results}
        self.assertEqual(evidence["calendar"].identity.state, social.EvidenceState.FAILED)
        self.assertEqual(evidence["notes:1"].identity.state, social.EvidenceState.VERIFIED)
        self.assertEqual(evidence["notes:1"].placement.state, social.EvidenceState.VERIFIED)
        self.assertEqual(progress[-1][0], "failed")
        self.assertEqual(progress[-1][2:], (6, 6))
        self.assertEqual([item[2] for item in progress], sorted(item[2] for item in progress))

    def test_invalid_or_overlapping_configuration_fails_clearly(self):
        cases = {
            "malformed TOML": "apps = [",
            "unexpected root": "shell = 'command'\n",
            "missing field": CONFIG.replace('executables = ["notes-bin"]\n', ""),
            "unsafe id": CONFIG.replace('id = "notes"', 'id = "../notes"'),
            "empty label": CONFIG.replace('label = "Notes"', 'label = " "'),
            "empty aliases": CONFIG.replace('["org.example.Notes", "notes-window"]', "[]"),
            "unsafe desktop id": CONFIG.replace('desktop_ids = ["org.example.Notes"]', 'desktop_ids = ["/tmp/notes"]'),
            "desktop command": CONFIG.replace('desktop_ids = ["org.example.Notes"]', 'desktop_ids = ["notes;touch x"]'),
            "desktop suffix": CONFIG.replace('desktop_ids = ["org.example.Notes"]', 'desktop_ids = ["notes.desktop"]'),
            "unsafe executable": CONFIG.replace('["notes-bin"]', '["/tmp/notes-bin"]'),
            "duplicate aliases": CONFIG.replace('"notes-window"', '"ORG.EXAMPLE.NOTES"'),
            "builtin id": CONFIG.replace('id = "notes"', 'id = "slack"'),
            "builtin alias": CONFIG.replace('"notes-window"', '"Slack"'),
            "builtin executable": CONFIG.replace('["notes-bin"]', '["discord"]'),
            "duplicate app": CONFIG + CONFIG,
            "overlapping custom apps": CONFIG + CONFIG.replace('id = "notes"', 'id = "other-notes"'),
        }
        for case, contents in cases.items():
            with self.subTest(case=case):
                self.configure(contents)
                with self.assertRaisesRegex(CommandError, "desktop app configuration"):
                    social.configured_apps()


if __name__ == "__main__":
    unittest.main()
