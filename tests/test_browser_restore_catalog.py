"""Creation guards receive every saved window from the relevant profile."""
import unittest
from unittest.mock import patch

from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, restore_browser


class BrowserRestoreCatalogTests(unittest.TestCase):
    def test_catalog_includes_unselected_windows_and_isolates_profiles(self):
        missing = {"id": "missing", "workspace": "Life", "tabs": [{"url": "https://missing.test/"}]}
        other = {"id": "other", "workspace": "Work", "tabs": [{"url": "https://other.test/"}]}
        private = {"id": "private", "workspace": "Life", "tabs": [{"url": "https://private.test/"}]}
        state = {"profiles": [
            {"profile": "Default", "windows": [missing, other]},
            {"profile": "Private", "windows": [private]},
        ]}
        with patch("workspace_state.browser.request_browser", return_value={
            "window_id": 10, "urls_restored": True, "created": False,
        }) as request:
            results = restore_browser(state, workspace="Life", place=False, restore_token_prefix="boot")
        self.assertTrue(all(result.success for result in results))
        self.assertEqual(request.call_count, 2)
        payloads = {call.kwargs["profile"]: call.args[1] for call in request.call_args_list}
        self.assertEqual(payloads["Default"]["restore_catalog"], [
            {"restore_token": "boot:Default:missing", "window": missing},
            {"restore_token": "boot:Default:other", "window": other},
        ])
        self.assertEqual(payloads["Private"]["restore_catalog"], [
            {"restore_token": "boot:Private:private", "window": private},
        ])
        self.assertEqual(payloads["Default"]["restore_token"], "boot:Default:missing")
        self.assertIn("unclaimed_original_guard", BROWSER_REQUIRED_CAPABILITIES)

    def test_single_window_request_retains_full_catalog(self):
        missing = {"id": "missing", "tabs": [{"url": "https://missing.test/"}]}
        other = {"id": "other", "tabs": [{"url": "https://other.test/"}]}
        full = {"profiles": [{"profile": "Default", "windows": [missing, other]}]}
        selected = {"profiles": [{"profile": "Default", "windows": [missing]}]}
        with patch("workspace_state.browser.request_browser", return_value={
            "window_id": 10, "urls_restored": True, "created": True,
        }) as request:
            restore_browser(selected, place=False, restore_token_prefix="boot", restore_catalog=full)
        self.assertEqual(request.call_count, 1)
        payload = request.call_args.args[1]
        self.assertEqual(payload["window"], missing)
        self.assertEqual([entry["window"] for entry in payload["restore_catalog"]], [missing, other])


if __name__ == "__main__":
    unittest.main()
