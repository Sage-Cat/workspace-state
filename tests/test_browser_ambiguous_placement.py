"""A Chrome window that could not be told apart keeps its tabs through save and restore."""
import argparse
import unittest
from copy import deepcopy
from unittest.mock import patch

from workspace_state import browser, browser_reconciliation as reconciliation, cli
from workspace_state.provider_results import (ADVISORY_PREFIX, EvidenceState, PhaseEvidence, ProviderCount,
                                              ProviderItemResult, RECIPE_AGE_ITEM, advisory_only)
from workspace_state.startup import StageMarker


def window(label, url, **fields):
    return {"id": label, "type": "normal", "incognito": False, "state": "normal", "groups": [],
            "tabs": [{"url": url, "title": "T", "pinned": False, "active": True, "group": None}], **fields}


PLACED = {"workspace_index": 1, "workspace": "Work", "monitor": {"index": 0, "connector": "DP-1"},
          "geometry": {"x": 0, "y": 0, "width": 1000, "height": 700}}


class AmbiguousPlacementTests(unittest.TestCase):
    def test_restore_opens_an_unplaced_window_with_its_tabs_and_marks_attention(self):
        saved = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True)]}]}

        def request(action, payload=None, **_kwargs):
            if action == "restore_window":
                return {"window_id": 7, "urls_restored": True, "created": True}
            return {}
        with patch.object(browser, "request_browser", side_effect=request) as rpc, \
                patch.object(browser, "_place_browser_window") as place, \
                patch.object(browser, "expect_window") as expect:
            results = browser.restore_browser(saved, place=True, restore_catalog=saved)
        place.assert_not_called()
        expect.assert_not_called()
        self.assertIn("restore_window", [call.args[0] for call in rpc.call_args_list])
        item, = results
        self.assertFalse(item.success)
        self.assertIn("placement unknown", item.message)
        self.assertEqual(item.evidence.placement.state, EvidenceState.SKIPPED)
        self.assertTrue(item.evidence.attention[0].startswith(ADVISORY_PREFIX))
        self.assertTrue(advisory_only(item.evidence.to_dict()))
        self.assertEqual(item.evidence.content.state, EvidenceState.VERIFIED)

    def test_a_window_without_placement_and_without_the_mark_still_is_not_restored(self):
        saved = {"profiles": [{"profile": "Default", "windows": [window("window-1", "https://example.test/a")]}]}
        with patch.object(browser, "request_browser", side_effect=AssertionError("companion reached")):
            item, = browser.restore_browser(saved, place=True, restore_catalog=saved)
        self.assertIn("no desktop placement was saved", item.message)

    def test_known_placement_is_carried_only_when_the_tabs_name_one_window(self):
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", **deepcopy(PLACED)),
            window("window-2", "https://example.test/new", **deepcopy(PLACED)),
            window("window-3", "https://example.test/new", **deepcopy(PLACED))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True),
            window("window-2", "https://example.test/new", placement_ambiguous=True),
            window("window-3", "https://example.test/new", placement_ambiguous=True)]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 1)
        first, second, third = current["profiles"][0]["windows"]
        self.assertEqual((first["workspace_index"], first["geometry"]), (1, PLACED["geometry"]))
        self.assertNotIn("workspace_index", second)
        self.assertNotIn("workspace_index", third)
        self.assertIsNotNone(browser.browser_window_placement(first))

    def test_ambiguous_windows_never_stop_later_captures(self):
        item = ProviderItemResult("chrome", "Default/window-1", PhaseEvidence(EvidenceState.VERIFIED),
                                  PhaseEvidence(EvidenceState.VERIFIED), PhaseEvidence(EvidenceState.SKIPPED),
                                  attention=(ADVISORY_PREFIX + "saved placement is unknown",))
        good = ProviderItemResult("chrome", "Default/window-2", PhaseEvidence(EvidenceState.VERIFIED),
                                  PhaseEvidence(EvidenceState.VERIFIED), PhaseEvidence(EvidenceState.VERIFIED))
        for results in ((item,), (item, good)):
            with patch.object(cli, "update_stage") as stage, patch.object(cli, "publish_provider_results"), \
                    patch.object(cli, "_record_seen"), patch("workspace_state.placement_guard.record"), \
                    patch.object(cli, "_write_attempt_marker") as marker:
                state = cli._publish_category_outcome("browsers", {}, count=ProviderCount(len(results), results))
            self.assertEqual(state, "degraded")
            self.assertEqual(marker.call_args.args[3], "ready")
            written = StageMarker("browsers", "ready", "", "", None, tuple(r.to_dict() for r in results))
            self.assertTrue(written.verified)
        failed = ProviderItemResult("chrome", "Default/window-3", PhaseEvidence(EvidenceState.FAILED),
                                    PhaseEvidence(EvidenceState.SKIPPED), PhaseEvidence(EvidenceState.SKIPPED))
        with patch.object(cli, "update_stage"), patch.object(cli, "publish_provider_results"), \
                patch.object(cli, "_record_seen"), patch("workspace_state.placement_guard.record"), \
                patch.object(cli, "_write_attempt_marker") as marker:
            cli._publish_category_outcome("browsers", {}, count=ProviderCount(2, (good, failed)))
        self.assertEqual(marker.call_args.args[3], "attempted")


if __name__ == "__main__":
    unittest.main()
