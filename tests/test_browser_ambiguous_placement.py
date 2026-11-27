"""A Chrome window that could not be told apart keeps its tabs through save and restore."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from workspace_state import browser, browser_reconciliation as reconciliation, cli
from workspace_state.provider_results import (ADVISORY_PREFIX, EvidenceState, PhaseEvidence, ProviderCount,
                                              ProviderItemResult, RECIPE_AGE_ITEM, has_advisory)


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
        self.assertTrue(item.success)  # Advisory attention is not a failure.
        self.assertIn("placement unknown", item.message)
        self.assertEqual(item.evidence.placement.state, EvidenceState.SKIPPED)
        self.assertTrue(item.evidence.attention[0].startswith(ADVISORY_PREFIX))
        self.assertTrue(has_advisory(item.evidence.to_dict()))
        self.assertEqual(item.evidence.content.state, EvidenceState.VERIFIED)

    def test_a_window_without_placement_and_without_the_mark_still_is_not_restored(self):
        saved = {"profiles": [{"profile": "Default", "windows": [window("window-1", "https://example.test/a")]}]}
        with patch.object(browser, "request_browser", side_effect=AssertionError("companion reached")):
            item, = browser.restore_browser(saved, place=True, restore_catalog=saved)
        self.assertIn("no desktop placement was saved", item.message)

    @staticmethod
    def options(*pairs):
        return [{"native_id": native_id, "fields": deepcopy(fields)} for native_id, fields in pairs]

    def test_known_placement_is_carried_only_for_a_candidate_placement_and_a_unique_url_set(self):
        ws1, ws2 = deepcopy(PLACED), dict(deepcopy(PLACED), workspace_index=2, workspace="Study")
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", **deepcopy(ws1)),
            window("window-2", "https://example.test/b", **deepcopy(ws2))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True,
                   _placement_options=self.options((1, ws1), (2, ws2))),
            window("window-2", "https://example.test/b", placement_ambiguous=True,
                   _placement_options=self.options((1, ws1), (2, ws2)))]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 2)
        first, second = current["profiles"][0]["windows"]
        self.assertEqual((first["workspace_index"], second["workspace_index"]), (1, 2))
        self.assertTrue(first["placement_ambiguous"] and "_placement_options" not in first)

    def test_a_placement_nobody_could_have_is_not_carried(self):
        # Closed and reopened: the old placement belongs to none of the candidates.
        gone = dict(deepcopy(PLACED), workspace_index=3, workspace="Other")
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", **deepcopy(PLACED))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True,
                   _placement_options=self.options((1, gone), (2, dict(gone, workspace_index=4))))]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 0)
        self.assertNotIn("workspace_index", current["profiles"][0]["windows"][0])

    def test_two_windows_claiming_one_native_window_carry_nothing(self):
        ws2 = dict(deepcopy(PLACED), workspace_index=2, workspace="Study")
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", **deepcopy(PLACED)),
            window("window-2", "https://example.test/b", **deepcopy(PLACED))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window(label, url, placement_ambiguous=True, _placement_options=self.options((1, PLACED), (2, ws2)))
            for label, url in (("window-1", "https://example.test/a"), ("window-2", "https://example.test/b"))]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 0)
        self.assertTrue(all("workspace_index" not in w for w in current["profiles"][0]["windows"]))

    def test_a_carried_placement_is_never_carried_again(self):
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True, **deepcopy(PLACED))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/a", placement_ambiguous=True,
                   _placement_options=self.options((1, PLACED)))]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 0)

    def test_windows_sharing_their_tabs_carry_nothing(self):
        previous = {"profiles": [{"profile": "Default", "windows": [
            window("window-1", "https://example.test/new", **deepcopy(PLACED)),
            window("window-2", "https://example.test/new", **deepcopy(PLACED))]}]}
        current = {"profiles": [{"profile": "Default", "windows": [
            window(label, "https://example.test/new", placement_ambiguous=True,
                   _placement_options=self.options((1, PLACED), (2, PLACED))) for label in ("window-1", "window-2")]}]}
        self.assertEqual(reconciliation.carry_ambiguous_placements(current, previous), 0)


if __name__ == "__main__":
    unittest.main()
