"""Fail closed when Chrome exists but saved tab identity is unverified."""
from argparse import Namespace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state.browser import (
    BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult, BrowserUnavailable,
    restore_browser,
)
from workspace_state.cli import _browser_restore_token_prefix, _close_startup_browser_duplicates, _restore_browsers
from workspace_state.provider_results import EvidenceState, waiting_only


class BrowserVerificationTests(unittest.TestCase):
    def test_saved_navigation_still_loading_remains_observable_without_duplicate(self):
        chrome = {"profiles": [{"profile": "Default", "windows": [{
            "id": "one", "tabs": [{"url": "https://example.com/saved"}],
        }]}]}
        with patch("workspace_state.browser.request_browser", return_value={
            "window_id": 7, "reused": True, "urls_restored": False, "urls_pending": True,
            "url_errors": ["Tab 1 has not finished loading"],
        }) as request:
            result = restore_browser(chrome, place=False, restore_token_prefix="startup")[0]
        self.assertFalse(result.success)
        self.assertTrue(waiting_only([result.evidence]))
        self.assertEqual(result.evidence.content.state, EvidenceState.WAITING)
        self.assertEqual(result.evidence.content.request_id, '["Default","startup:Default:one",7]')
        self.assertEqual(request.call_count, 1)

    def test_unverified_urls_fail_even_without_placement(self):
        chrome = {"profiles": [{"profile": "Default", "windows": [{
            "id": "one", "tabs": [{"url": "https://chatgpt.com/c/saved"}],
        }]}]}
        for verified in (False, None):
            with self.subTest(verified=verified), patch(
                "workspace_state.browser.request_browser",
                return_value={"window_id": 7, "reused": True, "urls_restored": verified},
            ) as request:
                result = restore_browser(chrome, place=False)
                self.assertFalse(result[0].success)
                self.assertIn("exact tab URLs", result[0].message)
                self.assertEqual(request.call_count, 1)

    def test_done_marker_does_not_hide_redirected_tabs(self):
        window = {"id": "one", "tabs": [{"url": "https://chatgpt.com/c/saved"}]}
        snapshot = {"created_at": "saved", "browsers": {"google_chrome": {
            "profiles": [{"profile": "Default", "windows": [window]}],
        }}}
        prefix = _browser_restore_token_prefix(snapshot)
        key = hashlib.sha256(f"{prefix}\0Default\0one".encode()).hexdigest()
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}):
            marker = Path(root) / "workspace-state/startup-test/browser-items" / f"{key}.done"
            marker.parent.mkdir(parents=True)
            marker.write_text("saved\n")
            with (
                patch("workspace_state.cli._boot_id", return_value="test"),
                patch("workspace_state.cli.ensure_browser_profiles", return_value=[]),
                patch("workspace_state.cli.connected_profiles", return_value=["Default"]),
                patch("workspace_state.cli.browser_companion_info", return_value={
                    "protocol_version": 2, "capabilities": list(BROWSER_REQUIRED_CAPABILITIES),
                }),
                patch("workspace_state.cli.wait_for_browser_settle"),
                patch("workspace_state.cli.request_browser", return_value={
                    "exists": True, "window_id": 7, "urls_restored": False,
                }) as status,
                patch("workspace_state.cli.restore_browser", return_value=[
                    BrowserRestoreResult("URL mismatch", False),
                ]) as restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "URL mismatch"):
                    _restore_browsers(snapshot, Namespace(
                        workspace=None, dry_run=False, no_place=True,
                    ), start_browser=True)
                self.assertEqual(status.call_args.args[1]["window"], window)
                restore.assert_called_once()
                self.assertFalse(marker.exists())

    def test_cleanup_preserves_unrelated_loading_and_new_windows(self):
        closed = []
        windows = [
            {"id": 1, "full_signature": "saved", "urls_loaded": True},  # keeper
            {"id": 2, "full_signature": "saved", "urls_loaded": True},  # exact duplicate
            {"id": 3, "full_signature": "other", "urls_loaded": True},
            {"id": 4, "full_signature": "saved", "urls_loaded": False},
            {"id": 5, "full_signature": "saved", "urls_loaded": True},  # newly opened
            {"id": 6},  # no proof
        ]
        def request(action, payload, **_kwargs):
            if action == "restore_status":
                return {"exists": True, "window_id": 1, "urls_restored": True}
            if action == "list_windows":
                return windows
            if action == "close_restored_window":
                closed.append(payload["window_id"])
                return {"closed": True}
            self.fail(action)
        with patch("workspace_state.cli.request_browser", side_effect=request):
            self.assertEqual(_close_startup_browser_duplicates(
                {"Default": {1, 2, 3, 4, 6}}, {"Default": ["token"]},
            ), 1)
        self.assertEqual(closed, [2])

    def test_cleanup_refuses_unverified_keeper(self):
        with patch("workspace_state.cli.request_browser", return_value={
            "exists": True, "window_id": 1, "urls_restored": False,
        }) as request:
            with self.assertRaisesRegex(BrowserUnavailable, "URLs are unverified"):
                _close_startup_browser_duplicates({"Default": {1, 2}}, {"Default": ["token"]})
        self.assertEqual(request.call_count, 1)
