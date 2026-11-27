"""Periodic Chrome capture identifies windows by title alone; synthetic data only."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from workspace_state import browser, cli
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

    def test_missing_extra_and_unpairable_titles_fail_the_capture(self):
        cases = [
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

    def same_title_pair(self, first, second, *, bounds=((1000, 700), (1000, 700)), focused=(False, False),
                        states=("normal", "normal")):
        windows = [chrome_window(11, "New Tab", "chrome://newtab/"), chrome_window(12, "New Tab", "chrome://newtab/")]
        for window, (width, height), flag, state in zip(windows, bounds, focused, states):
            window.update(bounds={"left": 0, "top": 0, "width": width, "height": height}, focused=flag, state=state)
        shell = [first, second]
        profiles = [profile(*windows)]
        return profiles, shell

    def test_same_titled_windows_are_told_apart_by_size(self):
        first, second = native(1, "New Tab - Google Chrome", 0), native(2, "New Tab - Google Chrome", 1)
        first["geometry"] = dict(first["geometry"], width=800)
        second["state"] = first["state"] = "normal"
        profiles, shell = self.same_title_pair(first, second, bounds=((1000, 700), (803, 700)))
        self.capture(profiles, [shell, shell])
        one, two = profiles[0]["windows"]
        self.assertEqual((one["workspace_index"], two["workspace_index"]), (1, 0))
        self.assertNotIn("placement_ambiguous", one)

    def test_same_titled_windows_are_told_apart_by_focus_in_both_samples(self):
        first, second = native(1, "New Tab - Google Chrome", 0), native(2, "New Tab - Google Chrome", 1)
        second["active"] = True
        profiles, shell = self.same_title_pair(first, second, focused=(True, False))
        summaries = [{"id": 11, "active_title": "New Tab", "focused": True},
                     {"id": 12, "active_title": "New Tab", "focused": False}]
        self.capture(profiles, [shell, shell], summaries=summaries)
        self.assertEqual([w["workspace_index"] for w in profiles[0]["windows"]], [1, 0])

    def test_focus_changing_between_samples_fails_the_capture(self):
        first, second = native(1, "New Tab - Google Chrome", 0), native(2, "New Tab - Google Chrome", 1)
        second["active"] = True
        profiles, shell = self.same_title_pair(first, second, focused=(True, False))
        summaries = [{"id": 11, "active_title": "New Tab", "focused": False},
                     {"id": 12, "active_title": "New Tab", "focused": True}]
        with self.assertRaisesRegex(BrowserUnavailable, "changed while matching"):
            self.capture(profiles, [shell, shell], summaries=summaries)

    def test_same_titled_windows_with_one_placement_need_no_distinction(self):
        first, second = native(1, "New Tab - Google Chrome", 1), native(2, "New Tab - Google Chrome", 1)
        profiles, shell = self.same_title_pair(first, second)
        self.capture(profiles, [shell, shell])
        self.assertEqual([w["workspace_index"] for w in profiles[0]["windows"]], [1, 1])

    def test_an_indistinguishable_window_is_set_aside_without_failing_the_others(self):
        windows = [chrome_window(11, "New Tab", "chrome://newtab/"), chrome_window(12, "New Tab", "chrome://newtab/"),
                   chrome_window(13, "Inbox", "https://mail.example.test/")]
        shell = [native(1, "New Tab - Google Chrome", 0), native(2, "New Tab - Google Chrome", 2),
                 native(3, "Inbox - Google Chrome", 1)]
        profiles = [profile(*windows)]
        self.capture(profiles, [shell, shell])
        one, two, mail = profiles[0]["windows"]
        for window in (one, two):
            self.assertIs(window["placement_ambiguous"], True)
            for key in ("workspace_index", "monitor", "geometry"):
                self.assertNotIn(key, window)
        self.assertEqual((mail["workspace_index"], mail.get("placement_ambiguous")), (1, None))
        self.assertEqual(cli._browser_problems({"browsers": {cli.BROWSER_KEY: {
            "available": True, "profiles": profiles}}}), [])

    @staticmethod
    def view(title, width=1000, height=700, **fields):
        return {"title": title, "state": "normal", "focused": False,
                "bounds": {"left": 0, "top": 0, "width": width, "height": height}, **fields}

    @staticmethod
    def shell_window(native_id, title, workspace, width=1000, height=700):
        return dict(native(native_id, f"{title} - Google Chrome", workspace), state="normal",
                    geometry={"x": 0, "y": 0, "width": width, "height": height})

    def test_a_pair_stays_ambiguous_however_many_other_windows_exist(self):
        views = [self.view("Docs"), self.view("Docs")] + [self.view("New Tab") for _ in range(12)]
        natives = ([self.shell_window(1, "Docs", 0), self.shell_window(2, "Docs", 1)]
                   + [self.shell_window(10 + index, "New Tab", 2) for index in range(12)])
        matches, unresolved = browser._title_matches(views, natives)
        self.assertEqual(unresolved, {0, 1})
        self.assertEqual(sorted(matches), list(range(2, 14)))

    def test_a_size_offset_that_fits_nobody_decides_nothing(self):
        views = [self.view("New Tab", 1000, 700), self.view("New Tab", 980, 680)]
        natives = [self.shell_window(1, "New Tab", 0, 980, 680), self.shell_window(2, "New Tab", 1, 960, 660)]
        self.assertEqual(browser._title_matches(views, natives), ({}, {0, 1}))

    def test_size_decides_only_when_every_pair_agrees(self):
        views = [self.view("New Tab", 1000, 700), self.view("New Tab", 800, 600)]
        natives = [self.shell_window(1, "New Tab", 0, 800, 600), self.shell_window(2, "New Tab", 1, 1000, 700)]
        matches, unresolved = browser._title_matches(views, natives)
        self.assertEqual((matches[0]["id"], matches[1]["id"], unresolved), (2, 1, set()))

    def test_interchangeable_windows_elsewhere_do_not_stop_size_from_deciding(self):
        docs = [self.view("Docs", 1200, 800), self.view("Docs", 900, 600)]
        natives = [self.shell_window(1, "Docs", 0, 1200, 800), self.shell_window(2, "Docs", 1, 900, 600)]
        for extra in (2, 3):
            with self.subTest(extra=extra):
                views = docs + [self.view("New Tab") for _ in range(extra)]
                shell = natives + [self.shell_window(10 + index, "New Tab", 2) for index in range(extra)]
                matches, unresolved = browser._title_matches(views, shell)
                self.assertEqual((unresolved, matches[0]["id"], matches[1]["id"]), (set(), 1, 2))

    def test_many_windows_and_an_impossible_pairing_are_decided_quickly(self):
        count = 40
        views = [self.view("T") for _ in range(count)] + [self.view("U")]
        natives = [self.shell_window(index, "T", 0) for index in range(count - 1)]
        natives += [self.shell_window(100, "U", 0), self.shell_window(101, "Loading - U", 0)]
        with self.assertRaises(BrowserUnavailable):
            browser._title_matches(views, natives)
        natives = [self.shell_window(index, "T", index % 3) for index in range(count)] + [self.shell_window(100, "U", 0)]
        matches, unresolved = browser._title_matches(views, natives)
        self.assertEqual(len(matches) + len(unresolved), count + 1)
        self.assertEqual(matches[count]["id"], 100)

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
