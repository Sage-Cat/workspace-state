from argparse import Namespace
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli
from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult


class BrowserPartialRestoreTests(unittest.TestCase):
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
            self.assertEqual(report.call_args.kwargs, {"current": 1, "total": 2})
            self.assertEqual(len(list((root / "browser-items").glob("*.done"))), 1)
            cleanup.assert_not_called()
