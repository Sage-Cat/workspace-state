from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_state import cli, operations
from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult, BrowserUnavailable
import chrome_native
import test_browser_fragment_preservation as fixtures

SAVED_URL = "https://{}.example.test/{}"


def saved_window(label, workspace):
    return fixtures.overlap_window(label, workspace, [SAVED_URL.format(label, "a"), SAVED_URL.format(label, "b")])


def native_window(label, runtime):
    return fixtures.overlap_window(None, 0, [SAVED_URL.format(label, "a"), SAVED_URL.format(label, "b")],
                                   runtime=runtime)


def profile(name, windows):
    return {"profile": name, "profile_directory": name, "app_id": "google-chrome", "windows": windows}


def snapshot_of(*profiles, **scope):
    return {**scope, "browsers": {"google_chrome": {"available": True, "errors": [], "profiles": list(profiles)}}}


class Harness:
    """Startup of the Chrome category against a synthetic companion."""

    def __init__(self, test, native, connected, *, restore=None, launch=None, info=None, real_launch=False):
        self.real_launch = real_launch
        self.test, self.native, self.connected, self.info = test, native, connected, info
        self.restore, self.launch = restore, launch

    def __enter__(self):
        self.stack = stack = ExitStack()
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        status = root / "status.json"
        status.write_text('{"operation_state":"running"}')
        owner = Mock(mode="startup")
        owner.matches.return_value = True
        owner.to_dict.return_value = {"operation_id": "synthetic-operation"}
        stack.enter_context(patch.object(operations, "current", return_value=owner))
        stack.enter_context(patch.object(cli, "status_path", return_value=status))
        stack.enter_context(patch.object(cli, "_startup_directory", return_value=root))
        stack.enter_context(patch.object(cli, "connected_profiles", side_effect=lambda: list(self.connected)))
        if self.real_launch:
            self.launch = None
        else:
            self.launch = stack.enter_context(patch.object(
                cli, "ensure_browser_profiles",
                **({"side_effect": self.launch} if self.launch else {"return_value": []})))
        stack.enter_context(patch.object(cli, "wait_for_browser_settle"))
        stack.enter_context(patch.object(cli, "browser_companion_info", return_value=self.info or {
            "protocol_version": 2, "capabilities": list(BROWSER_REQUIRED_CAPABILITIES)}))
        self.calls, self.profiles = [], []

        def companion(action, params, **kwargs):
            self.calls.append(action)
            self.profiles.append((action, kwargs.get("profile")))
            if action == "capture":
                return deepcopy(self.native[kwargs["profile"]])
            return {"exists": False}

        stack.enter_context(patch.object(cli, "request_browser", side_effect=companion))
        self.restore = stack.enter_context(patch.object(cli, "restore_browser", **(
            {"side_effect": self.restore} if self.restore else
            {"return_value": [BrowserRestoreResult("Placed window", True)]})))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def startup(self, snapshot, **args):
        return cli._restore_browsers(snapshot, Namespace(
            workspace=None, dry_run=False, no_place=False, **args), start_browser=True)


class BrowserPartialRestoreTests(unittest.TestCase):
    def test_partial_disabled_profile_uses_background_launch_without_blocking_other_profiles(self):
        snapshot = snapshot_of(profile("Active", [saved_window("one", 1)]), profile("Paused", [saved_window("two", 2)]),
                               _workspace_switch_exclusions=True, _workspace_switch_blocked_profiles={"Paused"})
        native = {"Active": profile("Active", [native_window("one", 11)])}
        with Harness(self, native, ["Active"], launch=[[], BrowserUnavailable("profile unavailable")]) as h:
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*Paused/two.*profile unavailable"):
                h.startup(snapshot)
        self.assertEqual(h.launch.call_args_list[0].args[0]["profiles"][0]["profile"], "Active")
        self.assertEqual(set(h.launch.call_args_list[0].kwargs), {"unavailable"})
        self.assertEqual(h.launch.call_args_list[1].args[0]["profiles"][0]["profile"], "Paused")
        self.assertEqual(h.launch.call_args_list[1].kwargs, {"workspace_switch": True})
        h.restore.assert_called_once()
        self.assertEqual(h.restore.call_args.args[0]["profiles"][0]["profile"], "Active")
        self.assertNotIn("close_restored_window", h.calls)

    def test_completed_off_uses_background_launch_or_reuses_an_already_connected_profile(self):
        snapshot = snapshot_of(profile("Default", [saved_window("active", 1)]), _workspace_switch_exclusions=True,
                               _workspace_switch_cold_profiles={"Default"}, _workspace_switch_blocked_profiles=set())
        native = {"Default": profile("Default", [native_window("active", 11)])}
        for connected_at_start in (False, True):
            connected = ["Default"] if connected_at_start else []

            def background_start(chrome, *, workspace_switch):
                self.assertTrue(workspace_switch)
                self.assertEqual(chrome["profiles"][0]["profile"], "Default")
                connected.append("Default")
                return ["Default"]

            with self.subTest(connected=connected_at_start), \
                    Harness(self, native, connected, launch=background_start) as h:
                h.startup(snapshot)
                self.assertEqual(h.launch.call_count, int(not connected_at_start))
                self.assertNotIn("list_windows", h.calls)
                h.restore.assert_called_once()

    def test_login_with_a_parked_workspace_pairs_chrome_native_session_with_saved_placement(self):
        native = {"Default": profile("Default", [native_window("active", 11)]),
                  "Unrelated": profile("Unrelated", [native_window("other", 12)])}
        for name, scope in (("enabled windows", {"_workspace_switch_cold_profiles": set(),
                                                  "_workspace_switch_blocked_profiles": set()}),
                            ("scoped profile", {"_workspace_switch_cold_profiles": {"Default"}})):
            snapshot = snapshot_of(profile("Default", [saved_window("active", 1)]),
                                   _workspace_switch_exclusions=True, **scope)
            with self.subTest(name), Harness(self, native, ["Default", "Unrelated"]) as h:
                h.startup(snapshot)
                # Pairing reads only the profiles of the filtered recipe.
                self.assertEqual(h.profiles[0], ("capture", "Default"))
                window = h.restore.call_args.args[0]["profiles"][0]["windows"][0]
                self.assertEqual(window["_reconcile_window_id"], 11)
                self.assertNotIn("list_windows", h.calls)
                self.assertNotIn("close_restored_window", h.calls)

    def test_unpaired_enabled_profile_fails_its_own_windows_and_never_closes_native_ones(self):
        snapshot = snapshot_of(profile("Default", [saved_window("active", 1)]),
                               profile("Parked", [saved_window("other", 2), saved_window("another", 3)]),
                               _workspace_switch_exclusions=True, _workspace_switch_cold_profiles=set(),
                               _workspace_switch_blocked_profiles=set())
        elsewhere = fixtures.overlap_window(None, 0, ["https://elsewhere.example.test/"], runtime=21)
        native = {"Default": profile("Default", [native_window("active", 11)]),
                  "Parked": profile("Parked", [elsewhere])}
        with Harness(self, native, ["Default", "Parked"]) as h:
            with self.assertRaisesRegex(RuntimeError, "Restored 1/3.*Parked/another.*Parked/other"):
                h.startup(snapshot)
        h.restore.assert_called_once()
        self.assertEqual(h.restore.call_args.args[0]["profiles"][0]["profile"], "Default")
        self.assertNotIn("close_restored_window", h.calls)

    def test_a_profile_without_a_connected_companion_fails_only_its_own_windows_at_startup(self):
        snapshot = snapshot_of(profile("Active", [saved_window("one", 1)]), profile("Silent", [saved_window("two", 2)]))
        native = {"Active": profile("Active", [native_window("one", 11)])}
        with Harness(self, native, ["Active"]) as h:
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*Silent/two.*not connected for profile 'Silent'"):
                h.startup(snapshot)
            h.restore.assert_called_once()
            self.assertEqual(h.restore.call_args.args[0]["profiles"][0]["profile"], "Active")
            self.assertNotIn(("capture", "Silent"), h.profiles)
        # A manual restore still refuses as a whole.
        with Harness(self, native, ["Active"]) as h:
            with self.assertRaisesRegex(RuntimeError, "not connected for profile"):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False, no_place=False,
                                                          category="browsers"))
            h.restore.assert_not_called()

    def test_a_profile_that_never_connects_fails_through_the_real_launcher_only_for_itself(self):
        from workspace_state import browser
        snapshot = snapshot_of(profile("Active", [saved_window("one", 1)]), profile("Silent", [saved_window("two", 2)]))
        native = {"Active": profile("Active", [native_window("one", 11)])}
        clock = iter(range(0, 10000, 5))
        with Harness(self, native, ["Active"], real_launch=True) as h, \
                patch.object(browser, "connected_profiles", side_effect=lambda: list(h.connected)), \
                patch.object(browser.shutil, "which", return_value="/usr/bin/google-chrome"), \
                patch.object(browser, "launch_graphical_service") as launch, \
                patch.object(browser.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(browser.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*Silent/two.*did not connect"):
                h.startup(snapshot)
            h.restore.assert_called_once()
            self.assertEqual(launch.call_count, 1)  # Only the missing profile was started.

    def test_several_silent_profiles_share_one_startup_deadline(self):
        from workspace_state import browser
        snapshot = snapshot_of(profile("Active", [saved_window("one", 1)]), profile("Silent1", [saved_window("two", 2)]),
                               profile("Silent2", [saved_window("three", 3)]))
        native = {"Active": profile("Active", [native_window("one", 11)])}
        clock = iter(range(0, 10000, 5))
        with Harness(self, native, ["Active"], real_launch=True) as h, \
                patch.object(browser, "connected_profiles", side_effect=lambda: list(h.connected)), \
                patch.object(browser.shutil, "which", return_value="/usr/bin/google-chrome"), \
                patch.object(browser, "launch_graphical_service") as launch, \
                patch.object(browser.time, "monotonic", side_effect=lambda: next(clock)), \
                patch.object(browser.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "Restored 1/3.*Silent1/two.*Silent2/three"):
                h.startup(snapshot)
            self.assertEqual(launch.call_count, 2)
            self.assertLessEqual(sleep.call_count, 4)  # One 15 s deadline, not one per profile.
            h.restore.assert_called_once()

    def test_failed_window_does_not_block_later_placement_or_inflate_progress(self):
        snapshot = snapshot_of(profile("Default", [saved_window("one", 1), saved_window("two", 2)]))
        native = {"Default": profile("Default", [native_window("one", 11), native_window("two", 12)])}
        results = [[BrowserRestoreResult("URLs redirected", False)], [BrowserRestoreResult("Placed second window", True)]]
        with Harness(self, native, ["Default"], restore=results) as h, \
                patch.object(cli, "update_stage") as report:
            with self.assertRaisesRegex(RuntimeError, "Restored 1/2.*URLs redirected"):
                h.startup(snapshot, login_status=True)
            self.assertEqual(h.restore.call_count, 2)
            self.assertEqual(report.call_args.kwargs, {"current": 1, "total": 2})


if __name__ == "__main__":
    unittest.main()
