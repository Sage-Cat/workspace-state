"""Periodic Chrome capture identifies windows by title alone; synthetic data only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from workspace_state import browser
from workspace_state.browser import BrowserUnavailable
from workspace_state.desktop import remap_monitor


def chrome_window(runtime_id, active_title, url):
    return {"id": f"window-{runtime_id}", "runtime_window_id": runtime_id, "type": "normal",
            "incognito": False, "state": "normal", "focused": False, "groups": [],
            "tabs": [{"url": "https://example.test/other", "title": "Other", "pinned": False,
                      "active": False, "group": None},
                     {"url": url, "title": active_title, "pinned": False, "active": True, "group": None}]}


def native(native_id, title, workspace):
    return {"id": native_id, "title": title, "app_id": "google-chrome", "workspace": workspace,
            "monitor": 0, "monitor_identity": {"connector": "DP-1"},
            "monitor_geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
            "geometry": {"x": 0, "y": 0, "width": 1000, "height": 700}, "state": "maximized"}


def profile(*windows):
    return {"profile": "Default", "profile_directory": "Default", "app_id": "google-chrome",
            "windows": list(windows)}


class TitleCaptureTests(unittest.TestCase):
    def capture(self, profiles, shells, summaries=None):
        """Run the title capture; return the request actions it sent."""
        requests = []

        def request(action, payload=None, **kwargs):
            requests.append(action)
            if action != "list_windows":
                raise AssertionError(f"title capture must not send {action}")
            if summaries is not None:
                return summaries
            return [{"id": window["runtime_window_id"], "active_title": browser._active_title(window)}
                    for window in profiles[0]["windows"]]

        with patch.object(browser, "capture_shell", side_effect=[{"windows": deepcopy(shell)} for shell in shells]), \
                patch.object(browser, "request_browser", side_effect=request), \
                patch.object(browser, "_identify_native_window", side_effect=AssertionError("no markers")):
            browser._attach_title_placements(profiles, ["Life", "Work", "Study"])
        return requests

    def test_windows_take_the_placement_of_the_native_window_titled_by_their_active_tab(self):
        profiles = [profile(chrome_window(11, "Inbox", "https://mail.example.test/"),
                            chrome_window(12, "Plan - Docs", "https://docs.example.test/plan"))]
        shell = [native(2, "Plan - Docs - Google Chrome", 2), native(1, "Inbox - Google Chrome", 1)]
        self.assertEqual(self.capture(profiles, [shell, shell]), ["list_windows"])
        first, second = profiles[0]["windows"]
        self.assertEqual((first["workspace"], first["workspace_index"]), ("Work", 1))
        self.assertEqual((second["workspace"], second["workspace_index"]), ("Study", 2))
        self.assertEqual(first["monitor"]["connector"], "DP-1")
        self.assertNotIn("runtime_window_id", first)

    def test_a_note_before_the_suffix_still_identifies_one_window(self):
        profiles = [profile(chrome_window(11, "Song", "https://media.example.test/"))]
        shell = [native(1, "Song - Audio playing - Google Chrome", 0)]
        self.capture(profiles, [shell, shell])
        self.assertEqual(profiles[0]["windows"][0]["workspace_index"], 0)

    def test_capture_and_restore_keep_pending_monitor_intent_separate_from_observation(self):
        identity = {"connector": "HDMI-2", "serial": "saved-display"}
        intent = {**identity, "physical_identities": [identity]}
        observed = native(1, "Inbox - Google Chrome", 1)
        observed["monitor_intent"] = intent
        profiles = [profile(chrome_window(11, "Inbox", "https://mail.example.test/"))]
        self.capture(profiles, [[observed], [observed]])

        window = profiles[0]["windows"][0]
        self.assertEqual(window["monitor"]["connector"], "DP-1")
        self.assertEqual(window["monitor_intent"], intent)
        self.assertEqual(window["geometry"], observed["geometry"])
        placement = browser.browser_window_placement(window)
        self.assertEqual(placement["monitor_identity"]["connector"], "DP-1")
        target = remap_monitor(placement, require_identity=True, shell={"monitors": [
            {"index": 0, "x": 0, "y": 0, "identity": {"connector": "DP-1"}},
            {"index": 1, "x": 1920, "y": 0, "identity": identity},
        ]})
        self.assertEqual(target["monitor"], 1)
        self.assertEqual(target["geometry"], {**observed["geometry"], "x": 1920})
        self.assertEqual(target["monitor_intent"], intent)

    def test_capture_and_restore_allow_absent_or_empty_monitor_intent(self):
        for fields in ({}, {"monitor_intent": {}}):
            with self.subTest(fields=fields):
                observed = native(1, "Inbox - Google Chrome", 1) | fields
                window = chrome_window(11, "Inbox", "https://mail.example.test/")
                window["monitor_intent"] = {"connector": "stale-companion-field"}
                profiles = [profile(window)]
                self.capture(profiles, [[observed], [observed]])

                self.assertNotIn("monitor_intent", window)
                placement = browser.browser_window_placement(window | fields)
                self.assertNotIn("monitor_intent", placement)
                self.assertEqual(placement["monitor_identity"]["connector"], "DP-1")

    def test_ambiguous_missing_and_extra_titles_fail_the_capture(self):
        cases = [
            ([chrome_window(11, "New Tab", "chrome://newtab/"), chrome_window(12, "New Tab", "chrome://newtab/")],
             [native(1, "New Tab - Google Chrome", 0), native(2, "New Tab - Google Chrome", 1)]),
            ([chrome_window(11, "Inbox", "https://mail.example.test/")],
             [native(1, "Loading - Google Chrome", 0)]),
            ([chrome_window(11, "Inbox", "https://mail.example.test/")],
             [native(1, "Inbox - Google Chrome", 0), native(2, "Private - Google Chrome", 1)]),
            ([chrome_window(11, "Inbox", "https://mail.example.test/"),
              chrome_window(12, "Inbox - Mail", "https://mail.example.test/2")],
             [native(1, "Inbox - Mail - Google Chrome", 0), native(2, "Inbox - Google Chrome", 1),
              native(3, "Inbox - Mail - Work - Google Chrome", 2)]),
        ]
        for windows, shell in cases:
            with self.subTest(shell=[item["title"] for item in shell]):
                with self.assertRaises(BrowserUnavailable):
                    self.capture([profile(*deepcopy(windows))], [shell, shell])

    def test_a_title_that_changes_during_the_capture_fails_it(self):
        profiles = [profile(chrome_window(11, "Inbox", "https://mail.example.test/"))]
        shell = [native(1, "Inbox - Google Chrome", 0)]
        with self.assertRaisesRegex(BrowserUnavailable, "changed while matching"):
            self.capture(deepcopy(profiles), [shell, shell], summaries=[{"id": 11, "active_title": "(1) Inbox"}])
        two = [chrome_window(11, "A", "https://a.example.test/"), chrome_window(12, "B", "https://b.example.test/")]
        before = [native(1, "A - Google Chrome", 0), native(2, "B - Google Chrome", 1)]
        after = [native(2, "A - Google Chrome", 1), native(1, "B - Google Chrome", 0)]
        with self.assertRaisesRegex(BrowserUnavailable, "changed while matching"):
            self.capture([profile(*two)], [before, after])

    def test_capture_browser_by_title_never_identifies_with_markers(self):
        captured = profile(chrome_window(11, "Inbox", "https://mail.example.test/"))
        with patch.object(browser, "_host_paths", return_value=["socket"]), \
                patch.object(browser, "_request_path", return_value=deepcopy(captured)), \
                patch.object(browser, "_attach_title_placements") as titles, \
                patch.object(browser, "_attach_desktop_placements", side_effect=AssertionError("markers")):
            result = browser.capture_browser(names=["Life"], by_title=True)
        titles.assert_called_once()
        self.assertTrue(result["available"])


if __name__ == "__main__":
    unittest.main()
