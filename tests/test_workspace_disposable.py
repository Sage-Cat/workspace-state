"""Switching a workspace off closes what it cannot archive and removes it from GNOME."""
from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import MagicMock, patch

from workspace_state import cli, desktop, file_manager, login_finalize, operations, social_apps, storage
from workspace_state import workspace_switch as switch
from workspace_state.util import CommandError

ORDER = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India"]
CAPABILITIES = ["close_window", "close_x11_window", "close_transient", "edit_workspaces"]
UID = os.getuid()
SCOPES = f"/user.slice/user-{UID}.slice/user@{UID}.service/app.slice"
REAL_REMOVE_WORKSPACE = desktop.remove_workspace
PENDING = "window placements are still in progress; retry in a moment"
STEAM = social_apps.App("steam", "Steam", ("steam",), ("steam",), ("steam",))


def window(wid, workspace, *, app="nemo", pid=None, client="wayland", blocker=None, **extra):
    return {"id": wid, "pid": wid * 100 if pid is None else pid, "workspace": workspace, "sticky": False, "close_blocker": blocker,
            "client_type": client, "monitor": 0, "state": "normal", "title": f"{app} {wid}",
            "app_id": app, "wm_class": app, **extra}


def x11(wid, workspace, *, app="steam", pid=None, blocker="unsupported_client", **extra):
    return window(wid, workspace, app=app, pid=pid, client="x11", blocker=blocker, **extra)


def recipe(names, *windows):
    return {"version": 7, "name": "current", "created_at": "fictional",
            "desktop": {"workspace_names": list(names)}, "sessions": [], "terminals": [],
            "file_manager": {"provider": "nemo", "windows": [
                {"_native_id": wid, "locations": [f"file:///example/{wid}"], "active_tab": 0,
                 "placement": {"workspace": index, "workspace_name": names[index], "monitor": 0, "state": "normal"}}
                for wid, index in windows]}}


class DisposableBase(unittest.TestCase):
    capabilities = CAPABILITIES

    def setUp(self):
        self.addCleanup(operations.bind, operations.current())
        operations.bind(None)
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"XDG_DATA_HOME": temp, "XDG_RUNTIME_DIR": temp}))
        self.enterContext(patch.object(cli, "_graphical_session_ending", return_value=None))
        self.enterContext(patch.object(file_manager, "verify_switch_close"))
        self.enterContext(patch.object(cli, "configured_apps", return_value=(*social_apps.APPS, *social_apps.DEFAULT_APPS, STEAM)))
        self.enterContext(patch.object(switch.time, "sleep"))
        self.out = self.enterContext(redirect_stdout(io.StringIO()))
        self.err = self.enterContext(redirect_stderr(io.StringIO()))
        self.names = list(ORDER)
        self.windows = []
        self.cgroups = {}
        self.scopes = {}
        self.blocking = {}
        self.other = None  # new gnome-winctl: windows it lists apart from the normal ones
        self.blockers = {}
        self.terminated = []
        self.capture = self.enterContext(patch.object(desktop, "capture_shell", side_effect=lambda **_k: self.shell()))
        self.close = self.enterContext(patch.object(desktop, "close_window", side_effect=self.closed))
        self.remove = self.enterContext(patch.object(desktop, "remove_workspace", side_effect=self.removed))
        self.insert = self.enterContext(patch.object(desktop, "insert_workspace", side_effect=self.inserted))
        self.enterContext(patch.object(switch, "_cgroup_path", side_effect=lambda pid: self.cgroups.get(pid)))
        self.enterContext(patch.object(switch, "_scope_processes", side_effect=lambda path: list(self.scopes.get(path, ()))))
        self.enterContext(patch.object(switch, "_process_name", return_value="steam"))
        original = switch._terminate_unit
        self.enterContext(patch.object(switch, "_terminate_unit", side_effect=self.terminate, __wrapped__=original))

    def shell(self):
        # gnome-winctl names an unnamed workspace "Workspace N" and says named: false.
        workspaces = [{"index": index, "name": name or f"Workspace {index + 1}", "named": bool(name),
                       "blocking_windows": self.blocking.get(index, 0) + len(self.blockers.get(index, ())),
                       **({"blockers": list(self.blockers.get(index, ()))} if self.other is not None else {})}
                      for index, name in enumerate(self.names)]
        extra = {"other_windows": copy.deepcopy(self.other)} if self.other is not None else {}
        return {"available": True, "enable_epoch": "epoch", "capabilities": list(self.capabilities),
                "dynamic_workspaces": False, "screen_unavailable": False, "monitors": [],
                "workspaces": workspaces, "windows": copy.deepcopy(self.windows), **extra}

    def closed(self, wid, **_kwargs):
        self.windows = [item for item in self.windows if item["id"] != wid]

    def removed(self, index, name, epoch):
        assert self.names[index] == name and epoch == "epoch"
        assert not any(item["workspace"] == index for item in self.windows)
        del self.names[index]
        for item in self.windows:
            item["workspace"] -= item["workspace"] > index
        return [{"index": i, "name": n} for i, n in enumerate(self.names)]

    def inserted(self, index, name, epoch):
        assert epoch == "epoch"
        self.names.insert(index, name)
        for item in self.windows:
            item["workspace"] += item["workspace"] >= index
        return [{"index": i, "name": n} for i, n in enumerate(self.names)]

    def terminate(self, unit):
        self.terminated.append(unit)
        for path in [path for path in self.scopes if path.endswith("/" + unit)]:
            self.scopes[path] = []

    def apply(self, action, name):
        return cli.main(["workspace", action, "--", name])

    def listed(self):
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(cli.main(["workspace", "list", "--json"]), 0)
        return json.loads(self.out.getvalue())["workspaces"]

    def archive(self, name):
        return json.loads(switch._path(name).read_text())

    def off_with(self, name, snapshot=None, *, expect=0):
        index = self.names.index(name)
        patcher = patch.object(switch, "_snapshot", side_effect=lambda *_a, **_k: snapshot or recipe(
            self.names, *[(item["id"], index) for item in self.windows if item["workspace"] == index
                          and item["app_id"] == "nemo"]))
        with patcher:
            self.assertEqual(self.apply("off", name), expect, self.err.getvalue())


class ClassificationTests(DisposableBase):
    def test_known_window_is_archived_and_closed_without_the_x11_flag(self):
        self.windows = [window(14, 2)]
        self.off_with("Charlie")
        self.close.assert_called_once_with(14, workspace=2, name="Charlie", pid=1400, epoch="epoch")
        self.assertEqual(switch._native_ids(self.archive("Charlie")["snapshot"]), [14])
        self.assertEqual(self.archive("Charlie")["unknown"], [])

    def test_unknown_wayland_window_is_closed_and_never_archived(self):
        self.windows = [window(14, 2), window(15, 2, app="org.example.editor")]
        self.off_with("Charlie")
        self.assertEqual([call.args[0] for call in self.close.call_args_list], [15, 14])
        self.assertNotIn("allow_x11", self.close.call_args_list[0].kwargs)
        entry = self.archive("Charlie")
        self.assertEqual(switch._native_ids(entry["snapshot"]), [14])
        self.assertEqual(entry["originals"].keys(), {"14"})
        self.assertEqual([(item["id"], item["app"]) for item in entry["unknown"]], [(15, "org.example.editor")])

    def test_configured_x11_app_is_unknown_and_closed_with_the_x11_flag(self):
        self.windows = [x11(31, 8, app="steam")]
        self.off_with("India")
        self.close.assert_called_once_with(31, workspace=8, name="India", pid=3100, epoch="epoch", allow_x11=True)
        self.assertEqual(switch._native_ids(self.archive("India")["snapshot"]), [])
        self.assertEqual(self.archive("India")["phase"], "off")

    def transient_pair(self, **dialog):
        # GNOME calls both the dialog and the window it belongs to transient.
        self.windows = [x11(31, 8, pid=3100, blocker="transient", transient_for=None),
                        x11(32, 8, pid=3100, blocker="transient", **{"transient_for": 31, **dialog})]
        path = f"{SCOPES}/app-gnome-steam-3100.scope"
        self.cgroups = {3100: path}
        self.scopes = {path: [3100, 3101]}

    def test_a_family_of_an_unknown_window_is_closed_by_one_request_to_its_root(self):
        self.transient_pair()
        self.close.side_effect = lambda wid, **_k: setattr(self, "windows", [])
        self.off_with("India")
        self.close.assert_called_once_with(31, workspace=8, name="India", pid=3100, epoch="epoch",
                                           allow_x11=True, allow_transient=True)
        self.assertEqual(self.archive("India")["terminated"], ["app-gnome-steam-3100.scope"])

    def test_a_transient_without_a_parent_in_the_family_is_a_root(self):
        self.windows = [x11(32, 8, pid=3100, blocker="transient", transient_for=99)]
        self.close.side_effect = lambda wid, **_k: setattr(self, "windows", [])
        self.off_with("India")
        self.assertEqual([call.args[0] for call in self.close.call_args_list], [32])

    def test_a_family_with_a_known_member_is_never_closed(self):
        self.transient_pair()
        self.windows[1] = window(32, 8, app="nemo", pid=1500, blocker="transient", transient_for=31)
        self.off_with("India", expect=2)
        self.assertIn("is a dialog of another window", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("India").exists())

    def test_without_the_transient_capability_the_family_is_refused_with_its_window_named(self):
        self.capabilities = ["close_window", "close_x11_window", "edit_workspaces"]
        self.transient_pair()
        self.close.side_effect = CommandError("GNOME refused to close the exact workspace window (transient)")
        self.off_with("India", expect=3)
        self.assertNotIn("allow_transient", self.close.call_args_list[0].kwargs)
        self.assertIn('India: unrecognized window "steam 31"', self.err.getvalue())
        self.assertEqual(self.terminated, [])

    def test_a_refusal_of_a_member_gives_off_a_detail_and_no_sigterm(self):
        self.transient_pair()
        self.close.side_effect = CommandError("GNOME refused to close the exact workspace window (unsupported_window)")
        self.off_with("India", expect=3)
        self.assertIn("unsupported_window", self.err.getvalue())
        self.assertIn('"steam 31"', self.err.getvalue())
        self.assertEqual(self.terminated, [])

    def test_a_family_that_stays_open_gets_no_sigterm(self):
        self.transient_pair()
        self.close.side_effect = lambda wid, **_k: None
        with patch.object(switch, "UNKNOWN_CLOSE_WAIT_SECONDS", 0):
            self.off_with("India", expect=3)
        self.assertEqual(self.terminated, [])
        self.assertIn("did not close; it may be asking to save", self.err.getvalue())

    def test_without_parent_ids_a_dialog_is_still_told_by_its_process(self):
        self.windows = [x11(31, 8, pid=3100, blocker="transient"), x11(32, 8, pid=3100, blocker="transient")]
        self.close.side_effect = lambda wid, **_k: setattr(self, "windows", [])
        self.off_with("India")
        self.assertTrue(self.close.called)

    def test_an_x11_client_with_a_window_on_another_workspace_blocks_before_any_change(self):
        self.windows = [x11(31, 8, pid=3100), x11(32, 1, pid=3100)]
        self.off_with("India", expect=2)
        self.assertIn("another workspace", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("India").exists())

    def test_a_window_only_a_new_gnome_winctl_leaves_as_client_type_none_is_still_trilium(self):
        live = {"id": 61, "pid": 6100, "workspace": 3, "sticky": False, "close_blocker": None, "client_type": None,
                "app_id": "", "wm_class": "trilium", "monitor": 0, "state": "normal", "title": "Notes"}
        self.windows = [live]
        shell = self.shell()
        known, unknown, problems = switch._classify(cli, shell, shell["workspaces"][3])
        self.assertEqual((sorted(known), unknown, problems), ([61], {}, []))

    def test_dialog_of_a_known_window_blocks(self):
        self.windows = [window(14, 2, pid=1400), window(15, 2, app="org.example.dialog", pid=1400, blocker="transient")]
        self.off_with("Charlie", expect=2)
        self.assertIn("is a dialog of another window", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("Charlie").exists())

    def test_known_window_that_cannot_be_captured_blocks_before_anything_closes(self):
        self.windows = [window(14, 2), window(15, 2, app="org.example.editor")]
        with patch.object(cli, "_capture_all", return_value=recipe(self.names)):
            self.assertEqual(self.apply("off", "Charlie"), 2)
        self.assertIn("has no provider that can archive it", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("Charlie").exists())

    def test_other_blockers_still_block_an_unknown_window(self):
        for blocker in ("not_closable", "unsupported_window", "invalid_pid"):
            with self.subTest(blocker=blocker):
                self.windows = [window(15, 2, app="org.example.editor", blocker=blocker)]
                self.off_with("Charlie", expect=2)
                self.close.assert_not_called()
        self.windows = [window(15, 2, app="org.example.editor", pid=0)]
        self.off_with("Charlie", expect=2)
        self.assertIn("no valid process ID", self.err.getvalue())

    def test_built_in_provider_never_closes_an_x11_window_in_silence(self):
        for app in ("alacritty", "google-chrome", "code", "nemo"):
            with self.subTest(app=app):
                self.windows = [x11(31, 2, app=app)]
                self.off_with("Charlie", expect=2)
                self.close.assert_not_called()
        self.assertIn("X11 client that cannot be archived", self.err.getvalue())

    def test_every_provider_identity_is_known_on_wayland(self):
        self.windows = [window(41, 2, app="Alacritty"), window(42, 2, app="trilium"), window(43, 2, app="google-chrome"),
                        window(44, 2, app="code"), window(45, 2, app="org.example.other")]
        shell = self.shell()
        known, unknown, problems = switch._classify(cli, shell, shell["workspaces"][2])
        self.assertEqual((sorted(known), sorted(unknown), problems), ([41, 42, 43, 44], [45], []))

    def test_without_the_x11_capability_everything_blocks_as_before_and_advises_the_update(self):
        self.capabilities = ["close_window", "edit_workspaces"]
        self.windows = [x11(31, 8)]
        self.off_with("India", expect=2)
        self.assertIn("update gnome-winctl", self.err.getvalue())
        self.close.assert_not_called()
        self.remove.assert_not_called()

    def test_without_the_edit_capability_the_workspace_stays_in_gnome(self):
        self.capabilities = ["close_window", "close_x11_window"]
        self.off_with("India")
        self.remove.assert_not_called()
        self.assertEqual(self.names, ORDER)
        self.assertEqual(self.archive("India")["phase"], "off")

    def test_a_workspace_with_the_old_contract_is_not_edited(self):
        self.capabilities = ["close_window"]
        self.windows = [window(14, 2)]
        self.off_with("Charlie")
        self.close.assert_called_once_with(14, workspace=2, name="Charlie", pid=1400, epoch="epoch")
        self.remove.assert_not_called()


class ScopeTests(DisposableBase):
    def setUp(self):
        super().setUp()
        self.path = f"{SCOPES}/app-gnome-steam-3100.scope"
        self.windows = [x11(31, 8, pid=3100)]
        self.cgroups = {3100: self.path}
        self.scopes = {self.path: [3100, 3101]}

    def test_a_tray_application_is_ended_by_sigterm_of_its_scope_alone(self):
        self.off_with("India")
        self.assertEqual(self.terminated, ["app-gnome-steam-3100.scope"])
        self.assertEqual(self.archive("India")["phase"], "off")

    def test_the_launch_service_of_a_configured_app_is_ended_too(self):
        path = f"{SCOPES}/wsctl-app-social-steam-0a1b2c3d.service"
        self.cgroups, self.scopes = {3100: path}, {path: [3100]}
        self.off_with("India")
        self.assertEqual(self.terminated, ["wsctl-app-social-steam-0a1b2c3d.service"])

    def test_a_service_session_or_unrelated_unit_is_never_touched(self):
        for leaf in ("wsctl-app-alacritty-0a1b2c3d.service", "wsctl-app-social-steam-1234.service", "session-3.scope", "org.gnome.Shell@wayland.service",
                     "dbus.service", "vte-spawn-1.scope"):
            with self.subTest(unit=leaf):
                path = f"{SCOPES}/{leaf}"
                self.cgroups, self.scopes = {3100: path}, {path: [3100]}
                self.windows = [x11(31, 8, pid=3100)]
                self.off_with("India")
                self.assertEqual(self.terminated, [])
                switch._path("India").unlink()
                self.names = list(ORDER)
        system = "/system.slice/app-steam.scope"
        self.assertIsNone(switch._scope_unit(system))
        self.assertEqual(switch._scope_unit(f"{SCOPES}/snap.steam.steam-1.scope"), "snap.steam.steam-1.scope")
        self.assertIsNone(switch._scope_unit(f"{SCOPES}/app-x.scope/child"))
        self.assertEqual(switch._scope_unit(f"{SCOPES}/wsctl-app-social-steam-0a1b2c3d.service"),
                         "wsctl-app-social-steam-0a1b2c3d.service")
        self.assertIsNone(switch._scope_unit(f"{SCOPES}/wsctl-app-chrome-0a1b2c3d.service"))

    def test_a_window_on_another_workspace_in_the_same_cgroup_keeps_the_scope(self):
        self.windows.append(x11(32, 1, pid=3105))
        self.cgroups[3105] = self.path
        self.off_with("India")
        self.assertEqual(self.terminated, [])
        self.assertEqual(self.archive("India")["phase"], "off")

    def test_a_known_window_in_the_same_cgroup_keeps_the_scope(self):
        self.windows.append(window(14, 8, pid=1400))
        self.cgroups[1400] = self.path
        self.off_with("India")
        self.assertEqual(self.terminated, [])

    def test_the_cgroup_of_a_known_window_closed_earlier_is_remembered(self):
        entry = {"known_scopes": [self.path], "unknown": [{"id": 31, "pid": 3100, "cgroup": self.path, "app": "steam"}]}
        self.windows = []
        self.assertEqual(switch._terminate_scopes(self.shell(), entry, {31}), [])
        self.assertEqual(self.terminated, [])

    def test_a_scope_without_processes_or_with_a_tmux_server_is_left_alone(self):
        self.scopes[self.path] = []
        self.off_with("India")
        self.assertEqual(self.terminated, [])
        switch._path("India").unlink()
        self.names, self.windows, self.scopes[self.path] = list(ORDER), [x11(31, 8, pid=3100)], [3100, 3102]
        with patch.object(switch, "_process_name", side_effect=lambda pid: "tmux: server" if pid == 3102 else "steam"):
            self.off_with("India")
        self.assertEqual(self.terminated, [])

    def test_the_scope_is_ended_by_sigterm_through_systemctl_kill_only(self):
        with patch("workspace_state.util.run", return_value="") as run:
            switch._terminate_unit.__wrapped__("app-gnome-steam-3100.scope")
        run.assert_called_once_with(["/usr/bin/systemctl", "--user", "kill", "--signal=SIGTERM",
                                     "app-gnome-steam-3100.scope"])

    def test_a_window_that_stays_open_fails_off_before_any_known_window_is_closed(self):
        self.windows.append(window(14, 8, pid=1400))
        self.cgroups[1400] = f"{SCOPES}/app-nemo-1400.scope"
        self.scopes[self.path] = [3100]
        self.close.side_effect = lambda wid, **_k: None  # the window ignores the request
        with patch.object(switch, "UNKNOWN_CLOSE_WAIT_SECONDS", 0):
            self.off_with("India", expect=3)
        self.assertEqual([call.args[0] for call in self.close.call_args_list], [31])
        self.assertEqual(self.archive("India")["phase"], "closing")
        self.assertEqual({item["id"] for item in self.windows}, {14, 31})
        self.assertEqual(self.terminated, [])
        self.assertIn("did not close; it may be asking to save", self.err.getvalue())

    def test_a_window_gnome_cannot_list_after_the_close_blocks_the_sigterm(self):
        def close(wid, **_kwargs):
            self.windows = []
            self.blocking = {8: 1}  # a save prompt GNOME does not list
        self.close.side_effect = close
        self.off_with("India", expect=3)
        self.assertEqual(self.terminated, [])

    def test_retry_after_the_windows_went_but_the_scope_remained_ends_it_without_new_windows(self):
        with patch.object(switch, "_terminate_unit", side_effect=CommandError("busy")):
            self.off_with("India", expect=3)
        self.assertEqual(self.archive("India")["phase"], "closing")
        self.assertEqual(self.windows, [])
        self.off_with("India")
        self.assertEqual(self.terminated, ["app-gnome-steam-3100.scope"])
        self.assertEqual(self.archive("India")["phase"], "off")


class OtherWindowTests(DisposableBase):
    def setUp(self):
        super().setUp()
        self.path = f"{SCOPES}/app-gnome-steam-3100.scope"
        self.windows = [x11(31, 8, pid=3100, blocker="transient", transient_for=None)]
        self.other = [{"id": 91, "pid": 3100, "type": "DIALOG", "workspace": 8, "sticky": False, "transient_for": 31,
                       "title": "Save changes?", "wm_class": "steam", "client_type": "x11", "close_blocker": "unsupported_window"}]
        self.blockers = {8: [91]}
        self.cgroups = {3100: self.path}
        self.scopes = {self.path: [3100, 3101]}

    def family_closes(self, wid, **_kwargs):
        self.windows, self.other, self.blockers = [], [], {}

    def test_a_dialog_gnome_does_not_list_blocks_before_any_change(self):
        # The extension refuses a family with a non-NORMAL member and a workspace with a blocker.
        self.close.side_effect = self.family_closes
        self.off_with("India", expect=2)
        self.assertIn('dialog "Save changes?" of steam', self.err.getvalue())
        self.assertIn("close it and retry", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("India").exists())
        self.assertEqual(self.terminated, [])

    def test_a_family_of_normal_windows_is_closed_through_its_root(self):
        self.other, self.blockers = [], {}
        self.windows.append(x11(32, 8, pid=3100, blocker="transient", transient_for=31))
        self.close.side_effect = self.family_closes
        self.off_with("India")
        self.close.assert_called_once_with(31, workspace=8, name="India", pid=3100, epoch="epoch",
                                           allow_x11=True, allow_transient=True)
        self.assertEqual(self.terminated, ["app-gnome-steam-3100.scope"])

    def test_a_normal_child_that_mutter_put_on_another_workspace_blocks_before_any_change(self):
        self.other, self.blockers = [], {}
        self.windows = [x11(31, 8, pid=3100, blocker="transient", transient_for=None, title="Probe Two"),
                        x11(32, 1, pid=3100, blocker="transient", transient_for=31, title="Probe Two Settings",
                            app="probe")]
        self.close.side_effect = self.family_closes
        self.off_with("India", expect=2)
        self.assertIn('window "Probe Two Settings" of probe belongs to "Probe Two" but is on workspace Bravo; '
                      "close or move it and retry", self.err.getvalue())
        self.close.assert_not_called()
        self.assertFalse(switch._path("India").exists())
        self.assertEqual(self.terminated, [])

    def test_a_save_dialog_that_appears_after_the_request_keeps_the_save_hint(self):
        self.other, self.blockers = [], {}
        self.windows = [x11(31, 8, pid=3100, blocker="transient", transient_for=None, title="Unknown Save",
                            app="unknown_app.py")]

        def asks_to_save(wid, **_kwargs):
            # Mutter puts the modal dialog on the active workspace, not on the target one.
            self.other = [{"id": 40, "pid": 3100, "type": "DIALOG", "workspace": 1, "sticky": False,
                           "transient_for": 31, "title": "", "wm_class": "unknown_app.py", "client_type": "x11",
                           "close_blocker": "unsupported_window"}]
        self.close.side_effect = asks_to_save
        with patch.object(switch, "UNKNOWN_CLOSE_WAIT_SECONDS", 0):
            self.off_with("India", expect=3)
        message = self.err.getvalue()
        self.assertIn('unrecognized window "Unknown Save" did not close; it may be asking to save', message)
        self.assertNotIn("belongs to", message)
        self.assertNotIn('"40"', message)
        self.assertEqual(self.terminated, [])

    def test_the_save_hint_survives_closing_the_next_unknown_window(self):
        self.other, self.blockers = [], {}
        self.windows = [x11(31, 8, pid=3100, blocker="transient", transient_for=None, title="Unknown Save",
                            app="unknown_app.py"),
                        window(32, 8, app="unknown_app.py", pid=3200, title="Unknown Wayland")]

        def close(wid, **_kwargs):
            if wid == 31:
                self.other = [{"id": 40, "pid": 3100, "type": "DIALOG", "workspace": 1, "sticky": False,
                               "transient_for": 31, "title": "", "wm_class": "unknown_app.py", "client_type": "x11",
                               "close_blocker": "unsupported_window"}]
            else:
                self.windows = [item for item in self.windows if item["id"] != wid]
        self.close.side_effect = close
        with patch.object(switch, "UNKNOWN_CLOSE_WAIT_SECONDS", 0):
            self.off_with("India", expect=3)
        message = self.err.getvalue()
        self.assertIn('unrecognized window "Unknown Save" did not close; it may be asking to save', message)
        self.assertNotIn("belongs to", message)
        self.assertEqual([call.args[0] for call in self.close.call_args_list], [31, 32])
        self.assertEqual(self.terminated, [])

    def test_a_window_without_a_title_is_never_named_by_its_number(self):
        self.other[0].update(title="")
        self.off_with("India", expect=2)
        self.assertIn("untitled dialog of steam", self.err.getvalue())
        self.assertNotIn('"91"', self.err.getvalue())

    def test_the_far_child_is_found_through_the_unlisted_windows_too(self):
        self.blockers = {}
        self.windows = [x11(31, 8, pid=3100, blocker=None, transient_for=None, title="Probe Two")]
        self.other[0].update(workspace=1, transient_for=31, title="Probe Two Settings")
        self.off_with("India", expect=2)
        self.assertIn("is on workspace Bravo", self.err.getvalue())
        self.close.assert_not_called()

    def test_a_dialog_of_a_known_window_is_named_and_blocks(self):
        self.windows = [window(14, 8, pid=1400, blocker="transient", transient_for=None)]
        self.other[0].update(transient_for=14, wm_class="nemo", pid=1400)
        self.off_with("India", expect=2)
        self.assertIn('dialog "Save changes?" of nemo', self.err.getvalue())
        self.close.assert_not_called()

    def test_a_window_of_no_family_is_named_with_its_kind(self):
        self.windows = []
        self.other[0].update(transient_for=None, wm_class="unknown_app")
        self.off_with("India", expect=2)
        self.assertIn('dialog "Save changes?" of unknown_app', self.err.getvalue())
        self.assertNotIn("GNOME cannot list", self.err.getvalue())

    def test_a_listed_or_unlisted_window_of_the_application_elsewhere_keeps_the_scope(self):
        self.other.append({"id": 92, "pid": 3105, "type": "DIALOG", "workspace": 1, "sticky": False,
                           "transient_for": None, "title": "Other", "wm_class": "steam", "client_type": "x11",
                           "close_blocker": "unsupported_window"})
        self.cgroups[3105] = self.path
        self.other, self.blockers = self.other[1:], {}
        self.close.side_effect = lambda wid, **_k: (setattr(self, "windows", []), setattr(self, "blockers", {}),
                                                    setattr(self, "other", self.other))
        self.off_with("India")
        self.assertEqual(self.terminated, [])  # a window of another workspace is in the same cgroup

    def test_unlisted_windows_of_another_workspace_occupy_the_scope(self):
        entry = {"known_scopes": [], "unknown": [{"id": 31, "pid": 3100, "cgroup": self.path, "app": "steam"}]}
        self.windows = []
        self.other = [{"id": 92, "pid": 3105, "type": "DIALOG", "workspace": 1, "sticky": False,
                       "transient_for": None, "title": "Other", "wm_class": "x", "client_type": "wayland",
                       "close_blocker": "unsupported_window"}]
        self.cgroups[3105] = self.path
        self.assertEqual(switch._terminate_scopes(self.shell(), entry, {31}), [])
        self.assertEqual(self.terminated, [])

    def test_without_the_field_the_old_count_message_stays(self):
        self.other = None
        self.blockers = {}
        self.blocking = {8: 1}
        self.windows = [window(14, 8)]
        self.off_with("India", expect=2)
        self.assertIn("1 window(s) GNOME cannot list", self.err.getvalue())


class PrimaryWorkspaceTests(DisposableBase):
    def test_the_first_workspace_in_the_order_is_primary_and_never_switched_off(self):
        self.windows = [window(14, 0)]
        self.assertEqual(self.apply("off", "Alpha"), 2)
        self.assertIn("Alpha is the primary workspace and always stays on", self.err.getvalue())
        self.assertEqual(self.apply("off", "alpha"), 2)
        self.close.assert_not_called()
        self.remove.assert_not_called()
        self.assertFalse(switch._path("Alpha").exists())

    def test_list_marks_only_the_primary_row(self):
        self.off_with("Echo")
        rows = self.listed()
        self.assertEqual([row.get("primary") for row in rows], [True] + [None] * 8)

    def test_cleanup_never_removes_the_primary_workspace(self):
        entry = {"version": 1, "name": "Alpha", "epoch": "epoch", "token": "t", "snapshot": {}, "originals": {},
                 "phase": "off", "detail": ""}
        switch._directory().mkdir(mode=0o700, parents=True, exist_ok=True)
        switch._write(entry, "off")
        self.assertEqual(switch.prune_disabled(), 0)
        self.remove.assert_not_called()
        self.assertIn("Alpha", self.names)

    def test_the_primary_follows_the_canonical_order_even_while_the_first_one_is_absent(self):
        self.off_with("Bravo")
        self.assertEqual(self.apply("off", "Alpha"), 2)
        self.assertEqual(self.apply("off", "Charlie"), 0)
        self.assertEqual(switch._primary_name(), "Alpha")


class RemovalAndInsertionTests(DisposableBase):
    def test_off_removes_the_workspace_and_keeps_the_order(self):
        self.off_with("Echo")
        self.remove.assert_called_once_with(4, "Echo", "epoch")
        self.assertNotIn("Echo", self.names)
        self.assertEqual(json.loads(switch._order_path().read_text())["names"], ORDER)
        self.assertFalse(switch._order_path().with_suffix(".tmp").exists())

    def test_the_last_workspace_is_never_switched_off(self):
        self.names = ["Only"]
        self.enterContext(patch.object(switch, "_primary_name", return_value=None))
        self.assertEqual(self.apply("off", "Only"), 2)
        self.assertIn("last workspace", self.err.getvalue())
        self.remove.assert_not_called()
        self.assertFalse(switch._path("Only").exists())

    def test_on_inserts_the_workspace_by_the_count_of_present_names_before_it(self):
        for name in ("Echo", "Foxtrot", "Hotel", "India"):
            self.off_with(name)
        self.assertEqual(self.names, ["Alpha", "Bravo", "Charlie", "Delta", "Golf"])
        for name, expected in (("Hotel", ["Alpha", "Bravo", "Charlie", "Delta", "Golf", "Hotel"]),
                               ("Echo", ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Golf", "Hotel"]),
                               ("India", ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Golf", "Hotel", "India"]),
                               ("Foxtrot", ORDER)):
            self.assertEqual(self.apply("on", name), 0, self.err.getvalue())
            self.assertEqual(self.names, expected)
            self.assertEqual(self.archive(name)["phase"], "on")
        self.assertEqual([call.args[:2] for call in self.insert.call_args_list],
                         [(5, "Hotel"), (4, "Echo"), (7, "India"), (5, "Foxtrot")])

    def test_off_of_a_removed_workspace_is_a_no_op(self):
        self.off_with("Echo")
        self.close.reset_mock()
        self.remove.reset_mock()
        self.assertEqual(self.apply("off", "Echo"), 0)
        self.remove.assert_not_called()
        self.assertEqual(self.archive("Echo")["phase"], "off")

    def test_list_shows_every_workspace_in_the_canonical_order_with_the_removed_ones_off(self):
        for name in ("Echo", "India"):
            self.off_with(name)
        rows = self.listed()
        self.assertEqual([row["name"] for row in rows], ORDER)
        self.assertEqual({row["name"]: row["state"] for row in rows},
                         {name: "off" if name in {"Echo", "India"} else "on" for name in ORDER})
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(cli.main(["workspace", "list"]), 0)
        self.assertEqual(self.out.getvalue().splitlines()[4], "Echo\toff\t")

    def test_the_order_starts_from_gnome_and_adds_new_names_after_their_predecessor(self):
        self.off_with("Bravo")
        self.names.insert(2, "Fresh")  # a workspace the user added after Charlie... Bravo is removed
        self.names.append("Tail")
        self.assertEqual(self.apply("off", "Tail"), 0)
        self.assertEqual(json.loads(switch._order_path().read_text())["names"],
                         ["Alpha", "Bravo", "Charlie", "Fresh", "Delta", "Echo", "Foxtrot", "Golf", "Hotel", "India", "Tail"])
        self.assertEqual(self.archive("Tail")["phase"], "off")
        self.assertEqual(set(switch._archives()), {"bravo", "tail"})

    def test_a_name_gnome_lost_by_hand_leaves_the_order(self):
        self.assertEqual(self.apply("off", "Echo"), 0)
        self.names.remove("Golf")
        self.assertEqual(self.apply("off", "India"), 0)
        self.assertNotIn("Golf", json.loads(switch._order_path().read_text())["names"])

    def test_interrupted_between_insertion_and_restoration_a_retry_does_not_insert_again(self):
        self.off_with("Echo")
        with patch.object(switch, "_on", side_effect=switch.SwitchPartial("interrupted")):
            self.assertEqual(self.apply("on", "Echo"), 3)
        self.assertEqual(self.names, ORDER)
        self.assertEqual(self.archive("Echo")["phase"], "off")
        self.assertEqual(self.apply("on", "Echo"), 0, self.err.getvalue())
        self.assertEqual(self.insert.call_count, 1)
        self.assertEqual(self.archive("Echo")["phase"], "on")
        self.assertEqual(self.names, ORDER)

    def test_insertion_failure_leaves_the_archive_off(self):
        self.off_with("Echo")
        self.insert.side_effect = CommandError("GNOME refused to insert workspace Echo")
        self.assertEqual(self.apply("on", "Echo"), 2)
        self.assertEqual(self.archive("Echo")["phase"], "off")

    def test_on_needs_the_edit_capability_for_a_removed_workspace(self):
        self.off_with("Echo")
        self.capabilities = ["close_window", "close_x11_window"]
        self.assertEqual(self.apply("on", "Echo"), 2)
        self.assertIn("Update gnome-winctl", self.err.getvalue())

    def test_another_unfinished_switch_blocks_editing_workspaces_but_not_off(self):
        self.windows = [window(14, 2)]
        self.close.side_effect = lambda wid, **_k: None
        with patch.object(switch, "CLOSE_WAIT_SECONDS", 0):
            self.off_with("Charlie", expect=3)
        self.assertEqual(self.archive("Charlie")["phase"], "closing")
        self.assertEqual(self.apply("off", "Echo"), 0)
        self.assertIn("stays in GNOME: Finish the switch of workspace Charlie", self.err.getvalue())
        self.remove.assert_not_called()
        self.assertEqual(self.archive("Echo")["phase"], "off")

    def test_forget_puts_a_removed_workspace_back_before_discarding_its_archive(self):
        self.off_with("Echo")
        self.assertEqual(cli.main(["workspace", "forget", "--", "Echo"]), 2)
        self.assertIn("An empty workspace Echo is put back", self.err.getvalue())
        self.assertTrue(switch._path("Echo").exists())
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Echo"]), 0)
        self.assertEqual(self.names, ORDER)
        self.assertFalse(switch._path("Echo").exists())
        self.assertEqual([row["state"] for row in self.listed()], ["on"] * 9)

    def test_forget_keeps_the_archive_when_the_workspace_cannot_come_back(self):
        self.off_with("Echo")
        self.insert.side_effect = CommandError("GNOME refused to insert workspace Echo")
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Echo"]), 2)
        self.assertTrue(switch._path("Echo").exists())


class DetailTests(DisposableBase):
    def test_unnamed_workspaces_stay_out_of_the_order_and_keep_their_place(self):
        for names in (["Alpha", "Bravo", "", "Delta"], ["Alpha", "", "Bravo", "Delta"], ["", "Alpha", "Bravo"]):
            with self.subTest(names=names):
                self.names = list(names)
                self.off_with("Bravo")
                self.assertNotIn("", json.loads(switch._order_path().read_text())["names"])
                self.assertEqual(self.apply("on", "Bravo"), 0, self.err.getvalue())
                self.assertEqual(self.names, names)
                switch._path("Bravo").unlink()

    def test_a_real_shaped_unnamed_workspace_is_not_a_name_and_never_primary(self):
        self.names = ["", "Alpha", "Bravo", "Delta"]
        self.assertEqual(self.apply("off", "Alpha"), 2)
        self.assertIn("Alpha is the primary workspace", self.err.getvalue())
        self.assertEqual(switch._read_order(), ["Alpha", "Bravo", "Delta"])
        self.assertEqual(self.listed()[0]["name"], "Alpha")

    def test_bravo_returns_to_index_one_beside_a_real_shaped_unnamed_workspace(self):
        self.names = ["Alpha", "Bravo", "", "Delta"]
        self.off_with("Bravo")
        self.assertEqual(self.names, ["Alpha", "", "Delta"])
        self.assertEqual(self.apply("on", "Bravo"), 0, self.err.getvalue())
        self.assertEqual(self.names, ["Alpha", "Bravo", "", "Delta"])
        self.assertEqual(self.insert.call_args.args[:2], (1, "Bravo"))

    def test_forget_of_a_removed_workspace_is_refused_while_gnome_is_unavailable(self):
        self.off_with("Echo")
        with patch.object(desktop, "capture_shell", return_value={"available": False, "windows": [],
                                                                    "monitors": [], "workspaces": []}):
            self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Echo"]), 2)
        self.assertTrue(switch._path("Echo").exists())
        self.assertIn("unavailable", self.err.getvalue())

    def test_forget_of_a_removed_workspace_is_refused_when_it_cannot_come_back(self):
        self.off_with("Echo")
        self.capabilities = ["close_window", "close_x11_window"]
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Echo"]), 2)
        self.assertIn("Update gnome-winctl", self.err.getvalue())
        self.assertTrue(switch._path("Echo").exists())

    def test_the_unreadable_archive_message_mentions_the_returning_workspace(self):
        self.assertIn("empty workspace", str(switch._unreadable_error("Echo")))

    def test_forget_of_an_unreadable_archive_of_a_removed_workspace_puts_it_back(self):
        self.off_with("Echo")
        switch._path("Echo").write_text("{")
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Echo"]), 0, self.err.getvalue())
        self.assertEqual(self.names, ORDER)


class PlacementWaitTests(DisposableBase):
    def pending(self, times):
        calls = {"n": 0}

        def remove(index, name, epoch):
            calls["n"] += 1
            if calls["n"] <= times:
                raise desktop.PlacementsPending(PENDING)
            return self.removed(index, name, epoch)
        self.remove.side_effect = remove
        return calls

    def test_removal_retries_while_placements_are_pending(self):
        calls = self.pending(2)
        self.off_with("Echo")
        self.assertEqual((calls["n"], "Echo" in self.names), (3, False))

    def test_a_manual_off_that_stays_pending_says_to_retry_with_rc_3(self):
        self.pending(10 ** 6)
        with patch.object(switch, "PLACEMENT_WAIT_SECONDS", 0):
            self.off_with("Echo", expect=3)
        self.assertIn("window placements are still in progress; retry in a moment", self.err.getvalue())
        self.assertEqual(self.archive("Echo")["phase"], "off")

    def test_insertion_waits_the_same_way(self):
        self.off_with("Echo")
        calls = {"n": 0}

        def insert(index, name, epoch):
            calls["n"] += 1
            if calls["n"] < 3:
                raise desktop.PlacementsPending(PENDING)
            return self.inserted(index, name, epoch)
        self.insert.side_effect = insert
        self.assertEqual(self.apply("on", "Echo"), 0, self.err.getvalue())
        self.assertEqual(calls["n"], 3)

    def test_the_wait_for_the_own_edit_is_twenty_seconds_at_most(self):
        self.assertLessEqual(switch.PLACEMENT_WAIT_SECONDS, 20)

    def test_the_cleanup_of_other_workspaces_makes_one_attempt_and_stops(self):
        self.capabilities = ["close_window", "close_x11_window"]
        for name in ("Echo", "Foxtrot"):
            self.off_with(name)
        self.capabilities = CAPABILITIES
        calls = []

        def remove(index, name, epoch):
            calls.append(name)
            if name != "Golf":
                raise desktop.PlacementsPending(PENDING)
            return self.removed(index, name, epoch)
        self.remove.side_effect = remove
        self.off_with("Golf")
        self.assertEqual(calls, ["Golf", "Echo"])

    def test_the_login_cleanup_makes_one_attempt_per_run_and_says_why_it_stopped(self):
        self.capabilities = ["close_window", "close_x11_window"]
        for name in ("Echo", "Foxtrot"):
            self.off_with(name)
        self.capabilities = CAPABILITIES
        self.remove.side_effect = desktop.PlacementsPending(PENDING)
        reasons = []
        self.assertEqual(switch.prune_disabled(report=reasons.append), 0)
        self.assertEqual(self.remove.call_count, 1)
        self.assertIn("stopped", reasons[-1])

    def test_the_wrapper_names_the_pending_refusal(self):
        with patch.object(desktop, "_winctl", return_value={"status": "refused", "reason": "placements_pending"}):
            with self.assertRaises(desktop.PlacementsPending):
                REAL_REMOVE_WORKSPACE(0, "Echo", "e")


class MigrationTests(DisposableBase):
    def test_a_manual_switch_also_removes_the_other_empty_workspaces_that_are_off(self):
        self.capabilities = ["close_window", "close_x11_window"]
        for name in ("Echo", "Foxtrot"):
            self.off_with(name)
        self.capabilities = CAPABILITIES
        self.off_with("Golf")
        self.assertEqual(self.names, ["Alpha", "Bravo", "Charlie", "Delta", "Hotel", "India"])
        self.assertEqual(self.apply("on", "Echo"), 0)
        self.assertIn("Echo", self.names)

    def test_a_failing_cleanup_of_the_others_leaves_the_result_of_the_switch(self):
        self.capabilities = ["close_window", "close_x11_window"]
        self.off_with("Echo")
        self.capabilities = CAPABILITIES
        def remove(index, name, epoch):
            if name == "Echo":
                raise CommandError("refused")
            return self.removed(index, name, epoch)
        self.remove.side_effect = remove
        self.off_with("Golf")
        self.assertEqual(self.archive("Golf")["phase"], "off")
        self.assertIn("Echo", self.names)

    def test_cleanup_waits_for_the_locks_and_reports_every_skip(self):
        self.capabilities = ["close_window", "close_x11_window"]
        self.off_with("Echo")
        self.capabilities = CAPABILITIES
        reasons = []
        real = switch._switch_lock
        calls = {"n": 0}

        def lock():
            calls["n"] += 1
            if calls["n"] < 3:
                raise storage.StateBusy("another workspace switch is running")
            return real()
        with patch.object(switch, "_switch_lock", side_effect=lock):
            self.assertEqual(switch.prune_disabled(report=reasons.append), 1)
        self.assertEqual(reasons, ["removed switched-off workspace Echo"])
        reasons.clear()
        with patch.object(switch, "_switch_lock", side_effect=storage.StateBusy("another workspace switch is running")), \
             patch.object(switch, "PRUNE_WAIT_SECONDS", 0):
            self.names.insert(4, "Echo")
            self.assertEqual(switch.prune_disabled(report=reasons.append), 0)
        self.assertTrue(reasons and "another workspace switch is running" in reasons[0])

    def test_cleanup_reports_why_it_removed_nothing(self):
        reasons = []
        self.capabilities = ["close_window"]
        self.assertEqual(switch.prune_disabled(report=reasons.append), 0)
        self.assertIn("edit_workspaces", reasons[-1])

    def present_off(self, *names):
        """Switched off in an older release: archived, but still in GNOME."""
        self.capabilities = ["close_window", "close_x11_window"]
        for name in names:
            self.off_with(name)
        self.capabilities = CAPABILITIES

    def test_repeating_off_removes_an_empty_workspace_that_is_off_but_present(self):
        self.present_off("Echo", "Foxtrot")
        self.assertEqual(self.names, ORDER)
        self.assertEqual(self.apply("off", "Echo"), 0)
        # The other parked workspace is removed along with it.
        self.assertEqual(self.names, [name for name in ORDER if name not in {"Echo", "Foxtrot"}])
        self.assertEqual(self.archive("Echo")["phase"], "off")

    def test_login_cleanup_removes_every_empty_workspace_that_is_off_but_present(self):
        self.present_off("Echo", "Foxtrot", "Hotel", "India")
        self.assertEqual(switch.prune_disabled(), 4)
        self.assertEqual(self.names, ["Alpha", "Bravo", "Charlie", "Delta", "Golf"])
        self.assertEqual([row["name"] for row in self.listed()], ORDER)
        self.assertEqual(switch.prune_disabled(), 0)

    def test_cleanup_keeps_a_workspace_with_windows_and_the_last_one(self):
        self.present_off("Echo", "Foxtrot")
        self.windows = [window(14, 4)]
        self.assertEqual(switch.prune_disabled(), 1)
        self.assertIn("Echo", self.names)
        self.assertNotIn("Foxtrot", self.names)
        self.names = ["Echo"]
        self.windows = []
        self.assertEqual(switch.prune_disabled(), 0)

    def test_cleanup_waits_for_an_unfinished_switch_and_an_old_gnome_winctl(self):
        self.present_off("Echo")
        entry = self.archive("Echo")
        entry["name"] = "Other"
        self.windows = [window(14, 2)]
        self.close.side_effect = lambda wid, **_k: None
        with patch.object(switch, "CLOSE_WAIT_SECONDS", 0):
            self.off_with("Charlie", expect=3)
        self.assertEqual(switch.prune_disabled(), 0)
        self.windows = []
        self.capabilities = ["close_window"]
        switch._path("Charlie").unlink()
        self.assertEqual(switch.prune_disabled(), 0)
        self.remove.assert_not_called()

    def test_cleanup_is_skipped_while_a_switch_runs(self):
        self.present_off("Echo")
        with switch._switch_lock(), patch.object(switch, "PRUNE_WAIT_SECONDS", 0):
            self.assertEqual(switch.prune_disabled(), 0)
        self.remove.assert_not_called()

    def test_finalizer_waits_a_bounded_time_for_pending_results_and_says_why_it_skipped(self):
        with patch.object(login_finalize, "status_path") as status, \
             patch("workspace_state.provider_progress.has_pending", return_value=True), \
             patch.object(login_finalize.time, "sleep"), \
             patch.object(switch, "prune_disabled") as prune, \
             patch.object(login_finalize, "append_diagnostic") as diagnostic:
            status.return_value.read_text.return_value = "{}"
            login_finalize._prune_disabled_workspaces(wait=0)
            prune.assert_not_called()
            self.assertIn("still pending", diagnostic.call_args.args[1])
            with patch("workspace_state.provider_progress.has_pending", side_effect=[True, True, False]):
                login_finalize._prune_disabled_workspaces(wait=30)
            prune.assert_called_once()

    def test_the_finalizer_of_an_already_completed_operation_still_prunes_once(self):
        status = {"operation_state": "completed", "stages": []}
        with patch.object(login_finalize.operations, "context_from_status", return_value=MagicMock()), \
             patch.object(login_finalize, "status_path") as path, \
             patch.object(login_finalize, "_prune_disabled_workspaces") as prune, \
             patch.object(login_finalize, "_finalize") as finalize:
            path.return_value.read_text.return_value = json.dumps(status)
            self.assertEqual(login_finalize.main(), 0)
        prune.assert_called_once_with()
        finalize.assert_not_called()

    def test_a_failing_cleanup_is_only_a_diagnostic(self):
        with patch.object(login_finalize, "status_path") as status, \
             patch("workspace_state.provider_progress.has_pending", return_value=False), \
             patch.object(switch, "prune_disabled", side_effect=RuntimeError("boom")), \
             patch.object(login_finalize, "append_diagnostic") as diagnostic:
            status.return_value.read_text.return_value = "{}"
            login_finalize._prune_disabled_workspaces()
        diagnostic.assert_called_once()
        self.assertIn("boom", diagnostic.call_args.args[1])


class NameBindingTests(DisposableBase):
    def test_a_removed_workspace_before_the_target_shifts_indices_but_not_names(self):
        placement = {"workspace": 6, "workspace_name": "Golf", "monitor": 0}
        self.names = [name for name in ORDER if name not in {"Echo", "Foxtrot"}]
        shell = self.shell()
        self.assertEqual(desktop.remap_workspace(placement, shell=shell)["workspace"], 4)
        self.assertEqual(desktop.resolve_placement_target(
            dict(placement, monitor_identity={"connector": "HDMI-1"}), provider="test",
            shell={**shell, "monitors": [{"index": 0, "connector": "HDMI-1", "identity": {"connector": "HDMI-1"}}]},
        )["workspace"], 4)

    def test_a_snapshot_made_before_the_removal_still_selects_its_windows_by_name(self):
        saved = recipe(ORDER, (14, 6), (15, 3))
        self.assertEqual(switch._native_ids(switch._project(
            saved, lambda _item, placement: switch._destination(saved, placement) == "golf", include_detached=False)), [14])
        self.names = ["Alpha", "Bravo", "Charlie", "Delta", "Golf"]
        self.assertEqual(switch._destination(saved, {"workspace": 4, "workspace_name": "Golf"}), "golf")
        self.assertEqual(switch._destination(saved, {"workspace": 6}), "golf")

    def test_on_restores_with_the_current_names_after_workspaces_were_removed(self):
        self.windows = [window(14, 6)]
        self.off_with("Golf")
        for name in ("Echo", "Foxtrot"):
            self.off_with(name)
        self.assertEqual(self.names, ["Alpha", "Bravo", "Charlie", "Delta", "Hotel", "India"])
        seen = {}

        def restore(snapshot, args):
            seen.update(names=args.workspace_names, workspace=args.workspace, saved=snapshot["desktop"]["workspace_names"])
            self.windows.append(window(24, self.names.index("Golf")))
            from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderCount, ProviderItemResult
            ok = PhaseEvidence(EvidenceState.VERIFIED)
            evidence = ProviderItemResult("nemo", "folder", ok, ok, ok, native_window_id=24)
            return {**dict.fromkeys(cli.CATEGORIES, 0), "codex_verified": 1, "file-manager": ProviderCount(1, [evidence])}

        observed = recipe(["Alpha", "Bravo", "Charlie", "Delta", "Golf", "Hotel", "India"], (24, 4))
        observed["file_manager"]["windows"][0]["locations"] = ["file:///example/14"]
        with patch.object(cli, "_restore", side_effect=restore), patch.object(switch, "_snapshot", return_value=observed), \
             patch.object(storage, "save") as save, patch.object(storage, "load", side_effect=FileNotFoundError):
            self.assertEqual(self.apply("on", "Golf"), 0, self.err.getvalue())
        self.assertEqual(seen["workspace"], "Golf")
        # The archive still holds the older list; the restore is given GNOME's present one.
        self.assertEqual(seen["saved"], ORDER)
        self.assertEqual(seen["names"], ("Alpha", "Bravo", "Charlie", "Delta", "Golf", "Hotel", "India"))
        self.assertEqual(self.names.index("Golf"), 4)
        self.assertEqual(save.call_args.args[0]["file_manager"]["windows"][0]["placement"]["workspace_name"], "Golf")

    def test_startup_waits_for_no_workspace_that_is_off(self):
        self.off_with("Echo")
        saved = recipe(ORDER, (14, 4), (15, 6))
        saved["sessions"] = [{"name": "alpha"}, {"name": "beta"}]
        saved["terminals"] = [{"session": "alpha", "placement": {"workspace": 4, "workspace_name": "Echo"}},
                              {"session": "beta", "placement": {"workspace": 6, "workspace_name": "Golf"}}]
        projected = switch.suppress(saved)
        args = type("Args", (), {"workspace": None, "session": None, "select": False})()
        self.assertEqual(cli._startup_workspace_names(projected, ["terminals"], args), {"Golf"})
        self.assertEqual(cli._startup_workspace_names(saved, ["terminals"], args), {"Echo", "Golf"})

    def test_a_missing_switched_off_workspace_is_neither_a_topology_nor_a_readiness_error(self):
        self.off_with("Echo")
        shell = self.shell()
        ready, _reason = desktop.desktop_readiness({**shell, "monitors": [{"index": 0}]}, set())
        self.assertTrue(ready)
        before = desktop.desktop_topology_signature(shell)
        self.assertEqual(before, desktop.desktop_topology_signature(self.shell()))
        self.assertEqual(json.loads(before)["workspaces"][4]["name"], "Foxtrot")

    def test_a_removed_workspace_with_an_unreadable_archive_still_pauses_its_windows(self):
        self.off_with("Echo")
        switch._path("Echo").write_text("{")
        self.assertTrue(switch._unreadable("Echo"))
        saved = recipe(self.names, (14, 3))
        saved["file_manager"]["windows"][0]["placement"].update(workspace_name="Echo", workspace=3)
        self.assertEqual(switch.suppress(saved)["file_manager"]["windows"], [])
        rows = {row["name"]: row for row in self.listed()}
        self.assertEqual(rows["Echo"]["state"], "partial")


class WinctlProcessTests(unittest.TestCase):
    """gnome-winctl prints a refusal as JSON on stdout and exits with 1."""

    def binary(self, payload, code=1):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        path = os.path.join(directory, "gnome-winctl")
        with open(path, "w") as stream:
            stream.write(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\nexit {code}\n")
        os.chmod(path, 0o755)
        self.enterContext(patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ["PATH"]}))

    def test_a_pending_refusal_in_stdout_with_exit_1_is_placements_pending(self):
        self.binary('{"status":"refused","reason":"placements_pending","message":"x"}')
        for call in (lambda: desktop.remove_workspace(2, "Echo", "e"), lambda: desktop.insert_workspace(2, "Echo", "e")):
            with self.assertRaises(desktop.PlacementsPending):
                call()

    def test_a_close_refusal_in_stdout_with_exit_1_names_its_reason(self):
        self.binary('{"status":"refused","reason":"unsupported_window"}')
        with self.assertRaisesRegex(CommandError, r"refused to close the exact workspace window \(unsupported_window\)"):
            desktop.close_window(7, workspace=2, name="Echo", pid=70, epoch="e")

    def test_the_real_unsupported_window_refusal_names_its_reason(self):
        self.binary('{"status":"refused","reason":"unsupported_window","ok":false}')
        with self.assertRaisesRegex(CommandError, r"\(unsupported_window\)"):
            desktop.close_window(7, workspace=2, name="Echo", pid=70, epoch="e", allow_transient=True)

    def test_other_failures_stay_plain_errors(self):
        self.binary("not json")
        with self.assertRaises(CommandError):
            desktop.remove_workspace(2, "Echo", "e")
        self.binary('{"status":"removed","workspaces":[]}', code=0)
        self.assertEqual(desktop.remove_workspace(2, "Echo", "e"), [])

    def test_a_place_refusal_keeps_its_old_failure_semantics(self):
        self.binary('{"status":"refused","reason":"x"}')
        self.assertEqual(desktop.place_by_title("t", {}), False)


class WinctlWrapperTests(unittest.TestCase):
    def test_close_passes_the_x11_flag_only_when_asked(self):
        with patch.object(desktop, "_winctl", return_value={"status": "requested"}) as winctl:
            desktop.close_window(7, workspace=2, name="India", pid=70, epoch="e")
            desktop.close_window(7, workspace=2, name="India", pid=70, epoch="e", allow_x11=True)
        self.assertEqual(winctl.call_args_list[0].args[0], [
            "close", "7", "--workspace", "2", "--pid", "70", "--epoch", "e", "--workspace-name=India"])
        self.assertEqual(winctl.call_args_list[1].args[0][-1], "--allow-x11")

    def test_close_passes_the_transient_consent_and_names_a_refusal(self):
        with patch.object(desktop, "_winctl", return_value={"status": "requested", "closed": [8, 7]}) as winctl:
            desktop.close_window(7, workspace=2, name="Gaming", pid=70, epoch="e", allow_x11=True, allow_transient=True)
        self.assertEqual(winctl.call_args.args[0][-2:], ["--allow-x11", "--allow-transient"])
        with patch.object(desktop, "_winctl", return_value={"status": "refused", "reason": "unsupported_window"}):
            with self.assertRaisesRegex(CommandError, "unsupported_window"):
                desktop.close_window(7, workspace=2, name="Gaming", pid=70, epoch="e", allow_transient=True)

    def test_close_refusal_is_an_error(self):
        with patch.object(desktop, "_winctl", return_value={"status": "refused"}):
            with self.assertRaises(CommandError):
                desktop.close_window(7, workspace=2, name="India", pid=70, epoch="e", allow_x11=True)

    def test_remove_and_insert_follow_the_contract(self):
        names = [{"index": 0, "name": "Alpha"}]
        with patch.object(desktop, "_winctl", return_value={"status": "removed", "workspaces": names}) as winctl:
            self.assertEqual(desktop.remove_workspace(3, "Echo", "e"), names)
        self.assertEqual(winctl.call_args.args[0], ["workspace-remove", "--index", "3", "--epoch", "e",
                                                    "--workspace-name=Echo"])
        with patch.object(desktop, "_winctl", return_value={"status": "inserted", "workspaces": names}) as winctl:
            self.assertEqual(desktop.insert_workspace(3, "Echo", "e"), names)
        self.assertEqual(winctl.call_args.args[0][0], "workspace-insert")

    def test_a_refusal_names_the_reason(self):
        with patch.object(desktop, "_winctl", return_value={"status": "refused", "reason": "last", "message": "it is the last"}):
            with self.assertRaisesRegex(CommandError, "remove workspace Echo: it is the last"):
                desktop.remove_workspace(0, "Echo", "e")
        with patch.object(desktop, "_winctl", return_value={"status": "refused"}):
            with self.assertRaisesRegex(CommandError, "insert workspace Echo"):
                desktop.insert_workspace(0, "Echo", "e")


class TriliumSwitchTests(DisposableBase):
    def test_two_trilium_windows_do_not_block_off_and_are_archived_independently(self):
        from workspace_state.social_apps import TRILIUM
        captured = {"social_apps": {"trilium": {"_native_window_count": 2}}}
        scoped = {"social_apps": {"trilium": {"windows": [{}]}}, "sessions": []}
        switch._verify_close_recipes(cli, captured, scoped)
        slack = {"social_apps": {"slack": {"windows": [{}]}}, "sessions": []}
        with self.assertRaisesRegex(switch.SwitchBlocked, "has several windows"):
            switch._verify_close_recipes(cli, {"social_apps": {"slack": {"_native_window_count": 2}}}, slack)
        self.assertTrue(TRILIUM.independent_windows)

    def test_off_archives_both_trilium_windows_of_the_workspace_and_leaves_the_other_one(self):
        self.windows = [window(51, 3, app="trilium", pid=5100), window(52, 3, app="trilium", pid=5200),
                        window(53, 1, app="trilium", pid=5300)]
        monitor = {"connector": "HDMI-2", "serial": "display-1"}
        geometry = {"x": 0, "y": 0, "width": 800, "height": 600}

        def placement(wid):
            return {"workspace": 3, "workspace_name": "Delta", "monitor": 0, "monitor_identity": monitor,
                    "monitor_geometry": geometry, "geometry": geometry, "geometry_relative": geometry,
                    "state": "normal", "_native_id": wid}

        apps = {app.id: {"running": False, "mode": "stopped", "windows": []} for app in social_apps.APPS}
        apps["trilium"] = {"running": True, "mode": "windowed", "windows": [placement(51), placement(52)],
                           "_native_window_count": 3}
        captured = {"version": 7, "name": "current", "desktop": {"workspace_names": ORDER}, "sessions": [],
                    "terminals": [], "social_apps": apps}
        with patch.object(cli, "_capture_all", return_value=captured):
            self.assertEqual(self.apply("off", "Delta"), 0, self.err.getvalue())
        entry = self.archive("Delta")
        self.assertEqual(sorted(switch._native_ids(entry["snapshot"])), [51, 52])
        self.assertEqual(sorted(call.args[0] for call in self.close.call_args_list), [51, 52])
        self.assertEqual([item["id"] for item in self.windows], [53])
        self.assertEqual(entry["unknown"], [])

    def trilium_archive(self):
        self.windows = [window(51, 3, app="trilium", pid=5100)]
        monitor = {"connector": "HDMI-2", "serial": "display-1"}
        geometry = {"x": 0, "y": 0, "width": 800, "height": 600}
        apps = {app.id: {"running": False, "mode": "stopped", "windows": []} for app in social_apps.APPS}
        apps["trilium"] = {"running": True, "mode": "windowed", "_native_window_count": 1, "windows": [{
            "workspace": 3, "workspace_name": "Delta", "monitor": 0, "monitor_identity": monitor,
            "monitor_geometry": geometry, "geometry": geometry, "geometry_relative": geometry,
            "state": "normal", "_native_id": 51, "trilium_dir": "/work/notes"}]}
        captured = {"version": 7, "name": "current", "desktop": {"workspace_names": ORDER}, "sessions": [],
                    "terminals": [], "social_apps": apps}
        with patch.object(cli, "_capture_all", return_value=captured):
            self.assertEqual(self.apply("off", "Delta"), 0, self.err.getvalue())

    def test_a_trilium_that_cannot_be_restored_never_keeps_the_workspace_in_opening(self):
        self.trilium_archive()
        counts = {**dict.fromkeys(cli.CATEGORIES, 0), "codex_verified": 1}
        with patch.object(cli, "_restore", return_value=counts) as restore, \
             patch.object(social_apps, "restore_social_apps", side_effect=CommandError("no window appeared")) as soft:
            self.assertEqual(self.apply("on", "Delta"), 0, self.err.getvalue())
        self.assertEqual(self.archive("Delta")["phase"], "on")
        self.assertIn("Trilium Desktop was not restored: no window appeared", self.err.getvalue())
        restore.assert_not_called()  # nothing but Trilium was saved
        records = soft.call_args.args[0]
        self.assertEqual(records["trilium"]["windows"][0]["trilium_dir"], "/work/notes")
        self.assertEqual((soft.call_args.kwargs["workspace"], soft.call_args.kwargs["workspace_switch"]), ("Delta", True))

    def test_trilium_is_not_part_of_the_verified_restore(self):
        self.trilium_archive()
        seen = {}

        def restore(snapshot, args):
            seen["social"] = snapshot["social_apps"]["trilium"]
            return {**dict.fromkeys(cli.CATEGORIES, 0), "codex_verified": 1}
        self.windows.append(window(61, 3))
        with patch.object(cli, "_restore", side_effect=restore), patch.object(social_apps, "restore_social_apps"):
            self.assertEqual(self.apply("on", "Delta"), 0, self.err.getvalue())
        self.assertEqual(seen.get("social", {"windows": []})["windows"], [])

    def test_trilium_on_the_workspace_is_known_and_a_trilium_on_another_one_is_not_touched(self):
        self.windows = [window(51, 3, app="trilium", pid=5100), window(52, 1, app="trilium", pid=5200)]
        shell = self.shell()
        known, unknown, problems = switch._classify(cli, shell, shell["workspaces"][3])
        self.assertEqual((sorted(known), unknown, problems), ([51], {}, []))


if __name__ == "__main__":
    unittest.main()
