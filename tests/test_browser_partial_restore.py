from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli
from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult, BrowserUnavailable


class BrowserPartialRestoreTests(unittest.TestCase):
    def test_partial_disabled_profile_uses_background_launch_without_blocking_other_profiles(self):
        snapshot = {"_workspace_switch_exclusions": True, "_workspace_switch_blocked_profiles": {"Paused"},
                    "browsers": {"google_chrome": {"profiles": [
                        {"profile": "Active", "windows": [{"id": "one", "tabs": []}]},
                        {"profile": "Paused", "windows": [{"id": "two", "tabs": []}]},
                    ]}}}
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(cli, "_startup_directory", return_value=root))
            stack.enter_context(patch.object(cli, "connected_profiles", return_value=["Active"]))
            launch = stack.enter_context(patch.object(cli, "ensure_browser_profiles",
                                     side_effect=[[], BrowserUnavailable("profile unavailable")]))
            stack.enter_context(patch.object(cli, "wait_for_browser_settle"))
            stack.enter_context(patch.object(cli, "browser_companion_info", return_value={
                "protocol_version": 2, "capabilities": list(BROWSER_REQUIRED_CAPABILITIES)}))
            stack.enter_context(patch.object(cli, "request_browser", return_value={"exists": False}))
            restore = stack.enter_context(patch.object(cli, "restore_browser",
                                       return_value=[BrowserRestoreResult("Placed active window", True)]))
            cleanup = stack.enter_context(patch.object(cli, "_close_startup_browser_duplicates"))
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*profile unavailable"):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False, no_place=False),
                                      start_browser=True)
        self.assertEqual(launch.call_args_list[0].args[0]["profiles"][0]["profile"], "Active")
        self.assertEqual(launch.call_args_list[0].kwargs, {})
        self.assertEqual(launch.call_args_list[1].args[0]["profiles"][0]["profile"], "Paused")
        self.assertEqual(launch.call_args_list[1].kwargs, {"workspace_switch": True})
        restore.assert_called_once()
        self.assertEqual(restore.call_args.args[0]["profiles"][0]["profile"], "Active")
        cleanup.assert_not_called()

    def test_completed_off_uses_background_launch_or_reuses_an_already_connected_profile(self):
        snapshot = {"_workspace_switch_exclusions": True, "_workspace_switch_cold_profiles": {"Default"},
                    "_workspace_switch_blocked_profiles": set(), "browsers": {"google_chrome": {
                        "profiles": [{"profile": "Default", "windows": [{"id": "active", "tabs": []}]}]}}}
        for connected_at_start in (False, True):
            with self.subTest(connected=connected_at_start), ExitStack() as stack:
                root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
                stack.enter_context(patch.object(cli, "_startup_directory", return_value=root))
                connected = ["Default"] if connected_at_start else []
                stack.enter_context(patch.object(cli, "connected_profiles", side_effect=lambda: list(connected)))

                def background_start(chrome, *, workspace_switch):
                    self.assertTrue(workspace_switch)
                    self.assertEqual(chrome["profiles"][0]["profile"], "Default")
                    connected.append("Default")
                    return ["Default"]

                launch = stack.enter_context(patch.object(cli, "ensure_browser_profiles", side_effect=background_start))
                stack.enter_context(patch.object(cli, "wait_for_browser_settle"))
                stack.enter_context(patch.object(cli, "browser_companion_info", return_value={
                    "protocol_version": 2, "capabilities": list(BROWSER_REQUIRED_CAPABILITIES)}))
                rpc = stack.enter_context(patch.object(cli, "request_browser", return_value={"exists": False}))
                restore = stack.enter_context(patch.object(cli, "restore_browser",
                                              return_value=[BrowserRestoreResult("Placed active window", True)]))
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False, no_place=False),
                                      start_browser=True)
                self.assertEqual(launch.call_count, int(not connected_at_start))
                self.assertNotIn("list_windows", [call.args[0] for call in rpc.call_args_list])
                restore.assert_called_once()

    def test_failed_window_does_not_block_later_placement_or_inflate_progress(self):
        snapshot = {"browsers": {"google_chrome": {"profiles": [{
            "profile": "Default", "windows": [
                {"id": "one", "tabs": []}, {"id": "two", "tabs": []},
            ],
        }]}}}
        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            stack.enter_context(patch.object(cli, "_startup_directory", return_value=root))
            stack.enter_context(patch.object(cli, "connected_profiles", return_value=["Default"]))
            stack.enter_context(patch.object(cli, "ensure_browser_profiles", return_value=[]))
            stack.enter_context(patch.object(cli, "wait_for_browser_settle"))
            stack.enter_context(patch.object(cli, "browser_companion_info", return_value={
                "protocol_version": 2, "capabilities": list(BROWSER_REQUIRED_CAPABILITIES),
            }))
            stack.enter_context(patch.object(cli, "request_browser", return_value={"exists": False}))
            restore = stack.enter_context(patch.object(cli, "restore_browser", side_effect=[
                [BrowserRestoreResult("URLs redirected", False)],
                [BrowserRestoreResult("Placed second window", True)],
            ]))
            report = stack.enter_context(patch.object(cli, "update_stage"))
            cleanup = stack.enter_context(patch.object(cli, "_close_startup_browser_duplicates"))
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*URLs redirected"):
                cli._restore_browsers(snapshot, Namespace(
                    workspace=None, dry_run=False, no_place=False, login_status=True,
                ), start_browser=True)
            self.assertEqual(restore.call_count, 2)
            self.assertTrue(all(call.kwargs["restore_catalog"] == snapshot["browsers"]["google_chrome"]
                                for call in restore.call_args_list))
            self.assertEqual(report.call_args.kwargs, {"current": 1, "total": 2})
            self.assertEqual(len(list((root / "browser-items").glob("*.done"))), 1)
            cleanup.assert_not_called()
