import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from workspace_state import vscode
from workspace_state.provider_results import ProviderRestoreError

from test_vscode import PLACEMENT, project, record

FOLDER = "file:///home/example/Projects/demo"
SAVED = ["file:///home/example/Projects/demo/a.md", "file:///home/example/Projects/demo/b.md"]


def live_window(item, instance, wid, *, texts=SAVED, markers=()):
    return {**item, "instance": instance, "endpoint": Path(f"/tmp/{instance}.sock"),
            "shell": {"id": wid, "workspace": 0, "monitor": 0, "state": "normal", "geometry": PLACEMENT["geometry"]},
            "unsupported_editors": 0, "switch_blockers": [], "text_editor_uris": list(texts),
            "capabilities": ["workspace_switch", "switch_policy"], "workspace_launch_markers": list(markers),
            "editor_uris": list(texts)}


def saved(**kwargs):
    item = {**project(**kwargs), "_native_id": 7, "_text_editor_uris": list(SAVED), "editor_uris": list(SAVED)}
    return item


class NativeFolderSwitchTests(unittest.TestCase):
    def switch(self, item, before, after, *, settle=0, request=None, texts_after=SAVED, records=None):
        """Run a workspace-switch On; `after` are the windows that exist once Code has been launched."""
        if records is None:
            records = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, records, True)
        state = {"launched": False}

        def get(deadline, *, strict=True):
            return after if state["launched"] else before

        def launch(argv, purpose):
            state["launched"] = True

        shell = {"available": True}
        fake_request = request or (lambda endpoint, method, *a, **k: (
            {"ready": True} if method == "probe" else
            next(w for w in after if w["endpoint"] == endpoint)))
        with patch.object(vscode, "_marker_directory", return_value=Path(records)), \
             patch.object(vscode, "_wait_for_live_windows", return_value=before), \
             patch.object(vscode, "_LiveWindows") as lw, patch.object(vscode, "_target", return_value=PLACEMENT), \
             patch.object(vscode, "_place"), patch.object(vscode, "_request", side_effect=fake_request), \
             patch.object(vscode, "_verify_profile"), patch.object(vscode, "_check_resource"), \
             patch.object(vscode, "_verify_scoped_cold_start"), \
             patch.object(vscode, "capture_shell", return_value=shell), \
             patch.object(vscode, "matching_windows", side_effect=lambda _shell: [
                 w["shell"] | {"id": w["shell"]["id"]} for w in (after if state["launched"] else before)]), \
             patch.object(vscode, "NATIVE_EDITOR_SETTLE", settle, create=True), \
             patch.object(vscode, "launch_graphical_service", side_effect=launch) as launch_mock:
            lw.return_value.get.side_effect = get
            try:
                count = vscode.restore_vscode(record(item), no_place=True, workspace_switch=True,
                                              switch_token="scoped", timeout=2)
                error = None
            except ProviderRestoreError as caught:
                count, error = None, caught
        return count, error, launch_mock

    def test_a_folder_window_is_launched_without_a_marker_file(self):
        item = saved()
        new = live_window(item, "new", 21)
        count, error, launch = self.switch(item, [], [new])
        self.assertEqual((count, error), (1, None))
        argv, purpose = launch.call_args.args
        self.assertEqual(purpose, "vscode-project")
        self.assertEqual(argv[argv.index("--folder-uri") + 1], FOLDER)
        self.assertIn("--new-window", argv)
        self.assertNotIn("--file-uri", argv)

    def test_only_a_window_that_was_absent_before_the_launch_is_accepted(self):
        item = saved()
        unrelated = live_window(project(uri="file:///home/example/Projects/other"), "old", 5)
        new = live_window(item, "new", 21)
        count, error, launch = self.switch(item, [unrelated], [unrelated, new])
        self.assertEqual(count, 1)
        self.assertEqual(launch.call_count, 1)

    def test_an_already_open_folder_refuses_the_launch_with_attention(self):
        item = saved()
        other_workspace = live_window(item, "open", 5, texts=[])
        other_workspace["shell"] = {**other_workspace["shell"], "workspace": 2}
        count, error, launch = self.switch(item, [other_workspace], [other_workspace])
        launch.assert_not_called()
        result = error.results[0]
        self.assertIn("already shows this folder", result.attention[0])
        self.assertIn("already shows this folder", str(error))

    def test_a_window_without_a_folder_keeps_the_marker(self):
        item = {**saved(kind="empty"), "editor_uris": [], "_text_editor_uris": []}
        with patch.object(vscode, "_marker_directory", return_value=Path("/nonexistent-marker-dir")), \
             patch.object(vscode.os, "open"), patch.object(vscode.os, "close"), \
             patch.object(Path, "mkdir"), patch.object(Path, "chmod"):
            _count, _error, launch = self.switch(item, [], [], records=Path("/nonexistent-marker-dir"))
        argv = launch.call_args.args[0]
        self.assertEqual(argv.count("--file-uri"), 1)
        self.assertTrue(argv[argv.index("--file-uri") + 1].startswith("file:///nonexistent-marker-dir/vscode-"))

    def test_a_code_workspace_still_launches_its_file_with_the_marker(self):
        item = {**saved(kind="workspace", workspace_file="file:///home/example/w.code-workspace"),
                "editor_uris": [], "_text_editor_uris": []}
        with patch.object(vscode, "_marker_directory", return_value=Path("/nonexistent-marker-dir")), \
             patch.object(vscode.os, "open"), patch.object(vscode.os, "close"), \
             patch.object(Path, "mkdir"), patch.object(Path, "chmod"):
            _count, _error, launch = self.switch(item, [], [], records=Path("/nonexistent-marker-dir"))
        argv = launch.call_args.args[0]
        self.assertEqual(argv.count("--file-uri"), 2)
        self.assertEqual(argv[argv.index("--file-uri") + 1], "file:///home/example/w.code-workspace")

    def test_login_never_adds_a_marker(self):
        item = saved()
        new = live_window(item, "new", 21)
        state = {"launched": False}
        with patch.object(vscode, "_wait_for_live_windows", side_effect=lambda *a, **k: [new] if state["launched"] else []), \
             patch.object(vscode, "_LiveWindows") as lw, patch.object(vscode, "_target", return_value=PLACEMENT), \
             patch.object(vscode, "_place"), patch.object(vscode, "_verify_profile"), patch.object(vscode, "_check_resource"), \
             patch.object(vscode, "_request", side_effect=lambda e, m, *a, **k: {"ready": True} if m == "probe" else new), \
             patch.object(vscode, "launch_graphical_service", side_effect=lambda *a: state.update(launched=True)) as launch:
            lw.return_value.get.return_value = [new]
            vscode.restore_vscode(record(item), no_place=True, timeout=2)
        for call in launch.call_args_list:
            self.assertNotIn("--file-uri", call.args[0])

    def test_editors_restored_by_code_are_not_reopened(self):
        item = saved()
        new = live_window(item, "new", 21)
        methods = []

        def request(endpoint, method, *a, **k):
            methods.append(method)
            return {"ready": True} if method == "probe" else new

        count, error, _launch = self.switch(item, [], [new], request=request)
        self.assertEqual(count, 1)
        self.assertNotIn("restore-editors", methods)
        self.assertNotIn("drop-launch-marker", methods)

    def test_missing_text_files_wait_for_code_before_the_companion_reopens_them(self):
        item = saved()
        partial = live_window(item, "new", 21, texts=SAVED[:1])
        complete = live_window(item, "new", 21)
        answers = iter([partial, partial, complete, complete, complete])
        methods = []

        def request(endpoint, method, *a, **k):
            methods.append(method)
            return {"ready": True} if method == "probe" else next(answers, complete)

        count, error, _launch = self.switch(item, [], [partial], request=request, settle=0.5)
        self.assertEqual(count, 1)
        # Code finished its own restore while the wait lasted, so nothing was opened twice.
        self.assertNotIn("restore-editors", methods)

    def test_missing_text_files_are_reopened_once_code_has_settled(self):
        item = saved()
        partial = live_window(item, "new", 21, texts=SAVED[:1])
        complete = live_window(item, "new", 21)
        methods = []

        def request(endpoint, method, *a, **k):
            methods.append(method)
            if method == "probe":
                return {"ready": True}
            return complete if "restore-editors" in methods else partial

        count, error, _launch = self.switch(item, [], [partial], request=request, settle=0.3)
        self.assertEqual(count, 1)
        self.assertEqual(methods.count("restore-editors"), 1)


class Harness:
    """One workspace-switch On over fake companions; `before` is what they report until Code is launched."""

    def drive(self, item, before, after=None, *, shell_before=None, shell_available=True, timeout=2, directory=None):
        after = before if after is None else after
        state = {"launched": False}
        place, launch = Mock(), Mock(side_effect=lambda *a: state.update(launched=True))
        windows = lambda: after if state["launched"] else before

        def request(endpoint, method, *a, **k):
            if method == "probe":
                return {"ready": True}
            window = next(w for w in after if w["endpoint"] == endpoint)
            if method == "restore-editors":
                window["text_editor_uris"] = window["editor_uris"] = list(SAVED)
            return window

        def shell(timeout=None):
            current = shell_before if shell_before is not None and not state["launched"] else windows()
            return {"available": shell_available, "windows": [w["shell"] for w in current]}

        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(vscode, "_marker_directory", return_value=Path(directory or temporary)), \
             patch.object(vscode, "_wait_for_live_windows", side_effect=lambda *a, **k: windows()), \
             patch.object(vscode, "_LiveWindows") as lw, patch.object(vscode, "_target", return_value=PLACEMENT), \
             patch.object(vscode, "_place", place), patch.object(vscode, "_request", side_effect=request), \
             patch.object(vscode, "_verify_profile"), patch.object(vscode, "_check_resource"), \
             patch.object(vscode, "_verify_scoped_cold_start"), patch.object(vscode, "NATIVE_EDITOR_SETTLE", 0, create=True), \
             patch.object(vscode, "capture_shell", side_effect=shell), \
             patch.object(vscode, "matching_windows", side_effect=lambda s: list(s.get("windows", []))), \
             patch.object(vscode, "launch_graphical_service", launch):
            lw.return_value.get.side_effect = lambda deadline, strict=True: windows()
            try:
                count, error = vscode.restore_vscode(record(item), no_place=False, workspace_switch=True,
                                                     switch_token="scoped", timeout=timeout), None
            except ProviderRestoreError as caught:
                count, error = None, caught
        return count, error, launch, place


class NativeFolderSafetyTests(unittest.TestCase, Harness):
    def test_a_window_that_existed_before_the_launch_is_never_adopted(self):
        item = saved()
        foreign = live_window(item, "foreign", 30)
        foreign["shell"] = {**foreign["shell"], "workspace": 1}
        # The companion was not ready before the launch, but GNOME already listed the window.
        count, error, launch, place = self.drive(item, [], [foreign], shell_before=[foreign])
        launch.assert_called_once()
        place.assert_not_called()
        self.assertIsNone(error.results[0].native_window_id)
        self.assertIn("did not acknowledge the exact project/profile", str(error))

    def test_an_unavailable_window_list_prevents_the_launch(self):
        item = saved()
        count, error, launch, place = self.drive(item, [], [live_window(item, "new", 21)], shell_available=False)
        launch.assert_not_called()
        place.assert_not_called()
        self.assertIn("GNOME window state is unavailable", error.results[0].attention[0])

    def test_a_foreign_window_with_the_folder_on_the_target_desk_only_asks_to_close_it(self):
        item = saved()
        foreign = live_window(item, "foreign", 30, texts=SAVED[:1])
        count, error, launch, place = self.drive(item, [foreign])
        launch.assert_not_called()
        place.assert_not_called()
        self.assertIsNone(error.results[0].native_window_id)
        self.assertNotIn("move it", error.results[0].attention[0])
        self.assertIn("close that window", error.results[0].attention[0])

    def test_a_foreign_window_on_another_desk_is_refused_with_the_reason(self):
        item = saved()
        foreign = live_window(item, "foreign", 30)
        foreign["shell"] = {**foreign["shell"], "workspace": 1}
        count, error, launch, place = self.drive(item, [foreign])
        launch.assert_not_called()
        place.assert_not_called()
        self.assertIsNone(error.results[0].native_window_id)
        self.assertIn("move it to this workspace", error.results[0].attention[0])


class NativeFolderRetryTests(unittest.TestCase, Harness):
    """An On that launched the window but did not finish is retried with the same switch token."""

    def retry(self, window):
        with tempfile.TemporaryDirectory() as directory:
            item = saved()
            with patch.object(vscode, "_marker_directory", return_value=Path(directory)):
                vscode._write_launch_record(vscode._launch_record("scoped", 7), window["shell"]["id"], window["instance"])
                result = self.drive(item, [window], directory=directory)
                return result, vscode._launch_record("scoped", 7).exists()

    def test_the_recorded_window_on_another_desk_is_adopted_and_placed(self):
        w = live_window(saved(), "w", 21)
        w["shell"] = {**w["shell"], "workspace": 1}
        (count, error, launch, place), kept = self.retry(w)
        self.assertEqual((count, error), (1, None))
        launch.assert_not_called()
        self.assertEqual([call.args[0] for call in place.call_args_list], [21])
        self.assertFalse(kept)

    def test_the_recorded_window_with_a_missing_tab_is_adopted_and_completed_once(self):
        w = live_window(saved(), "w", 21, texts=SAVED[:1])
        (count, error, launch, place), kept = self.retry(w)
        self.assertEqual((count, error), (1, None))
        launch.assert_not_called()
        self.assertFalse(kept)

    def test_a_window_with_the_folder_that_is_not_recorded_is_refused(self):
        item = saved()
        w = live_window(item, "w", 21, texts=SAVED[:1])
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(vscode, "_marker_directory", return_value=Path(directory)):
                vscode._write_launch_record(vscode._launch_record("scoped", 7), 99, "w")
            count, error, launch, place = self.drive(item, [w], directory=directory)
        launch.assert_not_called()
        place.assert_not_called()
        self.assertIn("already shows this folder", error.results[0].attention[0])

    def test_a_stale_record_with_the_same_window_id_but_another_companion_is_refused(self):
        item = saved()
        w = live_window(item, "new-companion", 21)
        w["shell"] = {**w["shell"], "workspace": 1}
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(vscode, "_marker_directory", return_value=Path(directory)):
                vscode._write_launch_record(vscode._launch_record("scoped", 7), 21, "old-companion")
            count, error, launch, place = self.drive(item, [w], directory=directory)
        launch.assert_not_called()
        place.assert_not_called()
        self.assertIsNone(error.results[0].native_window_id)
        self.assertIn("already shows this folder", error.results[0].attention[0])

    def test_a_launch_records_its_window_and_a_failed_slot_keeps_the_record(self):
        item = saved()
        new = live_window(item, "new", 21, texts=SAVED[:1])
        new["capabilities"] = []  # the companion cannot reopen the missing tab, so the slot fails
        with tempfile.TemporaryDirectory() as directory:
            count, error, launch, place = self.drive(item, [], [new], directory=directory)
            self.assertIsNotNone(error)
            with patch.object(vscode, "_marker_directory", return_value=Path(directory)):
                self.assertEqual(vscode._read_launch_record(vscode._launch_record("scoped", 7)), (21, "new"))


if __name__ == "__main__":
    unittest.main()
