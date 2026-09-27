import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from workspace_state import vscode
from workspace_state.util import CommandError


IDENTITY = {"connector": "DP-1", "edid_hash": "display"}
PLACEMENT = {
    "workspace": 0, "workspace_name": "Work", "monitor": 0,
    "monitor_intent": IDENTITY, "monitor_identity": IDENTITY,
    "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
    "geometry": {"x": 10, "y": 20, "width": 900, "height": 700},
    "geometry_relative": {"x": 10, "y": 20, "width": 900, "height": 700},
    "state": "normal",
}


def project(kind="folder", uri="file:///home/example/Projects/demo", *, instance="one",
            profile_id="default", profile_name="Default", user_data_dir="/tmp/code-data",
            dirty_count=0, hot_exit="on", workspace_file=None, remote_name=None):
    folders = [] if kind in {"empty", "workspace", "untitled"} else [{"uri": uri, "name": Path(uri).name or "demo"}]
    return {
        "kind": kind, "workspace_file": workspace_file, "folders": folders,
        "editor_uris": [], "dirty_count": dirty_count,
        "profile": {"id": profile_id, "name": profile_name},
        "user_data_dir": user_data_dir, "remote_name": remote_name,
        "window_key": instance, "hot_exit": hot_exit,
        "placement": dict(PLACEMENT),
    }


def record(*items):
    return {"provider": "vscode", "version": 1, "windows": list(items)}


class VscodeTests(unittest.TestCase):
    def test_dry_run_has_no_launch_or_placement_side_effects(self):
        item = project()
        with patch.object(vscode, "launch_graphical_service") as launch, patch.object(vscode, "move_window_result") as move:
            self.assertEqual(vscode.restore_vscode(record(item), dry_run=True), 1)
        launch.assert_not_called()
        move.assert_not_called()

    def test_empty_checkpoint_does_not_launch(self):
        with patch.object(vscode, "launch_graphical_service") as launch:
            self.assertEqual(vscode.restore_vscode(record(), timeout=.01), 0)
        launch.assert_not_called()

    def test_validation_rejects_control_credentials_and_dangerous_uri(self):
        for uri in ("file:///tmp/x?query", "file://user:password@host/x", "file:///tmp/x\n"):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    vscode.validate_vscode(record(project(uri=uri)))
        with self.assertRaises(ValueError):
            vscode.validate_vscode(record(project(uri="file://relative/x")))

    def test_storage_gate_precedes_launch(self):
        item = project(uri="file:///home/sagecat/Drives/gdrive/demo")
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_verify_profile"), patch.object(vscode, "launch_graphical_service") as launch, patch.object(vscode.subprocess, "run") as run:
            run.return_value.returncode = 1
            live.return_value.get.return_value = []
            with self.assertRaises(CommandError):
                vscode.restore_vscode(record(item), no_place=True)
        launch.assert_not_called()
        run.assert_called()

    def test_same_basename_folders_match_by_uri(self):
        a = project(uri="file:///home/example/a/demo")
        b = project(uri="file:///home/example/b/demo")
        self.assertNotEqual(vscode._identity(a), vscode._identity(b))

    def test_same_project_multiple_instances_are_claimed_once(self):
        a = project(instance="one")
        b = project(instance="two")
        live_window = lambda instance: {**project(instance=instance), "instance": instance, "shell": {"id": 1 if instance == "one" else 2, "workspace": 0, "monitor": 0, "state": "normal", "geometry": PLACEMENT["geometry"]}, "endpoint": Path("/tmp/x.sock")}
        def request(endpoint, method, *args, **kwargs):
            return {"ready": True} if method == "probe" else {"profile": a["profile"], "user_data_dir": a["user_data_dir"], "workspace_file": None, "folders": a["folders"], "editor_uris": [], "dirty_count": 0}
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_place"), patch.object(vscode, "_request", side_effect=request):
            live.return_value.get.return_value = [live_window("one"), live_window("two")]
            self.assertEqual(vscode.restore_vscode(record(a, b)), 2)
        self.assertEqual(live.return_value.get.call_count, 1)

    def test_already_running_windows_are_not_duplicated(self):
        item = project()
        live_item = {**item, "instance": "one", "endpoint": Path("/tmp/x.sock"), "shell": {"id": 7, "workspace": 0, "monitor": 0, "state": "normal", "geometry": PLACEMENT["geometry"]}}
        def request(endpoint, method, *args, **kwargs):
            return {"ready": True} if method == "probe" else item
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_place"), patch.object(vscode, "_request", side_effect=request):
            live.return_value.get.return_value = [live_item]
            with patch.object(vscode, "launch_graphical_service") as launch:
                self.assertEqual(vscode.restore_vscode(record(item)), 1)
        launch.assert_not_called()

    def test_missing_companion_fails_before_launch(self):
        item = project()
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "launch_graphical_service") as launch:
            live.return_value.get.side_effect = CommandError("companion missing")
            with self.assertRaises(CommandError):
                vscode.restore_vscode(record(item))
        launch.assert_not_called()

    def test_capture_incomplete_refuses_wrong_success(self):
        shell = {"available": True, "windows": [{"id": 1, "app_id": "code"}], "workspaces": [{"index": 0, "name": "Work"}]}
        with patch.object(vscode._LiveWindows, "get", return_value=[]):
            with self.assertRaises(CommandError):
                vscode.capture_vscode(shell)

    def test_dirty_editor_with_hot_exit_off_fails(self):
        state = {"instance": "one", "profile": {"id": "default", "name": "Default"}, "user_data_dir": "/tmp/code", "folders": [{"uri": "file:///tmp/demo", "name": "demo"}], "workspace_file": None, "editor_uris": [], "dirty_count": 1, "hot_exit": "off"}
        native = {"id": 1, "app_id": "code", "workspace": 0, "monitor": 0, "state": "normal", "geometry": PLACEMENT["geometry"]}
        with patch.object(vscode._LiveWindows, "get", return_value=[{**state, "shell": native}]):
            with self.assertRaises(CommandError):
                vscode.capture_vscode({"available": True, "windows": [native], "workspaces": [{"index": 0, "name": "Work"}]})

    def test_identification_releases_in_finally(self):
        state = {"instance": "one", "endpoint": Path("/tmp/one.sock")}
        with patch.object(vscode, "_request", side_effect=[{"token": "bad"}, CommandError("release failed")]) as request, patch.object(vscode, "capture_shell", return_value={"windows": [], "available": True}):
            with self.assertRaises(CommandError):
                vscode._LiveWindows()._identify(state, time.monotonic() + 1)
        self.assertEqual(request.call_args_list[-1].args[:2], (state["endpoint"], "release"))

    def test_timeout_reports_failure_but_processes_unrelated_windows(self):
        items = [project(uri="file:///tmp/a"), project(uri="file:///tmp/b")]
        states = {"/tmp/a.sock": items[0], "/tmp/b.sock": items[1]}
        def request(endpoint, method, *args, **kwargs):
            return {"ready": True} if method == "probe" else states[str(endpoint)]
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_place", side_effect=[CommandError("timeout"), None]), patch.object(vscode, "_request", side_effect=request), patch.object(vscode, "_verify_profile"):
            live.return_value.get.return_value = [{**items[0], "instance": "a", "endpoint": Path("/tmp/a.sock"), "shell": {"id": 1}}, {**items[1], "instance": "b", "endpoint": Path("/tmp/b.sock"), "shell": {"id": 2}}]
            with self.assertRaises(CommandError) as ctx:
                vscode.restore_vscode(record(*items), timeout=1)
        self.assertIn("window 1", str(ctx.exception))
        self.assertNotIn("window 2", str(ctx.exception))

    def test_remote_probe_failure_prevents_placement(self):
        item = project(uri="vscode-remote://ssh-remote+host/home/example/demo", remote_name="ssh-remote")
        live_item = {**item, "instance": "one", "endpoint": Path("/tmp/remote.sock"), "shell": {"id": 7, "workspace": 0, "monitor": 0, "state": "normal", "geometry": PLACEMENT["geometry"]}}
        def request(endpoint, method, *args, **kwargs):
            if method == "probe":
                return {"ready": False}
            return item
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_place") as place, patch.object(vscode, "_request", side_effect=request):
            live.return_value.get.return_value = [live_item]
            with self.assertRaises(CommandError) as error:
                vscode.restore_vscode(record(item), timeout=1)
        self.assertIn("remote", str(error.exception).lower())
        place.assert_not_called()

    def test_empty_untitled_recovery_does_not_replace_blank_window(self):
        item = project(kind="empty")
        item["editor_uris"] = ["untitled:///recovered-buffer"]
        blank = {**project(kind="empty"), "instance": "blank", "endpoint": Path("/tmp/blank.sock"), "shell": {"id": 9}}
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_verify_profile"), patch.object(vscode, "launch_graphical_service") as launch:
            live.return_value.get.return_value = [blank]
            with self.assertRaises(CommandError):
                vscode.restore_vscode(record(item), timeout=.1)
        launch.assert_not_called()

    def test_profile_identity_is_part_of_project_identity(self):
        self.assertNotEqual(vscode._identity(project(profile_id="one")), vscode._identity(project(profile_id="two")))

    def test_remote_identity_is_part_of_project_identity(self):
        self.assertNotEqual(vscode._identity(project(remote_name="ssh-1")), vscode._identity(project(remote_name="ssh-2")))

    def test_missing_named_profile_is_rejected_without_creating_it(self):
        with tempfile.TemporaryDirectory() as temp:
            item = project(profile_id="work", profile_name="Work", user_data_dir=temp)
            with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_profile_registry", return_value=[{"id": "default", "name": "Default"}]), patch.object(vscode, "_check_resource"), patch.object(vscode, "launch_graphical_service") as launch:
                live.return_value.get.return_value = []
                with self.assertRaises(CommandError) as error:
                    vscode.restore_vscode(record(item), no_place=True)
        self.assertIn("profile", str(error.exception).lower())
        launch.assert_not_called()

    def test_private_endpoint_permissions_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "vscode"
            root.mkdir(mode=0o700)
            endpoint = root / ("a" * 32 + ".sock")
            endpoint.touch(mode=0o600)
            with patch.object(vscode, "runtime_root", return_value=root), patch.object(vscode, "_request", wraps=vscode._request):
                with self.assertRaises(CommandError):
                    vscode._request(endpoint, "state")


if __name__ == "__main__":
    unittest.main()
