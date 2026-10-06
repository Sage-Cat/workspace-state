from __future__ import annotations

import subprocess
import threading
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
    class Clock:
        def __init__(self):
            self.value = 0.0

        def monotonic(self):
            self.value += .15
            return self.value

        def sleep(self, seconds):
            self.value += seconds

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

    def test_place_inactive_workspace_stages_then_applies_exact_final_target(self):
        clock = self.Clock()
        wid = 9
        target = placement(workspace=2, monitor="B")
        initial = {"id": wid, "app_id": "nemo", "workspace": 1, "monitor": "A",
                   "state": "normal", "geometry": {"x": 0, "y": 0, "width": 400, "height": 300}}
        staged = dict(initial, **{key: target[key] for key in ("workspace", "monitor", "state", "geometry")})
        staged["workspace"] = 1
        final = dict(staged, workspace=2, monitor="B")
        move = Mock(return_value={"placed": True})
        def shell(**kwargs):
            window = initial if move.call_count == 0 else staged if move.call_count == 1 else final
            return {"windows": [window], "active_workspace": 1}
        with patch.object(file_manager.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(file_manager.time, "sleep", side_effect=clock.sleep), \
             patch.object(file_manager, "capture_shell", side_effect=shell), \
             patch.object(file_manager, "move_window_result", move):
            file_manager._place(wid, target, clock.value + 30)
        self.assertEqual(move.call_count, 2)
        self.assertEqual(move.call_args_list[0].args[0], wid)
        self.assertEqual(move.call_args_list[0].args[1]["workspace"], 1)
        self.assertEqual(move.call_args_list[1].args[1], target)

    def test_place_on_active_workspace_applies_requested_geometry_and_state(self):
        clock = self.Clock()
        wid = 9
        target = placement(workspace=1, monitor="B", state="normal")
        initial = {"id": wid, "app_id": "nemo", "workspace": 1, "monitor": "A",
                   "state": "normal", "geometry": {"x": 0, "y": 0, "width": 400, "height": 300}}
        final = dict(initial, monitor="B", geometry=target["geometry"])
        move = Mock(return_value={"placed": True})
        def shell(**kwargs):
            window = initial if move.call_count == 0 else final
            return {"windows": [window], "active_workspace": 1}
        with patch.object(file_manager.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(file_manager.time, "sleep", side_effect=clock.sleep), \
             patch.object(file_manager, "capture_shell", side_effect=shell), \
             patch.object(file_manager, "move_window_result", move):
            file_manager._place(wid, target, clock.value + 30)
        self.assertEqual(move.call_count, 1)
        self.assertEqual(move.call_args.args, (wid, target))
        self.assertGreater(move.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(move.call_args.kwargs["timeout"], 10)

    def test_place_minimized_stages_visible_then_minimizes_before_final_move(self):
        clock = self.Clock()
        wid = 9
        target = placement(workspace=2, monitor="B", state="minimized")
        geometry = {"x": 0, "y": 0, "width": 400, "height": 300}
        initial = {"id": wid, "app_id": "nemo", "workspace": 1, "monitor": "A",
                   "state": "normal", "geometry": geometry}
        visible = dict(initial, workspace=1, monitor="B", geometry=target["geometry"])
        minimized = dict(visible, state="minimized")
        final = dict(minimized, workspace=2)
        move = Mock(return_value={"placed": True})
        def shell(**kwargs):
            window = (initial if move.call_count == 0 else visible if move.call_count == 1
                      else minimized if move.call_count == 2 else final)
            return {"windows": [window], "active_workspace": 1}
        with patch.object(file_manager.time, "monotonic", side_effect=clock.monotonic), \
             patch.object(file_manager.time, "sleep", side_effect=clock.sleep), \
             patch.object(file_manager, "capture_shell", side_effect=shell), \
             patch.object(file_manager, "move_window_result", move):
            file_manager._place(wid, target, clock.value + 60)
        self.assertEqual(move.call_count, 3)
        self.assertEqual(move.call_args_list[0].args[1]["state"], "normal")
        self.assertEqual(move.call_args_list[0].args[1]["workspace"], 1)
        self.assertEqual(move.call_args_list[1].args[1]["state"], "minimized")
        self.assertEqual(move.call_args_list[1].args[1]["workspace"], 1)
        self.assertEqual(move.call_args_list[2].args[1], target)

    def test_placement_timeout_exposes_only_final_request_receipts(self):
        for workspace, state, stop_after, final_request in (
            (2, "normal", 1, False),
            (2, "minimized", 1, False),
            (2, "minimized", 2, False),
            (1, "normal", 1, True),
            (2, "normal", 2, True),
            (2, "minimized", 3, True),
        ):
            with self.subTest(workspace=workspace, state=state, stop_after=stop_after):
                clock = [0.0]
                target = placement(workspace=workspace, monitor="B", state=state)
                native = {"id": 9, "app_id": "nemo", "workspace": 1, "monitor": "A",
                          "state": "normal", "geometry": {"x": 0, "y": 0, "width": 400, "height": 300}}
                current = {"pid": 42, "id": 9, "locations": ["file:///tmp"],
                           "active_tab": 0, "shell": native}
                destinations = []
                def move(wid, destination, **kwargs):
                    self.assertEqual(wid, native["id"])
                    destinations.append(destination)
                    native.update(destination)
                    if len(destinations) == stop_after:
                        clock[0] = 31
                    return {"status": "accepted", "token": f"placement-{len(destinations)}"}
                def sleep(seconds):
                    clock[0] += seconds
                reporter = Mock()
                with patch.object(file_manager.time, "monotonic", side_effect=lambda: clock[0]), \
                     patch.object(file_manager.time, "sleep", side_effect=sleep), \
                     patch.object(file_manager, "_default_is_nemo", return_value=True), \
                     patch.object(file_manager._LiveWindows, "get", return_value=[current]), \
                     patch.object(file_manager, "_bridge", return_value=True), \
                     patch.object(file_manager, "_target", return_value=target), \
                     patch.object(file_manager, "capture_shell", return_value={"windows": [native], "active_workspace": 1}), \
                     patch.object(file_manager, "move_window_result", side_effect=move), \
                     patch.object(file_manager, "launch_graphical_service") as launch:
                    with self.assertRaises(ProviderRestoreError) as raised:
                        file_manager.restore_file_manager(record(saved("file:///tmp", placement=target)),
                                                          timeout=30, reporter=reporter)
                launch.assert_not_called()
                self.assertEqual(len(destinations), stop_after)
                self.assertEqual(destinations[-1] == target, final_request)
                result, = raised.exception.results
                self.assertEqual(result.identity.state, EvidenceState.VERIFIED)
                self.assertEqual(result.content.state, EvidenceState.VERIFIED)
                self.assertFalse(result.success)
                self.assertTrue(result.placement.retryable)
                self.assertEqual(result.placement.state, EvidenceState.WAITING if final_request else EvidenceState.FAILED)
                self.assertEqual(result.placement.request_id, f"placement-{stop_after}" if final_request else None)
                self.assertEqual(reporter.call_args.args[0], "waiting" if final_request else "failed")
                if not final_request:
                    self.assertIn("retry restoration", result.placement.detail)

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

    def test_place_bounds_contended_gate_wait_without_request_or_release(self):
        gate = Mock()
        gate.acquire.return_value = False
        with patch.object(file_manager.time, "monotonic", return_value=0), \
             patch.object(file_manager.operations, "current", return_value=None), \
             patch.object(file_manager, "placement_lock", gate), \
             patch.object(file_manager, "capture_shell") as capture, \
             patch.object(file_manager, "move_window_result") as move:
            with self.assertRaisesRegex(CommandError, "waiting for another window; no placement requested"):
                file_manager._place(19, placement(), 5)
        gate.acquire.assert_called_once_with(timeout=5)
        gate.release.assert_not_called()
        capture.assert_not_called()
        move.assert_not_called()

    def test_place_rechecks_deadline_after_real_lock_contention(self):
        clock = [0.0]
        waiting = threading.Event()
        real_gate = threading.RLock()
        gate = Mock()
        def acquire(*, timeout):
            waiting.set()
            return real_gate.acquire(timeout=timeout)
        gate.acquire.side_effect = acquire
        gate.release.side_effect = real_gate.release
        errors = []
        def worker():
            try:
                file_manager._place(19, placement(), 5)
            except CommandError as error:
                errors.append(str(error))
        with patch.object(file_manager.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(file_manager.operations, "current", return_value=None), \
             patch.object(file_manager, "placement_lock", gate), \
             patch.object(file_manager, "capture_shell") as capture, \
             patch.object(file_manager, "move_window_result") as move:
            real_gate.acquire()
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(waiting.wait(1))
                clock[0] = 6
            finally:
                real_gate.release()
            thread.join(1)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["Nemo placement deadline elapsed while waiting for another window; no placement requested"])
        gate.acquire.assert_called_once_with(timeout=5)
        gate.release.assert_called_once()
        capture.assert_not_called()
        move.assert_not_called()

    def test_slow_native_read_cannot_submit_after_deadline(self):
        clock = [0.0]
        gate = Mock()
        gate.acquire.return_value = True
        def capture(*, timeout):
            self.assertEqual(timeout, 5)
            clock[0] = 6
            return {"windows": [{"id": 19, "app_id": "nemo"}], "active_workspace": 1}
        with patch.object(file_manager.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(file_manager.operations, "current", return_value=None), \
             patch.object(file_manager, "placement_lock", gate), \
             patch.object(file_manager, "capture_shell", side_effect=capture), \
             patch.object(file_manager, "move_window_result") as move:
            with self.assertRaisesRegex(CommandError, "before any placement request"):
                file_manager._place(19, placement(), 5)
        move.assert_not_called()
        gate.release.assert_called_once()

    def test_changed_or_cancelled_startup_owner_after_read_cannot_submit(self):
        for superseded in (True, False):
            with self.subTest(superseded=superseded):
                owner = Mock(deadline=30, mode="startup")
                other = Mock(deadline=30, mode="startup")
                changed = [False]
                gate = Mock()
                gate.acquire.return_value = True
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
                     patch.object(file_manager, "placement_lock", gate), \
                     patch.object(file_manager, "capture_shell", side_effect=capture), \
                     patch.object(file_manager, "move_window_result") as move:
                    with self.assertRaisesRegex(CommandError, "no longer owns a live operation"):
                        file_manager._place(19, placement(), 50)
                gate.acquire.assert_called_once_with(timeout=30)
                gate.release.assert_called_once()
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
