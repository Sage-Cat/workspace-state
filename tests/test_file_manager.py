from __future__ import annotations

import subprocess
import unittest
from unittest.mock import Mock, call, patch

from workspace_state import browser, desktop, file_manager
from workspace_state.provider_results import EvidenceState, ProviderRestoreError
from workspace_state.util import CommandError


def placement(**overrides):
    value = {
        "workspace_name": "Work",
        "workspace": 1,
        "monitor_intent": {"edid_hash": "display-a"},
        "state": "normal",
        "geometry": {"x": 10, "y": 20, "width": 800, "height": 600},
        "geometry_relative": {"x": 10, "y": 20, "width": 800, "height": 600},
    }
    value.update(overrides)
    return value


def record(*windows):
    return {"provider": "nemo", "desktop_id": "nemo.desktop", "windows": list(windows)}


def saved(uri="file:///home/example/Documents", *, active_tab=0, **extra):
    value = {"locations": [uri], "active_tab": active_tab, "placement": placement()}
    value.update(extra)
    return value


class FileManagerTests(unittest.TestCase):
    def test_locked_placement_keeps_one_final_owned_handoff(self):
        target = placement()
        shell = {'available': True, 'active_workspace': 0,
                 'capabilities': ['staged_placement_handoff'],
                 'windows': [{'id': 9, 'app_id': 'nemo'}]}
        with patch.object(file_manager, 'capture_shell', return_value=shell), \
                patch.object(file_manager.operations, 'current', return_value=None), \
                patch.object(file_manager, 'move_window_result', return_value={
                    'status': 'deferred', 'token': 'final', 'handoff_owned': True}) as move:
            with self.assertRaises(file_manager.PlacementPending) as raised:
                file_manager._place(9, target, file_manager.time.monotonic() + 3)
        self.assertEqual(raised.exception.request_id, 'native-handoff:final')
        self.assertEqual(move.call_count, 1)
        self.assertEqual(move.call_args.args[1], dict(target, stage_before_handoff=True))

    def test_legacy_none_and_empty_snapshot_skip_without_actions(self):
        for snapshot in (None, record()):
            with self.subTest(snapshot=snapshot), \
                 patch.object(file_manager, "_default_is_nemo") as default, \
                 patch.object(file_manager, "launch_graphical_service") as launch:
                self.assertEqual(file_manager.restore_file_manager(snapshot), 0)
                default.assert_not_called()
                launch.assert_not_called()

    def test_dry_run_has_no_desktop_bridge_or_launch_effects(self):
        snapshot = record(saved(), saved("file:///tmp"))
        with patch.object(file_manager, "_default_is_nemo") as default, \
             patch.object(file_manager, "capture_shell") as shell, \
             patch.object(file_manager, "_bridge") as bridge, \
             patch.object(file_manager, "launch_graphical_service") as launch, \
             patch.object(file_manager, "expect_window") as expect:
            self.assertEqual(file_manager.restore_file_manager(snapshot, dry_run=True), 2)
            default.assert_not_called()
            shell.assert_not_called()
            bridge.assert_not_called()
            launch.assert_not_called()
            expect.assert_not_called()

    def test_uri_schema_rejects_credentials_controls_and_bad_active_tab(self):
        for uri in (
            "file://user:secret@example.test/path",
            "file:///tmp/a%00b",
            "file:///tmp/a?query=1",
            "file:///tmp/a#fragment",
            "https://example.test/path",
            "file://relative/path",
        ):
            with self.subTest(uri=uri):
                with self.assertRaises(ValueError):
                    file_manager.validate_file_manager(record(saved(uri)))
        for active_tab in (-1, 1, True, "0"):
            with self.subTest(active_tab=active_tab):
                with self.assertRaises(ValueError):
                    file_manager.validate_file_manager(record(saved(active_tab=active_tab)))

    def test_capture_with_no_nemo_does_not_call_bridge(self):
        shell = {"available": True, "windows": [], "workspaces": []}
        with patch.object(file_manager, "_bridge") as bridge:
            self.assertEqual(file_manager.capture_file_manager(shell)["windows"], [])
            bridge.assert_not_called()

    def test_capture_fails_for_ambiguous_or_incomplete_windows(self):
        shell = {
            "available": True,
            "windows": [
                {"id": 10, "app_id": "nemo", "pid": 42, "workspace": 0},
                {"id": 11, "app_id": "nemo", "pid": 42, "workspace": 0},
            ],
            "workspaces": [{"index": 0, "name": "Work"}],
        }
        incomplete = [{"id": 10, "locations": ["file:///tmp"], "active_tab": 0,
                       "complete": True, "shell": shell["windows"][0], "pid": 42}]
        with patch.object(file_manager._LiveWindows, "get", return_value=incomplete), \
             self.assertRaises(CommandError):
            file_manager.capture_file_manager(shell)

        with patch.object(file_manager._LiveWindows, "get", side_effect=CommandError("ambiguous")), \
             self.assertRaisesRegex(CommandError, "ambiguous"):
            file_manager.capture_file_manager({**shell, "windows": [shell["windows"][0]]})

    def test_default_changed_is_checked_before_launch(self):
        with patch.object(file_manager, "_default_is_nemo", return_value=False), \
             patch.object(file_manager, "launch_graphical_service") as launch:
            with self.assertRaisesRegex(CommandError, "default file manager has changed"):
                file_manager.restore_file_manager(record(saved()))
            launch.assert_not_called()

    def test_mount_gate_fails_before_gio_probe(self):
        uri = "file:///home/sagecat/Drives/gdrive/folder"
        def run(command, **kwargs):
            self.assertEqual(command[:5], ["/usr/bin/systemctl", "--user", "is-active", "--quiet", "rclone-gdrive.service"])
            return subprocess.CompletedProcess(command, 1)

        with patch.object(file_manager.subprocess, "run", side_effect=run) as mocked:
            with self.assertRaisesRegex(CommandError, "mounted storage"):
                file_manager._check_folder(uri)
            self.assertEqual(mocked.call_count, 1)

    def test_exact_uri_reuses_existing_window_without_duplicate_launch(self):
        item = {"pid": 42, "id": 7, "locations": ["file:///tmp"], "active_tab": 0,
                "shell": {"id": 7, "workspace": 1, "monitor": "A", "state": "normal",
                          "geometry": placement()["geometry"]}}
        live = Mock()
        live.get.side_effect = [[item], [item]]
        with patch.object(file_manager, "_default_is_nemo", return_value=True), \
             patch.object(file_manager, "_LiveWindows", return_value=live), \
             patch.object(file_manager, "_bridge", return_value=True) as bridge, \
             patch.object(file_manager, "launch_graphical_service") as launch:
            self.assertEqual(file_manager.restore_file_manager(record(saved("file:///tmp")), no_place=True), 1)
        launch.assert_not_called()
        bridge.assert_called_once_with("SelectTab", 7, 0)

    def test_two_same_uri_windows_are_claimed_separately(self):
        first = {"pid": 42, "id": 7, "locations": ["file:///tmp"], "active_tab": 0,
                 "shell": {"id": 7, "workspace": 1}}
        second = {"pid": 42, "id": 8, "locations": ["file:///tmp"], "active_tab": 0,
                  "shell": {"id": 8, "workspace": 1}}
        live = Mock()
        live.get.side_effect = [[first, second], [first], [first, second], [second]]
        with patch.object(file_manager, "_default_is_nemo", return_value=True), \
             patch.object(file_manager, "_LiveWindows", return_value=live), \
             patch.object(file_manager, "_bridge", return_value=True) as bridge:
            self.assertEqual(file_manager.restore_file_manager(record(saved("file:///tmp"), saved("file:///tmp")), no_place=True), 2)
        self.assertEqual(bridge.call_args_list, [call("SelectTab", 7, 0), call("SelectTab", 8, 0)])

    def test_partial_errors_are_reported_and_raise_after_all_windows(self):
        first = {"pid": 42, "id": 7, "locations": ["file:///tmp/a"], "active_tab": 0,
                 "shell": {"id": 7, "workspace": 1}}
        second = {"pid": 42, "id": 8, "locations": ["file:///tmp/b"], "active_tab": 0,
                  "shell": {"id": 8, "workspace": 1}}
        live = Mock()
        live.get.side_effect = [[first, second], [first], [first, second], [second]]
        reports = []
        def reporter(state, message, current, total):
            reports.append((state, message, current, total))
        with patch.object(file_manager, "_default_is_nemo", return_value=True), \
             patch.object(file_manager, "_LiveWindows", return_value=live), \
             patch.object(file_manager, "_bridge", side_effect=[True, CommandError("selection failed")]):
            with self.assertRaisesRegex(CommandError, "selection failed"):
                file_manager.restore_file_manager(record(saved("file:///tmp/a"), saved("file:///tmp/b")), no_place=True, reporter=reporter)
        self.assertTrue(any(state == "failed" and "selection failed" in message for state, message, _, _ in reports))
        self.assertTrue(any(state == "running" and "restored" in message for state, message, _, _ in reports))


    def test_later_windows_receive_individual_budgets_under_fixed_operation_cap(self):
        for operation_deadline in (None, 45.0):
            with self.subTest(operation_deadline=operation_deadline):
                clock = [0.0]
                owner = None if operation_deadline is None else Mock(deadline=operation_deadline)
                items = [{"pid": 42, "id": index, "locations": [f"file:///tmp/{index}"],
                          "active_tab": 0, "shell": {"id": index, "workspace": 1}}
                         for index in range(1, 5)]
                deadlines = []
                def place(wid, target, deadline):
                    deadlines.append(deadline)
                    self.assertGreaterEqual(deadline - clock[0], 15)
                    clock[0] += 15
                with patch.object(file_manager.time, "monotonic", side_effect=lambda: clock[0]), \
                     patch.object(file_manager.operations, "current", return_value=owner), \
                     patch.object(file_manager, "_placement_authority"), \
                     patch.object(file_manager, "_default_is_nemo", return_value=True), \
                     patch.object(file_manager._LiveWindows, "get", return_value=items), \
                     patch.object(file_manager, "_bridge", return_value=True), \
                     patch.object(file_manager, "_target", return_value=placement()), \
                     patch.object(file_manager, "_place", side_effect=place), \
                     patch.object(file_manager, "launch_graphical_service") as launch:
                    recipe = record(*(saved(item["locations"][0]) for item in items))
                    if owner is None:
                        result = file_manager.restore_file_manager(recipe, timeout=30)
                        self.assertEqual(result, 4)
                        self.assertTrue(all(item.success for item in result.results))
                    else:
                        with self.assertRaises(ProviderRestoreError) as raised:
                            file_manager.restore_file_manager(recipe, timeout=30)
                        self.assertEqual([item.success for item in raised.exception.results], [True, True, True, False])
                        self.assertIn("deadline exceeded", raised.exception.results[-1].identity.detail)
                        self.assertEqual(owner.deadline, operation_deadline)
                launch.assert_not_called()
                self.assertEqual(deadlines, [30, 45, 60, 75] if owner is None else [30, 45, 45])

    def test_slow_native_read_cannot_submit_after_deadline(self):
        clock = [0.0]
        def capture(*, timeout):
            self.assertEqual(timeout, 5)
            clock[0] = 6
            return {"windows": [{"id": 19, "app_id": "nemo"}], "active_workspace": 1}
        with patch.object(file_manager.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(file_manager.operations, "current", return_value=None), \
             patch.object(file_manager, "capture_shell", side_effect=capture), \
             patch.object(file_manager, "move_window_result") as move:
            with self.assertRaisesRegex(CommandError, "before any placement request"):
                file_manager._place(19, placement(), 5)
        move.assert_not_called()

    def test_changed_or_cancelled_startup_owner_after_read_cannot_submit(self):
        for superseded in (True, False):
            with self.subTest(superseded=superseded):
                owner = Mock(deadline=30, mode="startup")
                other = Mock(deadline=30, mode="startup")
                changed = [False]
                def capture(**kwargs):
                    changed[0] = True
                    return {"windows": [{"id": 19, "app_id": "nemo"}], "active_workspace": 1}
                def startup_guard(context):
                    self.assertIs(context, owner)
                    if changed[0] and not superseded:
                        raise CommandError("Startup operation was cancelled or suspended")
                with patch.object(file_manager.time, "monotonic", return_value=0), \
                     patch.object(file_manager.operations, "current", side_effect=lambda: other if changed[0] and superseded else owner), \
                     patch.object(browser, "browser_continuation_guard", side_effect=startup_guard), \
                     patch.object(file_manager, "capture_shell", side_effect=capture), \
                     patch.object(file_manager, "move_window_result") as move:
                    with self.assertRaisesRegex(CommandError, "no longer owns a live operation"):
                        file_manager._place(19, placement(), 50)
                move.assert_not_called()

    def test_native_submission_timeout_reaches_transport_without_changing_defaults(self):
        target = placement()
        accepted = {"status": "applied", "token": "owned-placement"}
        with patch.object(desktop, "_winctl", return_value=accepted) as transport:
            self.assertEqual(desktop.move_window_result(19, target, timeout=.75), accepted)
            self.assertEqual(transport.call_args.kwargs, {"timeout": .75})
            self.assertIn('{"id":19}', transport.call_args.args[0])
            self.assertEqual(desktop.move_window_result(19, target), accepted)
            self.assertEqual(transport.call_args.kwargs, {})

    def test_launch_recreates_only_missing_exact_uri_window_and_uses_tabs_flag(self):
        existing = {"pid": 42, "id": 7, "locations": ["file:///tmp/existing"], "active_tab": 0,
                    "shell": {"id": 7, "workspace": 1}}
        created = {"pid": 42, "id": 8, "locations": ["file:///tmp/missing"], "active_tab": 0,
                   "shell": {"id": 8, "workspace": 1}}
        live = Mock()
        live.get.side_effect = [[existing], [existing], [], [existing], [created], [created]]
        with patch.object(file_manager, "_default_is_nemo", return_value=True), \
             patch.object(file_manager, "_LiveWindows", return_value=live), \
             patch.object(file_manager, "_bridge", return_value=True), \
             patch.object(file_manager, "_check_folder") as check, \
             patch.object(file_manager, "launch_graphical_service") as launch:
            self.assertEqual(file_manager.restore_file_manager(record(saved("file:///tmp/existing"), saved("file:///tmp/missing")), no_place=True), 2)
        check.assert_called_once_with("file:///tmp/missing")
        launch.assert_called_once_with(["/usr/bin/nemo", "--no-default-window", "--", "file:///tmp/missing"], "file-manager")


if __name__ == "__main__":
    unittest.main()
