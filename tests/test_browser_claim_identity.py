from __future__ import annotations

import copy
import unittest

from workspace_state.cli import _browser_restore_token_prefix


class BrowserClaimIdentityTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"browsers": {"google_chrome": {"profiles": [{
            "profile": "Default", "profile_directory": "Default", "app_id": "google-chrome",
            "windows": [{
                "id": "window-1", "type": "normal", "incognito": False,
                "workspace_index": 2, "workspace": "Research", "monitor": {"index": 0},
                "groups": [{"id": "group-1", "title": "Work", "color": "blue"}],
                "tabs": [{"url": "https://example.test/current", "pinned": False,
                          "active": True, "group": "group-1"}],
            }],
        }]}}}

    def test_observation_and_capture_provenance_do_not_invalidate_live_claims(self):
        expected = _browser_restore_token_prefix(self.snapshot)
        observed = copy.deepcopy(self.snapshot)
        category = observed["browsers"]["google_chrome"]
        category.update(
            available=True, errors=["past capture unavailable"],
            captured_at="later", capture_context={"epoch": 4},
            provenance={"retained_reason": "startup incomplete"},
            latest_observation={"captured_at": "first", "browser_state": {"profiles": []}},
        )
        profile = category["profiles"][0]
        profile["captured_at"] = "new capture"
        window = profile["windows"][0]
        window.update(runtime_window_id=915, capture_context={"native_id": 916})
        window["tabs"][0].update(title="New notification", id=917, status="loading")
        self.assertEqual(expected, _browser_restore_token_prefix(observed))
        category["latest_observation"]["captured_at"] = "second"
        category["latest_observation"]["browser_state"] = {
            "profiles": [{"windows": [{"tabs": [{"url": "https://example.test/unadopted"}]}]}],
        }
        self.assertEqual(expected, _browser_restore_token_prefix(observed))

    def test_semantic_recipe_changes_still_create_a_new_claim_scope(self):
        expected = _browser_restore_token_prefix(self.snapshot)
        changes = [
            ("url", "https://example.test/accepted"), ("pinned", True),
            ("active", False), ("group", None),
        ]
        for field, value in changes:
            with self.subTest(tab_field=field):
                changed = copy.deepcopy(self.snapshot)
                changed["browsers"]["google_chrome"]["profiles"][0]["windows"][0]["tabs"][0][field] = value
                self.assertNotEqual(expected, _browser_restore_token_prefix(changed))
        for field, value in [("workspace_index", 3), ("incognito", True), ("type", "popup")]:
            with self.subTest(window_field=field):
                changed = copy.deepcopy(self.snapshot)
                changed["browsers"]["google_chrome"]["profiles"][0]["windows"][0][field] = value
                self.assertNotEqual(expected, _browser_restore_token_prefix(changed))


if __name__ == "__main__":
    unittest.main()
