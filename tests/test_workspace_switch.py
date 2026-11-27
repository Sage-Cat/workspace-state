from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from workspace_state import cli, desktop, file_manager, operations, storage, workspace_switch as switch
from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderCount, ProviderItemResult

NAMES = ["Alpha", "Beta"]


def native(wid, workspace=0, *, pid=None):
    return {"id": wid, "pid": pid or wid * 100, "workspace": workspace, "sticky": False,
            "close_blocker": None, "monitor": 0, "state": "normal", "title": "Nemo"}


def desktop_state(*windows, epoch="current", dynamic=False, blockers=0):
    return {"available": True, "enable_epoch": epoch, "capabilities": ["close_window"],
            "dynamic_workspaces": dynamic, "screen_unavailable": False, "monitors": [],
            "workspaces": [{"index": index, "name": name, "named": True,
                            "blocking_windows": blockers if index == 0 else 0}
                           for index, name in enumerate(NAMES)],
            "windows": list(windows)}


def recipe(*windows):
    placements = [{"_native_id": wid, "locations": [f"file:///example/{wid}"], "active_tab": 0,
                   "placement": {"workspace": index, "workspace_name": NAMES[index],
                                 "monitor": 0, "state": "normal"}}
                  for wid, index in windows]
    return {"version": 7, "name": "current", "created_at": "fictional",
            "desktop": {"workspace_names": NAMES}, "sessions": [], "terminals": [],
            "file_manager": {"provider": "nemo", "windows": placements}}


class WorkspaceSwitchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(operations.bind, operations.current())
        operations.bind(None)
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"XDG_DATA_HOME": temp, "XDG_RUNTIME_DIR": temp}))
        self.enterContext(patch.object(cli, "_graphical_session_ending", return_value=None))
        self.capture = self.enterContext(patch.object(desktop, "capture_shell", side_effect=lambda: self.shell))
        self.close = self.enterContext(patch.object(desktop, "close_window"))
        self.enterContext(patch.object(file_manager, "verify_switch_close"))
        self.out = self.enterContext(redirect_stdout(io.StringIO()))
        self.err = self.enterContext(redirect_stderr(io.StringIO()))
        self.shell = desktop_state()

    def list_rows(self):
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(cli.main(["workspace", "list", "--json"]), 0)
        return json.loads(self.out.getvalue())

    def apply(self, action):
        return cli.main(["workspace", action, "--", "Alpha"])

    def archive(self):
        return json.loads(switch._path("Alpha").read_text())

    def test_committed_on_retries_only_the_outstanding_marker_cleanup(self):
        self.assertEqual(self.apply("off"), 0)
        entry = self.archive()
        cli._set_browser_state(entry["snapshot"], {"profiles": [{"profile": "Default", "windows": [
            {"id": "window-1", "_native_id": 71}]}]})
        entry["chrome_cleanup_pending"] = True
        switch._write(entry, "on")
        with patch("workspace_state.browser.cancel_workspace_window", side_effect=[
            RuntimeError("companion offline"), None]) as cleanup, patch.object(cli, "_restore") as restore:
            self.assertEqual(self.apply("on"), 3)
            self.assertEqual(self.archive()["phase"], "on")
            self.assertEqual(self.list_rows()["workspaces"][0]["target"], "on")
            self.assertEqual(self.apply("on"), 0)
            self.assertNotIn("chrome_cleanup_pending", self.archive())
            self.assertEqual(self.apply("on"), 0)
            self.assertEqual(cleanup.call_count, 2)
            cleanup.assert_called_with("Default", f"{entry['token']}:Default:window-1")
            restore.assert_not_called()

    def test_empty_workspace_switch_is_durable_and_serialized(self):
        self.assertEqual(self.apply("off"), 0)
        path = switch._path("Alpha")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        old = path.read_bytes()
        self.shell = desktop_state(epoch="new")
        self.assertEqual(self.list_rows()["workspaces"][0]["state"], "off")
        self.assertEqual(self.apply("off"), 0)
        self.assertEqual(path.read_bytes(), old)
        with switch._switch_lock():
            self.assertTrue(self.list_rows()["busy"])
            self.assertEqual(self.apply("on"), 2)
        self.assertEqual(self.apply("on"), 0)
        self.assertEqual(self.list_rows()["workspaces"][0]["state"], "on")
        self.close.assert_not_called()

    def test_inventory_refuses_unsupported_and_dialog_windows_before_closure(self):
        self.shell = desktop_state(native(14), blockers=1)
        self.assertEqual(self.apply("off"), 2)
        self.assertFalse(switch._path("Alpha").exists())
        self.shell = desktop_state({**native(14), "close_blocker": "unsupported_client"})
        self.assertEqual(self.apply("off"), 2)
        self.close.assert_not_called()

    def test_missed_native_window_fails_closed(self):
        self.shell = desktop_state(native(14))
        with patch.object(cli, "_capture_all", return_value=recipe()):
            self.assertEqual(self.apply("off"), 2)
        self.assertIn('window "Nemo" has no provider that can archive it', self.err.getvalue())
        self.assertFalse(switch._path("Alpha").exists())
        self.close.assert_not_called()

    def test_new_window_after_capture_leaves_archive_and_windows_untouched(self):
        self.shell = desktop_state(native(14))
        with patch.object(switch, "_snapshot", side_effect=self.add_window_after_capture):
            self.assertEqual(self.apply("off"), 2)
        self.assertIn("changed during capture", self.err.getvalue())
        self.assertFalse(switch._path("Alpha").exists())
        self.close.assert_not_called()

    def add_window_after_capture(self, *_):
        self.shell["windows"].append(native(15))
        return recipe((14, 0))

    def test_archive_precedes_close_and_shared_pid_remains(self):
        self.shell = desktop_state(native(14), native(15, 1, pid=1400))

        def close(wid, *, workspace, name, pid, epoch):
            self.assertEqual(self.archive()["phase"], "closing")
            self.assertEqual((wid, workspace, name, pid, epoch), (14, 0, "Alpha", 1400, "current"))
            self.shell["windows"].pop(0)

        self.close.side_effect = close
        with patch.object(switch, "_snapshot", return_value=recipe((14, 0))):
            self.assertEqual(self.apply("off"), 0)
        self.assertEqual(self.archive()["phase"], "off")
        self.assertEqual(switch._native_ids(self.archive()["snapshot"]), [14])
        self.assertEqual([win["id"] for win in self.shell["windows"]], [15])

    def test_partial_close_retry_preserves_complete_snapshot(self):
        self.shell = desktop_state(native(14), native(15))

        def close(wid, **_):
            if wid == 15:
                raise desktop.CommandError("client still open")
            self.shell["windows"].pop(0)

        self.close.side_effect = close
        with patch.object(switch, "_snapshot", side_effect=lambda _cli, _shell, _slot, windows:
                          recipe(*[(wid, 0) for wid in windows])):
            self.assertEqual(self.apply("off"), 3)
        self.assertEqual(self.list_rows()["workspaces"][0]["target"], "off")
        before = self.archive()
        self.close.side_effect = lambda *_a, **_k: self.shell["windows"].clear()
        with patch.object(switch, "_snapshot", return_value=recipe((15, 0))):
            self.assertEqual(self.apply("off"), 0)
        self.assertEqual(self.archive()["snapshot"], before["snapshot"])
        self.assertEqual(self.archive()["token"], before["token"])

    def test_partial_on_requires_positive_evidence_and_keeps_archive(self):
        self.shell = desktop_state(native(14))
        self.close.side_effect = lambda *_a, **_k: self.shell["windows"].clear()
        with patch.object(switch, "_snapshot", return_value=recipe((14, 0))):
            self.assertEqual(self.apply("off"), 0)
        before = self.archive()
        self.shell = desktop_state(epoch="after-login")
        counts = {**dict.fromkeys(cli.CATEGORIES, 0), "codex_verified": 1}
        with patch.object(cli, "_restore", return_value=counts):
            self.assertEqual(self.apply("on"), 3)
        self.assertEqual(self.archive()["snapshot"], before["snapshot"])
        self.assertEqual(self.list_rows()["workspaces"][0]["target"], "on")

        def restore(snapshot, args):
            self.assertEqual(switch._native_ids(snapshot), [14])
            self.assertTrue(args.workspace_switch)
            self.assertEqual(args.switch_token, before["token"])
            self.shell["windows"].append(native(24))
            verified = PhaseEvidence(EvidenceState.VERIFIED)
            evidence = ProviderItemResult("nemo", "folder", verified, verified, verified, native_window_id=24)
            return {**counts, "file-manager": ProviderCount(1, [evidence])}

        observed = recipe((24, 0))
        with patch.object(cli, "_restore", side_effect=restore), \
             patch.object(switch, "_snapshot", return_value=observed), patch.object(storage, "save") as save:
            # Another window on the selected slot does not verify a restored
            # provider's content, even with a successful provider report.
            self.assertEqual(self.apply("on"), 3)
            save.assert_not_called()
            observed["file_manager"]["windows"][0]["locations"] = ["file:///example/14"]
            self.shell["windows"].clear()
            self.assertEqual(self.apply("on"), 0)
            self.assertNotIn("_native_id", save.call_args.args[0]["file_manager"]["windows"][0])
        self.assertEqual(self.archive()["phase"], "on")

    def test_close_refuses_a_background_tab_change_after_archive(self):
        self.shell = desktop_state(native(14))
        original, changed = recipe((14, 0)), recipe((14, 0))
        changed["file_manager"]["windows"][0]["locations"].append("file:///example/new-background-tab")
        with patch.object(switch, "_snapshot", side_effect=[original, changed]):
            self.assertEqual(self.apply("off"), 3)
        self.close.assert_not_called()
        self.assertEqual(self.archive()["snapshot"], original)

    def test_same_recipe_on_wrong_display_or_surviving_original_is_not_a_second_match(self):
        saved = recipe((14, 0))
        observed = recipe((24, 0))
        observed["file_manager"]["windows"][0]["locations"] = ["file:///example/14"]
        shell = desktop_state({**native(24), "monitor": 1})
        with self.assertRaisesRegex(desktop.CommandError, "saved physical placement"):
            switch._verified_windows(saved, observed, shell, set(), {24})
        shell["windows"][0]["monitor"] = 0
        with self.assertRaisesRegex(desktop.CommandError, "saved physical placement"):
            switch._verified_windows(saved, observed, shell, {24}, {24})
        with self.assertRaisesRegex(desktop.CommandError, "saved physical placement"):
            switch._verified_windows(saved, observed, shell, set(), {25})
        self.assertEqual(switch._verified_windows(saved, observed, shell, set(), {24}), {24})

    def test_canonical_merge_keeps_other_windows_of_the_same_app(self):
        def social_recipe(index):
            saved = recipe()
            saved["social_apps"] = {"chat": {"running": True, "mode": "windowed",
                                             "windows": [{"workspace": index, "workspace_name": NAMES[index]}]}}
            return saved
        with patch.object(storage, "load", return_value=social_recipe(1)), \
             patch.object(storage, "save") as save:
            switch._commit_enabled(cli, {"name": "Alpha"}, desktop_state(), social_recipe(0))
        self.assertEqual(save.call_args.args[0]["social_apps"]["chat"]["windows"],
                         [{"workspace": 1, "workspace_name": "Beta"},
                          {"workspace": 0, "workspace_name": "Alpha"}])

    def test_chrome_recipe_tracks_group_membership(self):
        snapshot = recipe()
        window = {"_native_id": 21, "type": "normal", "incognito": False, "workspace_index": 0, "workspace": "Alpha",
                  "monitor": {"index": 0}, "tabs": [
                      {"url": "https://example.test/one", "pinned": False, "group": "group"},
                      {"url": "https://example.test/two", "pinned": False},
                  ], "groups": [{"id": "group", "title": "Example", "color": "blue", "collapsed": False}]}
        cli._set_browser_state(snapshot, {"profiles": [{"profile": "Default", "windows": [window]}]})
        archived = switch._recipe(snapshot)
        window["tabs"][0].pop("group")
        window["tabs"][1]["group"] = "group"
        self.assertNotEqual(switch._recipe(snapshot), archived)

    def test_tmux_retention_resolves_inherited_exact_session_options(self):
        from workspace_state import restore, util
        identity = {"server_pid": "100", "server_start_tick": "200", "session_id": "$1", "boot_id": "test"}
        with patch.object(restore, "tmux_runtime_identity", return_value=identity), \
             patch.object(util, "run", side_effect=["off", "off"]) as run:
            switch._verify_tmux_retention({"sessions": [{"name": "demo", "tmux_identity": identity}]})
        self.assertEqual(run.call_args.args[0], ["tmux", "show-options", "-Av", "-t", "=demo:", "destroy-unattached"])

    def test_login_projection_keeps_shared_terminal_and_other_windows(self):
        self.assertEqual(self.apply("off"), 0)
        saved = recipe((14, 0), (15, 1))
        saved["sessions"] = [{"name": name} for name in ("shared", "hidden", "detached")]
        saved["terminals"] = [{"session": name, "placement": {"workspace": workspace}}
                              for name, workspace in (("shared", 0), ("shared", 1), ("hidden", 0))]
        original = copy.deepcopy(saved)
        filtered = switch.suppress(saved)
        self.assertEqual(saved, original)
        self.assertEqual([item["name"] for item in filtered["sessions"]], ["shared", "detached"])
        self.assertEqual(filtered["terminals"], [saved["terminals"][1]])
        self.assertEqual(filtered["file_manager"]["windows"], [saved["file_manager"]["windows"][1]])

    def test_parked_profiles_skip_native_session_replay_after_last_window_closes(self):
        self.assertEqual(self.apply("off"), 0)
        entry = self.archive()
        entry["snapshot"]["vscode"] = {"windows": [{"user_data_dir": "/example/editor"}]}
        cli._set_browser_state(entry["snapshot"], {"profiles": [
            {"profile": "Default", "windows": [{"id": "one"}]}]})
        for phase in ("off", "closing", "opening"):
            with self.subTest(phase=phase):
                switch._write(entry, phase)
                projected = switch.suppress(recipe())
                self.assertEqual(projected["_workspace_switch_cold_profiles"], {"Default"})
                self.assertEqual(projected["_workspace_switch_blocked_vscode_scopes"], {"/example/editor"})
                expected = set() if phase == "off" else {"Default"}
                self.assertEqual(projected["_workspace_switch_blocked_profiles"], expected)

    def test_every_blocking_window_is_named_with_its_workspace(self):
        minimized = {**native(14), "state": "minimized", "close_blocker": "unsupported_client", "title": "Legacy tool"}
        dialog = {**native(15), "close_blocker": "transient", "title": "Open file"}
        self.shell = desktop_state(minimized, dialog, native(16), blockers=2)
        self.assertEqual(self.apply("off"), 2)
        message = self.err.getvalue()
        self.assertIn('Alpha: 2 window(s) GNOME cannot list', message)
        self.assertIn('Alpha: minimized window "Legacy tool" is an X11 client', message)
        self.assertIn('Alpha: window "Open file" is a dialog of another window', message)
        self.assertLessEqual(len(message.strip()), 240)  # the whole stderr line, as the menu shows it
        self.assertFalse(switch._path("Alpha").exists())
        self.close.assert_not_called()

    def test_every_provider_problem_is_listed_with_its_workspace(self):
        self.shell = desktop_state({**native(14), "app_id": "nemo"})
        with patch.object(cli, "_capture_all", return_value=recipe((14, 0))), \
             patch.object(cli, "_category_problems", return_value={"terminals": [], "file-manager": ["first fault", "second fault"]}):
            self.assertEqual(self.apply("off"), 2)
        self.assertIn("Alpha: first fault; Alpha: second fault", self.err.getvalue())

    def test_a_workspace_name_with_spaces_is_quoted_in_the_forget_hint(self):
        self.assertIn("-- 'Deep Work'", str(switch._unreadable_error("Deep Work")))

    def test_summary_lists_problems_within_the_menu_limit(self):
        self.assertEqual(switch._summary(["a", "b", "a"]), "a; b")
        many = [f"Alpha: Code project-{index} unsaved tab file-{index}.txt" for index in range(12)]
        text = switch._summary(many)
        self.assertLessEqual(len("wsctl: " + text), 240)
        self.assertTrue(text.startswith(many[0]))
        self.assertRegex(text, r" \+\d+ more$")
        shown = text.rsplit(" +", 1)[0].count("; ") + 1
        self.assertEqual(text.rsplit(" +", 1)[1], f"{len(many) - shown} more")
        single = switch._summary(["x" * 400, "second"])
        self.assertLessEqual(len(single), switch.MESSAGE_LIMIT)
        self.assertTrue(single.endswith(" +1 more"))

    def test_chrome_pages_that_open_by_url_do_not_block_but_others_are_named(self):
        for url in ("https://example.test/", "http://example.test/", "file:///example/a.html", "about:blank",
                    "chrome://newtab/", "chrome://settings/", "chrome://extensions", "chrome://history/"):
            self.assertTrue(switch._chrome_reopenable(url), url)
        for url in ("data:text/html,x", "blob:https://example.test/id", "devtools://devtools/bundled/x.html",
                    "view-source:https://example.test/", "chrome://crash", "chrome://kill/", "chrome://quit",
                    "chrome-extension://abc/page.html", "chrome://version/#wsctl-workspace-abc", "javascript:alert(1)"):
            self.assertFalse(switch._chrome_reopenable(url), url)

        def window(*urls):
            return {"id": "w", "type": "normal", "incognito": False, "workspace": "Alpha",
                    "tabs": [{"url": url} for url in urls]}

        scoped = {"browsers": {}, "sessions": []}
        with patch.object(cli, "_browser_state", return_value={"profiles": [
                {"profile": "Default", "windows": [window("chrome://settings/", "data:text/html,x", "blob:x")]}]}), \
             patch("workspace_state.browser.browser_companion_info", return_value={"capabilities": ["workspace_switch"]}), \
             patch("workspace_state.browser.browser_windows",
                   side_effect=lambda state: [(p, w) for p in state["profiles"] for w in p["windows"]]):
            with self.assertRaises(switch.SwitchBlocked) as caught:
                switch._verify_close_recipes(cli, {}, scoped)
        self.assertEqual(len(caught.exception.problems), 1)
        self.assertIn("cannot be reopened by URL: blob:, data:", caught.exception.problems[0])
        self.assertNotIn("chrome", caught.exception.problems[0].split("URL:")[1])

    def vscode_window(self, **fields):
        return {"kind": "folder", "folders": [{"uri": "file:///example/shop", "name": "shop"}],
                "workspace_file": None, "placement": {"workspace_name": "Alpha"}, "editor_uris": [],
                "dirty_count": 0, "_supports_workspace_switch": True, **fields}

    def test_vscode_policy_blocks_only_unsaved_untitled_and_running_terminals(self):
        ok = self.vscode_window(_unsupported_editors=None, _switch_blockers=[])
        self.assertEqual(switch._vscode_problems(ok), [])
        blocked = self.vscode_window(_unsupported_editors=None, _switch_blockers=[
            {"kind": "unsaved", "label": "a.py", "reason": ""},
            {"kind": "untitled", "label": "Untitled-1", "reason": ""},
            {"kind": "terminal", "label": "bash", "reason": "command running"},
            {"kind": "terminal", "label": "task", "reason": "activity unverified"}])
        self.assertEqual(switch._vscode_problems(blocked), [
            "Alpha: Code shop unsaved tab a.py", "Alpha: Code shop untitled tab with content Untitled-1",
            "Alpha: Code shop terminal bash (command running)", "Alpha: Code shop terminal task (activity unverified)"])

    def test_vscode_companion_without_the_policy_keeps_the_old_rule(self):
        old = self.vscode_window(_unsupported_editors=0, _switch_blockers=None)
        self.assertEqual(switch._vscode_problems(old), [])
        old["_unsupported_editors"] = 2
        old["dirty_count"] = 1
        self.assertEqual(len(switch._vscode_problems(old)), 2)
        stale = self.vscode_window(_supports_workspace_switch=False)
        self.assertIn("Reload Window", switch._vscode_problems(stale)[0])

    def test_content_identity_ignores_blockers_and_the_legacy_count_but_not_text_files(self):
        def key(**fields):
            item = {"_native_id": 3, "placement": {}, "editor_uris": ["file:///example/a.md"], "dirty_count": 0,
                    "user_data_dir": "/example", "kind": "folder", "folders": [{"uri": "file:///example", "name": "example"}],
                    "workspace_file": None, "profile": {"id": "default"}, "remote_name": None, **fields}
            return next(iter(switch._window_recipes({"terminals": [], "vscode": {"windows": [item]}})))[1]
        # An archive written before the switch policy, a current archive, and a window with a revived terminal.
        old = key(_unsupported_editors=0)
        current = key(_unsupported_editors=None, _switch_blockers=[], _text_editor_uris=["file:///example/a.md"])
        revived = key(_unsupported_editors=None, _text_editor_uris=["file:///example/a.md"],
                      _switch_blockers=[{"kind": "terminal", "label": "zsh", "reason": "activity unverified"}])
        self.assertEqual(old, current)
        self.assertEqual(current, revived)
        # Diff operands and untitled tabs do not count; the text files do.
        self.assertEqual(current, key(editor_uris=["file:///example/a.md", "untitled:Untitled-1"],
                                      _text_editor_uris=["file:///example/a.md"]))
        self.assertNotEqual(current, key(_text_editor_uris=[]))

    def test_on_verifies_a_window_restored_from_an_older_archive_with_a_current_companion(self):
        from workspace_state import provider_results

        def window(native_id, **fields):
            base = {"_native_id": native_id, "placement": {"workspace_name": "Alpha", "workspace": 0},
                    "editor_uris": ["file:///example/a.md"], "dirty_count": 0, "user_data_dir": "/example",
                    "kind": "folder", "folders": [{"uri": "file:///example", "name": "example"}],
                    "workspace_file": None, "profile": {"id": "default"}, "remote_name": None, **fields}
            return {"terminals": [], "sessions": [], "vscode": {"windows": [base]}}

        shell = {"windows": [{"id": 77, "workspace": 0}]}
        live = window(77, _unsupported_editors=None, _text_editor_uris=["file:///example/a.md"],
                      _switch_blockers=[{"kind": "terminal", "label": "zsh", "reason": "activity unverified"}])
        with patch.object(desktop, "remap_workspace", side_effect=lambda placement, shell=None: placement), \
             patch.object(desktop, "remap_monitor", side_effect=lambda placement, shell=None: placement), \
             patch.object(provider_results, "placement_frame_matches", return_value=True):
            self.assertEqual(switch._verified_windows(window(10, _unsupported_editors=0), live, shell, set(), {77}), {77})

    def test_forget_with_confirmation_deletes_an_archive_whatever_its_content(self):
        switch._directory().mkdir(mode=0o700, parents=True)
        entry = {"version": 1, "name": "Alpha", "phase": "off", "epoch": "e", "token": "t", "originals": {},
                 "snapshot": {"vscode": "not-a-record"}}
        switch._path("Alpha").write_text(json.dumps(entry))
        self.assertEqual(cli.main(["workspace", "forget", "--", "Alpha"]), 2)
        self.assertIn("--yes", self.err.getvalue())
        self.assertNotIn("Traceback", self.err.getvalue())
        self.assertTrue(switch._path("Alpha").exists())
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Alpha"]), 0)
        self.assertFalse(switch._path("Alpha").exists())

    def test_message_length_counts_utf16_units_like_the_menu(self):
        problems = [f"Alpha: window \"{'\U0001F600' * 12}\" has no valid process ID" for _ in range(1)]
        problems += [f"Alpha: window \"{'\U0001F600' * 12}{n}\" is not a regular window" for n in range(8)]
        text = switch._summary(problems)
        self.assertLessEqual(switch._units("wsctl: " + text), 240)
        self.assertGreater(len("wsctl: " + text), 0)
        self.assertRegex(text, r"\+\d+ more$")
        clipped = switch._summary(["\U0001F600" * 400, "second"])
        self.assertLessEqual(switch._units("wsctl: " + clipped), 240)
        self.assertTrue(clipped.endswith("\u2026 +1 more"))

    def test_unreadable_archive_pauses_only_its_own_workspace(self):
        directory = switch._directory()
        directory.mkdir(mode=0o700, parents=True)
        switch._path("Alpha").write_text("{broken")
        rows = {row["name"]: row for row in self.list_rows()["workspaces"]}
        self.assertEqual(rows["Alpha"]["state"], "partial")
        self.assertIn("workspace Alpha is unreadable", rows["Alpha"]["detail"])
        self.assertIn("wsctl workspace forget --yes -- Alpha", rows["Alpha"]["detail"])
        self.assertEqual(rows["Beta"]["state"], "on")
        for action in ("off", "on"):
            self.err.seek(0)
            self.err.truncate()
            self.assertEqual(self.apply(action), 2)
            self.assertIn("forget --yes -- Alpha", self.err.getvalue())
        # Ordinary restore keeps Beta's windows and hides only Alpha's.
        projected = switch.suppress(recipe((14, 0), (15, 1)))
        self.assertEqual([item["_native_id"] for item in projected["file_manager"]["windows"]], [15])
        self.assertEqual(cli.main(["workspace", "off", "--", "Beta"]), 0)
        self.assertEqual(switch._path("Alpha").read_text(), "{broken")

    def test_forget_requires_confirmation_and_removes_one_archive(self):
        self.assertEqual(self.apply("off"), 0)
        self.assertEqual(cli.main(["workspace", "off", "--", "Beta"]), 0)
        self.assertEqual(cli.main(["workspace", "forget", "--", "Alpha"]), 2)
        self.assertIn("--yes", self.err.getvalue())
        self.assertTrue(switch._path("Alpha").exists())
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Alpha"]), 0)
        self.assertFalse(switch._path("Alpha").exists())
        self.assertTrue(switch._path("Beta").exists())
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "Alpha"]), 2)
        self.assertIn("has no switch archive", self.err.getvalue())

    def test_forget_needs_neither_a_readable_archive_nor_a_gnome_slot(self):
        switch._directory().mkdir(mode=0o700, parents=True)
        switch._path("Renamed").write_text("not json")
        self.shell = desktop_state(dynamic=True)
        self.assertEqual(cli.main(["workspace", "forget", "--yes", "--", "renamed"]), 0)
        self.assertFalse(switch._path("Renamed").exists())

    def test_enabled_workspace_windows_restart_with_chrome_native_session(self):
        self.assertEqual(self.apply("off"), 0)
        entry = self.archive()
        cli._set_browser_state(entry["snapshot"], {"profiles": [
            {"profile": "Default", "windows": [{"id": "parked"}]},
            {"profile": "Spare", "windows": [{"id": "parked-spare"}]}]})
        live = recipe()
        cli._set_browser_state(live, {"profiles": [
            {"profile": "Default", "windows": [{"id": "live", "workspace": "Beta", "workspace_index": 1}]}]})
        for phase, cold, blocked in (("off", {"Spare"}, set()), ("closing", {"Default", "Spare"}, {"Default", "Spare"})):
            with self.subTest(phase=phase):
                switch._write(entry, phase)
                projected = switch.suppress(copy.deepcopy(live))
                self.assertEqual(projected["_workspace_switch_cold_profiles"], cold)
                self.assertEqual(projected["_workspace_switch_blocked_profiles"], blocked)


    def test_dynamic_duplicate_and_default_slots_are_never_archived(self):
        self.shell = desktop_state(dynamic=True)
        self.assertEqual(self.list_rows()["workspaces"], [])
        self.assertEqual(self.apply("off"), 2)
        self.shell["dynamic_workspaces"] = False
        self.shell["workspaces"][1]["name"] = "alpha"
        self.assertEqual(self.list_rows()["workspaces"], [])
        self.shell["workspaces"][1].update(name="Workspace 2", named=False)
        self.assertEqual([row["name"] for row in self.list_rows()["workspaces"]], ["Alpha"])
        self.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
