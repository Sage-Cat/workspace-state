"""Every supported provider submits one final intent to the native owner."""
from contextlib import ExitStack
from pathlib import Path
import time
import threading
import unittest
from unittest.mock import patch

from workspace_state import browser, file_manager, restore, social_apps, vscode
from workspace_state.provider_results import PlacementPending


class NativeHandoffContractTests(unittest.TestCase):
    def test_missing_required_capability_never_falls_back_to_temporary_moves(self):
        target = {"workspace": 2, "monitor": 1, "state": "maximized", "geometry": {}}
        window = {"id": 42, "pid": 123, "app_ids": ["alacritty"]}
        shell = {"available": True, "windows": [window], "capabilities": []}
        calls = [
            (browser, lambda: browser._place_browser_window(profile="Default", chrome_window_id=1,
                         app_id="google-chrome", placement=target)),
            (file_manager, lambda: file_manager._place(42, target, time.monotonic() + 10)),
            (restore, lambda: restore._verify_terminal_placement({"id": 42}, target)),
            (social_apps, lambda: social_apps._place_social_window(social_apps.APPS[0], 42, target, 0,
                         deadline=time.monotonic() + 10)),
            (vscode, lambda: vscode._place(42, target, time.monotonic() + 10)),
        ]
        for module, call in calls:
            with self.subTest(provider=module.__name__), ExitStack() as stack:
                stack.enter_context(patch.object(module, "capture_shell", return_value=shell))
                move = stack.enter_context(patch.object(module, "move_window_result"))
                stack.enter_context(patch.object(browser, "_identify_native_window", return_value=(42, {})))
                stack.enter_context(patch.object(browser, "_release_window_identification"))
                stack.enter_context(patch.object(file_manager, "_placement_authority"))
                stack.enter_context(patch.object(file_manager.operations, "current", return_value=None))
                for owner in (file_manager, social_apps, vscode):
                    stack.enter_context(patch.object(owner, "matching_windows", return_value=[window]))
                with self.assertRaisesRegex(Exception, "handoff capability is unavailable"):
                    call()
                move.assert_not_called()

    def test_provider_preserves_final_state_and_destination_for_every_window_state(self):
        for state in ("normal", "minimized", "maximized", "fullscreen"):
            target = {"workspace": 2, "monitor": 1, "state": state, "geometry": {}}
            shell = {"capabilities": ["staged_placement_handoff"], "windows": [{"id": 42, "app_id": "nemo"}]}
            with self.subTest(state=state), patch.object(file_manager, "capture_shell", return_value=shell), \
                    patch.object(file_manager.operations, "current", return_value=None), \
                    patch.object(file_manager, "move_window_result", return_value={
                        "status": "deferred", "token": "final", "handoff_owned": True}) as move:
                with self.assertRaises(PlacementPending) as caught:
                    file_manager._place(42, target, time.monotonic() + 10)
                self.assertEqual(caught.exception.request_id, "native-handoff:final")
                self.assertEqual(move.call_count, 1)
                self.assertEqual(move.call_args.args, (42, dict(target, stage_before_handoff=True)))

    def test_blocked_browser_identification_does_not_starve_other_native_submissions(self):
        target = {"workspace": 2, "monitor": 1, "state": "normal", "geometry": {}}
        window = {"id": 42, "pid": 123, "app_ids": ["alacritty"], "workspace": 0}
        shell = {"available": True, "windows": [window], "capabilities": ["staged_placement_handoff"]}
        identified = threading.Event()
        release_browser = threading.Event()
        errors = []
        def blocked_identification(**kwargs):
            identified.set()
            if not release_browser.wait(5):
                raise AssertionError("test did not release browser identification")
            return 42, {}
        def run_browser():
            try:
                browser._place_browser_window(profile="Default", chrome_window_id=1,
                    app_id="google-chrome", placement=target)
            except browser.BrowserPlacementPending:
                pass
            except BaseException as error:
                errors.append(error)
        calls = [
            (file_manager, lambda: file_manager._place(42, target, time.monotonic() + 10)),
            (restore, lambda: restore._verify_terminal_placement({"id": 42}, target)),
            (social_apps, lambda: social_apps._place_social_window(social_apps.APPS[0], 42, target, 0,
                         deadline=time.monotonic() + 10)),
            (vscode, lambda: vscode._place(42, target, time.monotonic() + 10)),
        ]
        with ExitStack() as stack:
            for module in (browser, *(module for module, _ in calls)):
                stack.enter_context(patch.object(module, "capture_shell", return_value=shell))
                stack.enter_context(patch.object(module, "move_window_result", return_value={
                    "status": "deferred", "token": "final", "handoff_owned": True}))
            stack.enter_context(patch.object(browser, "_identify_native_window", side_effect=blocked_identification))
            stack.enter_context(patch.object(browser, "_release_window_identification"))
            stack.enter_context(patch.object(file_manager, "_placement_authority"))
            stack.enter_context(patch.object(file_manager.operations, "current", return_value=None))
            for module in (file_manager, social_apps, vscode):
                stack.enter_context(patch.object(module, "matching_windows", return_value=[window]))
            thread = threading.Thread(target=run_browser)
            thread.start()
            workers = []
            try:
                self.assertTrue(identified.wait(1))
                for module, invoke in calls:
                    with self.subTest(provider=module.__name__):
                        finished = threading.Event()
                        def submit(invoke=invoke, finished=finished):
                            try:
                                result = invoke()
                                self.assertEqual(result, "native-handoff:final")
                            except PlacementPending as pending:
                                self.assertEqual(pending.request_id, "native-handoff:final")
                            except BaseException as error:
                                errors.append(error)
                            finally:
                                finished.set()
                        worker = threading.Thread(target=submit)
                        workers.append(worker)
                        worker.start()
                        self.assertTrue(finished.wait(1), "native submission waited behind Chrome identification")
                        module.move_window_result.assert_called_once()
                self.assertTrue(thread.is_alive(), "Chrome should still be waiting for its own marker")
            finally:
                release_browser.set()
                thread.join(2)
                for worker in workers:
                    worker.join(2)
        self.assertEqual(errors, [])
