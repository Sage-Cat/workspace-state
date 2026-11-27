import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import synthetic_drives
from workspace_state import vscode
from workspace_state.provider_results import EvidenceState, ProviderRestoreError
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
        synthetic_drives.install(self)
        item = project(uri="file:///home/example/Drives/drive-a/demo")
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_verify_profile"), patch.object(vscode, "launch_graphical_service") as launch, patch.object(vscode.subprocess, "run") as run:
            run.return_value.returncode = 1
            live.return_value.get.return_value = []
            with self.assertRaises(CommandError):
                vscode.restore_vscode(record(item), no_place=True)
        launch.assert_not_called()
        run.assert_called()

    def test_scoped_cold_start_checks_application_jsonc_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = root / "User" / "settings.json"
            settings.parent.mkdir()
            vscode._verify_scoped_cold_start(str(root))  # default all
            settings.write_text("""{
                // Saved by the Settings editor
                "unrelated.value": "https://example.test/a/*not-a-comment*/",
                "window.restoreWindows": "all", /* allow explicit startup */
            }""")
            vscode._verify_scoped_cold_start(str(root))
            for document in (
                '{"window.restoreWindows": "preserve"}',
                '{"window": {"restoreWindows": "preserve",},}',
            ):
                with self.subTest(document=document):
                    settings.write_text(document)
                    with self.assertRaisesRegex(CommandError, "restoreWindows is 'preserve'"):
                        vscode._verify_scoped_cold_start(str(root))
            for document in (
                '{"window.restoreWindows": "unfamiliar"}',
                '{"window.restoreWindows": ["all"]}',
                '{"window.restoreWindows": "preserve", "window.restoreWindows": "all"}',
                '{"window.restoreWindows": "all",',
                '{,}',
            ):
                with self.subTest(document=document):
                    settings.write_text(document)
                    with self.assertRaisesRegex(CommandError, "Cannot verify VS Code window.restoreWindows"):
                        vscode._verify_scoped_cold_start(str(root))
            settings.unlink()
            settings.symlink_to(settings.parent / "missing.json")
            with self.assertRaisesRegex(CommandError, "Cannot verify VS Code window.restoreWindows"):
                vscode._verify_scoped_cold_start(str(root))

    def test_preserve_setting_blocks_scoped_or_interrupted_cold_launch_before_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "User" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"window.restoreWindows": "preserve"}')
            item = {**project(user_data_dir=temporary), "_native_id": 14}
            for kwargs in ({"workspace_switch": True, "switch_token": "scoped"},
                           {"blocked_native_scopes": frozenset({temporary})}):
                with self.subTest(kwargs=kwargs), \
                     patch.object(vscode, "_wait_for_live_windows", return_value=[]), \
                     patch.object(vscode, "_check_resource"), \
                     patch.object(vscode, "launch_graphical_service") as launch:
                    with self.assertRaisesRegex(ProviderRestoreError, "scoped cold launch could reopen other workspaces"):
                        vscode.restore_vscode(record(item), no_place=True, **kwargs)
                    launch.assert_not_called()

    def test_preserve_setting_does_not_block_explicit_launch_into_a_live_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            settings = Path(temporary) / "User" / "settings.json"
            settings.parent.mkdir()
            settings.write_text('{"window.restoreWindows": "preserve"}')
            item = project(user_data_dir=temporary)
            other = {**project(uri="file:///example/other", user_data_dir=temporary),
                     "instance": "existing", "shell": {"id": 10, "workspace": 1}}
            created = {**item, "instance": "new", "endpoint": Path("/tmp/example.sock"),
                       "shell": {"id": 12, "workspace": 0}}
            with patch.object(vscode, "_wait_for_live_windows", return_value=[other]), \
                 patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_check_resource"), \
                 patch.object(vscode, "_request", side_effect=lambda _path, method, **_: {"ready": True} if method == "probe" else item), \
                 patch.object(vscode, "launch_graphical_service") as launch:
                live.return_value.get.return_value = [other, created]
                self.assertEqual(vscode.restore_vscode(record(item), no_place=True,
                                                       blocked_native_scopes=frozenset({temporary})), 1)
            launch.assert_called_once()
            self.assertIn("--new-window", launch.call_args.args[0])

    def test_interrupted_scope_uses_only_explicit_project_launch(self):
        item = project()
        live_item = {**item, "instance": "new", "endpoint": Path("/tmp/example.sock"),
                     "shell": {"id": 12, "workspace": 0}}
        with patch.object(vscode, "_wait_for_live_windows", return_value=[]), \
             patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_verify_profile"), \
             patch.object(vscode, "_request", side_effect=lambda _path, method, **_: {"ready": True} if method == "probe" else item), \
             patch.object(vscode, "_check_resource"), patch.object(vscode, "launch_graphical_service") as launch:
            live.return_value.get.return_value = [live_item]
            self.assertEqual(vscode.restore_vscode(record(item), no_place=True,
                                                   blocked_native_scopes=frozenset({item["user_data_dir"]})), 1)
        launch.assert_called_once()
        argv, purpose = launch.call_args.args
        self.assertEqual(purpose, "vscode-project")
        self.assertIn("--new-window", argv)
        self.assertIn("--folder-uri", argv)

    def test_interrupted_scope_leaves_native_only_editor_pending(self):
        item = project(kind="empty", dirty_count=1)
        with patch.object(vscode, "_wait_for_live_windows", return_value=[]), \
             patch.object(vscode, "launch_graphical_service") as launch, \
             self.assertRaisesRegex(ProviderRestoreError, "enable the parked workspace"):
            vscode.restore_vscode(record(item), no_place=True,
                                   blocked_native_scopes=frozenset({item["user_data_dir"]}))
        launch.assert_not_called()

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

    def test_volatile_project_is_only_storage_cleared_at_reboot(self):
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": "/run/user/4242"}):
            self.assertTrue(vscode.volatile_project(project(uri="file:///tmp/tool/scratch")))
            self.assertTrue(vscode.volatile_project(project(uri="file:///dev/shm/scratch")))
            self.assertTrue(vscode.volatile_project(project(uri="file:///run/user/4242/scratch")))
            self.assertTrue(vscode.volatile_project(project(kind="workspace", workspace_file="file:///tmp/a.code-workspace")))
            self.assertFalse(vscode.volatile_project(project()))
            self.assertFalse(vscode.volatile_project(project(uri="file:///var/tmp/kept")))
            self.assertFalse(vscode.volatile_project(project(uri="file:///tmpdir/kept")))
            self.assertFalse(vscode.volatile_project(project(kind="empty")))
            self.assertFalse(vscode.volatile_project(project(uri="vscode-remote://ssh-remote+host/tmp/x",
                                                             remote_name="ssh-remote")))
            mixed = project(uri="file:///tmp/tool/scratch")
            mixed["folders"].append({"uri": "file:///home/example/Projects/demo", "name": "demo"})
            self.assertFalse(vscode.volatile_project(mixed))

    def test_capture_skips_windows_on_folders_cleared_at_reboot(self):
        base = {"profile": {"id": "default", "name": "Default"}, "user_data_dir": "/home/example/.config/Code",
                "workspace_file": None, "editor_uris": [], "dirty_count": 0, "hot_exit": "onExit"}
        kept = {**base, "window_key": "kept", "folders": [{"uri": "file:///home/example/Projects/demo", "name": "demo"}]}
        scratch = {**base, "window_key": "scratch", "folders": [{"uri": "file:///tmp/tool/scratch", "name": "scratch"}]}
        natives = [{**PLACEMENT, "id": index, "app_id": "code"} for index in (1, 2)]
        with patch.object(vscode._LiveWindows, "get", return_value=[{**kept, "shell": natives[0]},
                                                                    {**scratch, "shell": natives[1]}]):
            captured = vscode.capture_vscode({"available": True, "windows": natives,
                                              "workspaces": [{"index": 0, "name": "Work"}]})
        self.assertEqual([item["window_key"] for item in captured["windows"]], ["kept"])

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


    def test_native_handoff_submits_final_target_once(self):
        from workspace_state.provider_results import PlacementPending
        shell = {"capabilities": ["staged_placement_handoff"], "windows": [{"id": 42}]}
        with patch.object(vscode, "capture_shell", return_value=shell), patch.object(
            vscode, "matching_windows", return_value=shell["windows"]), patch.object(
            vscode.time, "monotonic", return_value=0), patch.object(
            vscode, "move_window_result", return_value={
                "status": "deferred", "token": "editor-final", "handoff_owned": True}) as move:
            with self.assertRaises(PlacementPending) as caught:
                vscode._place(42, PLACEMENT, 30)
        self.assertEqual(caught.exception.request_id, "native-handoff:editor-final")
        move.assert_called_once_with(42, dict(PLACEMENT, stage_before_handoff=True), timeout=10)

    def test_remote_probe_failure_is_retryable_after_identified_placement(self):
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
        place.assert_called_once()
        self.assertEqual(error.exception.results[0].content.state, vscode.EvidenceState.FAILED)
        self.assertTrue(error.exception.results[0].content.retryable)

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


class SwitchPolicyTests(unittest.TestCase):
    def state(self, blockers, **fields):
        return {**project(), "instance": "one", "capabilities": ["workspace_switch", "switch_policy"],
                "switch_blockers": blockers, "unsupported_editors": 5, "dirty_count": 0, **fields}

    def test_policy_is_read_only_from_a_companion_that_declares_it(self):
        legacy = {"capabilities": ["workspace_switch"], "switch_blockers": [{"kind": "unsaved", "label": "a"}]}
        self.assertIsNone(vscode.switch_blockers(legacy))
        self.assertEqual(vscode.switch_blockers(self.state([])), [])
        self.assertEqual(vscode.switch_blockers(self.state([{"kind": "terminal", "label": "bash", "reason": "command running"}])),
                         [{"kind": "terminal", "label": "bash", "reason": "command running"}])
        for bad in (None, "x", [{"kind": "other", "label": "a"}], [{"kind": "unsaved"}], [1]):
            self.assertEqual(vscode.switch_blockers(self.state(bad))[0]["kind"], "unverified")

    def close_check(self, state, saved):
        with patch.object(vscode, "_request", return_value=state), patch.object(vscode, "runtime_root", return_value=Path("/x")):
            vscode.verify_switch_close({**saved, "_vscode_instance": "one"})

    def test_close_is_allowed_with_previews_and_blocked_by_the_current_policy_only(self):
        saved = project()
        self.close_check(self.state([]), saved)  # five non-text editors do not block
        for kind in ("unsaved", "untitled", "terminal"):
            with self.subTest(kind=kind), self.assertRaisesRegex(CommandError, "editors changed"):
                self.close_check(self.state([{"kind": kind, "label": "x", "reason": ""}]), saved)

    def test_close_compares_text_files_while_editor_uris_may_hold_diff_operands(self):
        saved = {**project(), "editor_uris": ["file:///example/a.md"], "_text_editor_uris": ["file:///example/a.md"]}
        wide = self.state([], editor_uris=["file:///example/a.md", "file:///example/old.md"],
                          text_editor_uris=["file:///example/a.md"])
        self.close_check(wide, saved)
        with self.assertRaisesRegex(CommandError, "editors changed"):
            self.close_check({**wide, "text_editor_uris": []}, saved)

    def test_a_companion_without_the_policy_still_blocks_on_non_text_editors(self):
        saved = project()
        legacy = {key: value for key, value in self.state([]).items() if key != "switch_blockers"}
        legacy["capabilities"] = ["workspace_switch"]
        legacy["unsupported_editors"] = 0
        self.close_check(legacy, saved)
        legacy["unsupported_editors"] = 1
        with self.assertRaisesRegex(CommandError, "editors changed"):
            self.close_check(legacy, saved)

    def test_editor_list_keeps_its_meaning_and_text_files_have_their_own_list(self):
        old = {"kind": "empty", "editor_uris": ["file:///example/a.md"]}
        self.assertEqual(vscode.saved_text_editors(old), ["file:///example/a.md"])
        new = {"editor_uris": ["file:///example/a.md", "untitled:Untitled-1"], "_text_editor_uris": ["file:///example/a.md"]}
        self.assertEqual(vscode.saved_text_editors(new), ["file:///example/a.md"])
        self.assertEqual(vscode._live_text_editors({"editor_uris": ["file:///x"], "text_editor_uris": []}), set())
        self.assertEqual(vscode._live_text_editors({"editor_uris": ["file:///x"]}), {"file:///x"})

    def test_a_reused_window_may_run_terminals_but_not_hold_unsaved_work(self):
        self.assertFalse(vscode._unsafe_to_reuse(self.state([{"kind": "terminal", "label": "bash", "reason": ""}])))
        self.assertTrue(vscode._unsafe_to_reuse(self.state([{"kind": "untitled", "label": "x", "reason": ""}])))
        legacy = {"capabilities": ["workspace_switch"], "unsupported_editors": 1, "dirty_count": 0}
        self.assertTrue(vscode._unsafe_to_reuse(legacy))


class MaximizedPlacementTests(unittest.TestCase):
    AREA = {"x": 0, "y": 44, "width": 3840, "height": 2030}
    TARGET = {"workspace": 0, "monitor": 0, "state": "maximized", "monitor_geometry": {"x": 0, "y": 0, "width": 3840, "height": 2160}}

    def window(self, state, geometry=None):
        return {"id": 5, "app_id": "code", "workspace": 0, "monitor": 0, "state": state,
                "geometry": geometry or dict(self.AREA), "monitor_work_area": dict(self.AREA),
                "monitor_geometry": self.TARGET["monitor_geometry"]}

    def run_keep(self, observed, target=None):
        frames = iter(observed)
        last = [observed[-1]]

        def shell(**_):
            window = next(frames, last[0])
            return {"windows": [window]}

        with patch.object(vscode, "capture_shell", side_effect=shell), \
             patch.object(vscode, "matching_windows", side_effect=lambda value: value["windows"]), \
             patch.object(vscode, "_place") as place, \
             patch.object(vscode, "SETTLE_SECONDS", .4), patch.object(vscode, "SETTLED_SECONDS", .05):
            try:
                vscode._keep_placement(5, target or self.TARGET, time.monotonic() + 5)
            finally:
                self.placed = place.call_count
        return place

    def test_a_window_that_stays_maximized_is_not_touched_again(self):
        self.run_keep([self.window("maximized")])
        self.assertEqual(self.placed, 0)

    def test_an_editor_that_unmaximizes_itself_after_the_first_placement_is_placed_again(self):
        # Observed drift after a verified first placement: GNOME is asked again.
        self.run_keep([self.window("normal"), self.window("maximized")])
        self.assertEqual(self.placed, 1)

    def test_a_window_that_keeps_reverting_fails_instead_of_claiming_placement(self):
        with self.assertRaisesRegex(CommandError, "changed its window state after placement"):
            self.run_keep([self.window("normal")])
        self.assertEqual(self.placed, 2)

    def test_normal_targets_are_not_observed(self):
        with patch.object(vscode, "capture_shell") as shell:
            vscode._keep_placement(5, {**self.TARGET, "state": "normal"}, time.monotonic() + 5)
        shell.assert_not_called()

    def test_restore_reports_a_placement_that_the_editor_reverts_after_content_verification(self):
        item = project()
        maximized = {**PLACEMENT, "state": "maximized"}
        live_item = {**item, "instance": "one", "endpoint": Path("/tmp/x.sock"),
                     "shell": {"id": 7, "workspace": 0, "monitor": 0, "state": "maximized", "geometry": PLACEMENT["geometry"]}}
        request = lambda endpoint, method, *args, **kwargs: {"ready": True} if method == "probe" else item
        for failure, state in ((CommandError("reverted"), EvidenceState.FAILED), (None, EvidenceState.VERIFIED)):
            with self.subTest(failure=failure), patch.object(vscode, "_LiveWindows") as live, \
                 patch.object(vscode, "_target", return_value=maximized), patch.object(vscode, "_place"), \
                 patch.object(vscode, "_keep_placements", side_effect=lambda requests, deadline: {0: failure}) as keep, \
                 patch.object(vscode, "_request", side_effect=request):
                live.return_value.get.return_value = [live_item]
                if failure:
                    with self.assertRaises(ProviderRestoreError) as caught:
                        vscode.restore_vscode(record(item))
                    self.assertEqual(caught.exception.results[0].placement.state, state)
                else:
                    self.assertEqual(vscode.restore_vscode(record(item)), 1)
                keep.assert_called_once()

    def keep(self, windows, deadline_in=5.0, matches=lambda window: True, shell_effect=None):
        calls = []

        def shell(**_):
            calls.append(1)
            if shell_effect:
                shell_effect(len(calls))
            return {"windows": list(windows.values())}

        with patch.object(vscode, "capture_shell", side_effect=shell), \
             patch.object(vscode, "matching_windows", side_effect=lambda value: value["windows"]), \
             patch.object(vscode, "placement_frame_matches", side_effect=lambda window, target: matches(window)), \
             patch.object(vscode, "_place") as place, \
             patch.object(vscode, "SETTLE_SECONDS", .4), patch.object(vscode, "SETTLED_SECONDS", .05):
            result = vscode._keep_placements({n: (n, self.TARGET, {}) for n in windows},
                                             time.monotonic() + deadline_in)
        return result, calls, place

    def test_many_maximized_windows_are_observed_in_one_pass(self):
        one = {1: self.window("maximized") | {"id": 1}}
        many = {n: self.window("maximized") | {"id": n} for n in (1, 2, 3, 4)}
        _, single_calls, _ = self.keep(one)
        result, many_calls, place = self.keep(many)
        self.assertEqual(result, {n: None for n in many})
        place.assert_not_called()
        self.assertEqual(len(many_calls), len(single_calls))  # observation does not grow with the window count

    def test_a_spent_restore_deadline_does_not_fail_stable_windows(self):
        windows = {n: self.window("maximized") | {"id": n} for n in (1, 2)}
        for ahead in (0.0, -3.0, 0.1):
            result, calls, _ = self.keep(windows, deadline_in=ahead)
            self.assertEqual(result, {1: None, 2: None}, ahead)

    def test_only_a_window_that_never_settles_fails_after_the_deadline(self):
        windows = {1: self.window("maximized") | {"id": 1}, 2: self.window("normal") | {"id": 2}}
        result, calls, place = self.keep(windows, deadline_in=-1.0, matches=lambda w: w["state"] == "maximized")
        self.assertIsNone(result[1])
        self.assertRegex(str(result[2]), "changed its window state")
        self.assertEqual(place.call_count, 2)  # re-placed, and observed again, before giving up

    def test_a_failed_observation_keeps_windows_that_already_matched(self):
        windows = {n: self.window("maximized") | {"id": n} for n in (1, 2)}

        def effect(count):
            if count > 1:
                raise CommandError("GNOME is not answering")

        with patch.object(vscode, "SETTLED_SECONDS", 10):
            result, calls, _ = self.keep(windows, shell_effect=effect)
        self.assertEqual(result, {1: None, 2: None})  # matched once, then the shell stopped answering

    def test_a_failed_first_observation_fails_unverified_windows_only(self):
        windows = {1: self.window("maximized") | {"id": 1}}

        def effect(count):
            raise CommandError("GNOME is not answering")

        result, _, _ = self.keep(windows, shell_effect=effect)
        self.assertRegex(str(result[1]), "not answering")


class EmptyWindowTests(unittest.TestCase):
    def empty(self, editor_uris, text, dirty=0):
        return {"kind": "empty", "folders": [], "workspace_file": None, "placement": {"workspace_name": "Alpha"},
                "editor_uris": editor_uris, "_text_editor_uris": text, "dirty_count": dirty, "user_data_dir": "/example/code",
                "profile": {"id": "default", "name": "Default"}, "remote_name": None, "_supports_workspace_switch": True,
                "_switch_blockers": [], "_unsupported_editors": None}

    def test_non_text_tabs_do_not_block_off_or_on_for_a_window_without_a_folder(self):
        from workspace_state import workspace_switch as switch
        relaunched = self.empty([], [])
        for name, uris in (("untitled", ["untitled:Untitled-1"]),
                           ("diff", ["file:///example/old.md", "file:///example/new.md"]),
                           ("preview", ["file:///example/image.png"])):
            item = self.empty(uris, [])
            with self.subTest(name):
                self.assertEqual(switch._vscode_problems(item), [])
                vscode._command(item)  # On launches an empty window instead of refusing
                self.assertEqual(vscode._identity(item), vscode._identity(relaunched))

    def test_text_files_and_unsaved_state_still_guard_an_empty_window(self):
        from workspace_state import workspace_switch as switch
        files = self.empty(["file:///example/a.md"], ["file:///example/a.md"])
        self.assertTrue(switch._vscode_problems(files))
        with self.assertRaisesRegex(CommandError, "refusing to replace unsaved state"):
            vscode._command(files)
        with self.assertRaisesRegex(CommandError, "refusing to replace unsaved state"):
            vscode._command(self.empty([], [], dirty=1))
        self.assertNotEqual(vscode._identity(files), vscode._identity(self.empty([], [])))

    def login(self, saved, live_uris):
        """Restore one saved empty window at login against a current companion that reports `live_uris`."""
        state = {**self.empty(live_uris, []), "instance": "one", "text_editor_uris": []}
        state = {key: state[key] for key in ("profile", "user_data_dir", "workspace_file", "folders", "editor_uris",
                                              "text_editor_uris", "dirty_count", "remote_name", "window_key", "hot_exit")
                 if key in state}
        state.setdefault("folders", [])
        live_item = {**vscode._project(state), "instance": "one", "endpoint": Path("/tmp/x.sock"),
                     "text_editor_uris": [], "shell": {"id": 7, "workspace": 0, "monitor": 0, "state": "normal",
                                                       "geometry": PLACEMENT["geometry"]}}
        request = lambda endpoint, method, *args, **kwargs: {"ready": True} if method == "probe" else state
        with patch.object(vscode, "_LiveWindows") as live, patch.object(vscode, "_target", return_value=PLACEMENT), \
             patch.object(vscode, "_place"), patch.object(vscode, "_request", side_effect=request), \
             patch.object(vscode, "launch_graphical_service") as launch:
            live.return_value.get.return_value = [live_item]
            count = vscode.restore_vscode(record({**saved, "placement": dict(PLACEMENT)}))
        launch.assert_not_called()
        return count

    def test_a_checkpoint_saved_before_the_text_list_matches_the_live_empty_window(self):
        for name, uris in (("untitled", ["untitled:Untitled-1"]),
                           ("diff", ["file:///example/old.md", "file:///example/new.md"]),
                           ("preview", ["file:///example/image.png"])):
            old = self.empty(uris, [])
            del old["_text_editor_uris"]  # what a checkpoint from the previous release holds
            with self.subTest(name):
                self.assertEqual(self.login(old, uris), 1)
                self.assertEqual(vscode._identity(old, wide=True), vscode._identity(vscode._project(
                    {**old, "text_editor_uris": []}), wide=True))

    def test_login_matches_the_wide_editor_list_and_a_switch_the_text_files(self):
        saved = self.empty(["untitled:Untitled-1"], [])
        # Login: a window that shows other editors is not this record, even though no text file differs.
        with self.assertRaises(CommandError):
            self.login(saved, ["untitled:Untitled-1", "file:///example/extra.png"])
        self.assertEqual(self.login(saved, ["untitled:Untitled-1"]), 1)
        # A workspace switch compares text files only.
        wide = self.empty(["untitled:Untitled-1", "file:///example/extra.png"], [])
        self.assertEqual(vscode._identity(saved), vscode._identity(wide))
        self.assertNotEqual(vscode._identity(saved, wide=True), vscode._identity(wide, wide=True))

    def test_two_folderless_windows_keep_their_own_records_in_either_discovery_order(self):
        base = {"profile": {"id": "default", "name": "Default"}, "user_data_dir": "/example/code",
                "workspace_file": None, "dirty_count": 0, "hot_exit": "onExit", "folders": [], "remote_name": None}
        states = {"a": {**base, "window_key": "a", "editor_uris": ["file:///example/x.md"], "text_editor_uris": []},
                  "b": {**base, "window_key": "b", "editor_uris": ["file:///example/y.png"], "text_editor_uris": []}}
        saved = [{**vscode._project(states[key]), "window_key": key, "placement": dict(PLACEMENT)} for key in "ab"]
        for order in ("ab", "ba"):
            live = [{**vscode._project(states[key]), "instance": key, "endpoint": Path(f"/tmp/{key}.sock"),
                     "text_editor_uris": [], "shell": {"id": 10 + n, "workspace": 0, "monitor": 0, "state": "normal",
                                                       "geometry": PLACEMENT["geometry"]}}
                    for n, key in enumerate(order)]
            request = lambda endpoint, method, *args, **kwargs: (
                {"ready": True} if method == "probe" else states[Path(endpoint).stem])
            with self.subTest(order=order), patch.object(vscode, "_LiveWindows") as lw, \
                 patch.object(vscode, "_target", return_value=PLACEMENT), patch.object(vscode, "_place"), \
                 patch.object(vscode, "_request", side_effect=request), \
                 patch.object(vscode, "launch_graphical_service") as launch:
                lw.return_value.get.return_value = live
                self.assertEqual(vscode.restore_vscode(record(*saved), timeout=2), 2)
                launch.assert_not_called()

    def test_login_does_not_launch_a_window_for_a_record_that_native_recovery_did_not_bring_back(self):
        base = {"profile": {"id": "default", "name": "Default"}, "user_data_dir": "/example/code",
                "workspace_file": None, "dirty_count": 0, "hot_exit": "onExit", "remote_name": None}
        states = {"diff": {**base, "window_key": "diff", "folders": [], "editor_uris": ["file:///example/x.md"], "text_editor_uris": []},
                  "proj": {**base, "window_key": "proj", "folders": [{"uri": "file:///example/proj", "name": "proj"}],
                           "editor_uris": [], "text_editor_uris": []}}
        saved = [{**vscode._project(states[key]), "window_key": key, "placement": dict(PLACEMENT)} for key in ("diff", "proj")]
        live = {**vscode._project(states["proj"]), "instance": "proj", "endpoint": Path("/tmp/proj.sock"),
                "text_editor_uris": [], "shell": {"id": 10, "workspace": 0, "monitor": 0, "state": "normal",
                                                  "geometry": PLACEMENT["geometry"]}}
        request = lambda endpoint, method, *a, **k: {"ready": True} if method == "probe" else states[Path(endpoint).stem]
        with patch.object(vscode, "_LiveWindows") as lw, patch.object(vscode, "_target", return_value=PLACEMENT), \
             patch.object(vscode, "_place"), patch.object(vscode, "_request", side_effect=request), \
             patch.object(vscode, "_verify_profile"), patch.object(vscode, "_check_resource"), \
             patch.object(vscode, "launch_graphical_service") as launch:
            lw.return_value.get.return_value = [live]
            with self.assertRaises(ProviderRestoreError) as caught:
                vscode.restore_vscode(record(*saved), timeout=2)
        launch.assert_not_called()
        states_by_window = [result.identity.state for result in caught.exception.results]
        self.assertEqual(states_by_window[1], EvidenceState.VERIFIED)  # the folder window still verifies
        self.assertNotEqual(states_by_window[0], EvidenceState.VERIFIED)

    def test_a_switch_may_launch_a_folderless_window_that_shows_only_non_text_editors(self):
        item = self.empty(["file:///example/x.md"], [])
        vscode._command(item)  # On: text files decide
        with self.assertRaisesRegex(CommandError, "refusing to replace unsaved state"):
            vscode._command(item, wide=True)  # login: the wide list decides

    def test_the_text_editor_list_is_validated(self):
        item = self.empty([], [])
        vscode._validate_project(item)
        for bad in ("file:///x", [1], ["untitled:Untitled-1"], ["file:///x"] * 600):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                vscode._validate_project({**item, "_text_editor_uris": bad})

    def test_a_record_from_before_the_text_list_keeps_its_pending_key(self):
        from workspace_state import pending
        new = {**vscode._project({"profile": {"id": "default", "name": "Default"}, "user_data_dir": "/example/code",
                                  "workspace_file": None, "folders": [], "dirty_count": 0,
                                  "editor_uris": ["untitled:Untitled-1"], "text_editor_uris": []})}
        old = {key: value for key, value in new.items() if key != "_text_editor_uris"}
        self.assertEqual(pending.vscode_key(new), pending.vscode_key(old))

    def test_live_state_carries_the_text_list_into_the_identity(self):
        state = {"profile": {"id": "default", "name": "Default"}, "user_data_dir": "/example/code", "folders": [],
                 "workspace_file": None, "editor_uris": ["untitled:Untitled-1"], "text_editor_uris": [], "dirty_count": 0}
        self.assertEqual(vscode._identity(vscode._project(state)), vscode._identity(self.empty([], [])))


if __name__ == "__main__":
    unittest.main()
