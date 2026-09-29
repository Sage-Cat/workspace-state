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
    browser_companion_info, restore_browser,
)
from workspace_state.cli import _browser_restore_token_prefix, _close_startup_browser_duplicates, _restore_browsers
from workspace_state.provider_results import EvidenceState, waiting_only


class BrowserVerificationTests(unittest.TestCase):
    def test_missing_original_group_is_reported_without_repeating_restore(self):
        chrome = {"profiles": [{"profile": "Default", "windows": [{
            "id": "one", "tabs": [{"url": "https://example.com/saved"}],
        }]}]}
        warning = "Original group is unavailable; no replacement created"
        with patch("workspace_state.browser.request_browser", return_value={
            "window_id": 7, "created": True, "urls_restored": True,
            "group_warnings": [warning], "warnings": [warning],
        }) as request:
            result = restore_browser(chrome, place=False)[0]
        self.assertFalse(result.success)
        self.assertFalse(result.evidence.success)
        self.assertEqual(result.evidence.content.state, EvidenceState.VERIFIED)
        self.assertEqual(result.evidence.attention, (warning,))
        self.assertIn(warning, result.message)
        self.assertEqual(request.call_count, 1)

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

    def test_companion_readiness_waits_through_reload_and_rejects_stuck_activation(self):
        ready = {"protocol_version": 2, "activation_pending": False,
                 "capabilities": list(BROWSER_REQUIRED_CAPABILITIES)}
        with patch("workspace_state.browser.request_browser", side_effect=[
            {**ready, "activation_pending": True}, BrowserUnavailable("reloading"), ready,
        ]) as request, patch("workspace_state.browser.time.sleep"):
            self.assertEqual(browser_companion_info("Default"), ready)
            self.assertEqual(request.call_count, 3)
            self.assertTrue(all(call.args == ("ping",) for call in request.call_args_list))
        with patch("workspace_state.browser.request_browser", return_value={"activation_pending": True}), \
                patch("workspace_state.browser.time.monotonic", side_effect=[0, 0, 0, 2]), \
                patch("workspace_state.browser.time.sleep"):
            with self.assertRaisesRegex(BrowserUnavailable, "activation pending"):
                browser_companion_info("Default", timeout=1)

    def test_old_worker_without_pending_url_contract_is_rejected_before_restore(self):
        snapshot = {"browsers": {"google_chrome": {"profiles": [{
            "profile": "Default", "windows": [{"id": "one", "tabs": [{"url": "https://example.com/"}]}],
        }]}}}
        with (
            patch("workspace_state.cli.ensure_browser_profiles", return_value=[]),
            patch("workspace_state.cli.connected_profiles", return_value=["Default"]),
            patch("workspace_state.cli.browser_companion_info", return_value={
                "protocol_version": 2,
                "capabilities": list(BROWSER_REQUIRED_CAPABILITIES - {"exact_url_pending"}),
            }),
            patch("workspace_state.cli.wait_for_browser_settle") as settle,
            patch("workspace_state.cli.restore_browser") as restore,
            patch("workspace_state.cli.request_browser") as request,
        ):
            with self.assertRaisesRegex(BrowserUnavailable, "outdated"):
                _restore_browsers(snapshot, Namespace(workspace=None, dry_run=False, no_place=True),
                                  start_browser=True)
            settle.assert_not_called()
            restore.assert_not_called()
            request.assert_not_called()

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
        for proof in ({"urls_restored": False},
                      {"urls_restored": True, "group_warnings": ["Original group missing"]}):
            with self.subTest(proof=proof), patch("workspace_state.cli.request_browser", return_value={
                "exists": True, "window_id": 1, **proof,
            }) as request:
                with self.assertRaisesRegex(BrowserUnavailable, "URLs or original groups are unverified"):
                    _close_startup_browser_duplicates({"Default": {1, 2}}, {"Default": ["token"]})
            self.assertEqual(request.call_count, 1)
