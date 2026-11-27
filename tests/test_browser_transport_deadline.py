"""A dead Chrome native host costs one deadline per profile, not one per window."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli
from workspace_state.browser import BrowserHostStalled, BrowserRestoreResult, BrowserTransportUnavailable, BrowserUnavailable, _request_path
from workspace_state.provider_results import EvidenceState, ProviderRestoreError
from test_browser_partial_restore import Harness, native_window, profile, saved_window, snapshot_of


class TransportDeadlineTests(unittest.TestCase):
    def native(self, **labels):
        return {name: profile(name, [native_window(label, 10 + index) for index, label in enumerate(names)])
                for name, names in labels.items()}

    def test_first_transport_failure_fails_the_rest_of_its_profile_without_more_requests(self):
        snapshot = snapshot_of(
            profile("Down", [saved_window(f"d{index}", index) for index in range(4)]),
            profile("Up", [saved_window("u0", 1)]))

        def restore(chrome, **_kwargs):
            if chrome["profiles"][0]["profile"] == "Down":
                raise BrowserHostStalled("Chrome native host closed without a response")
            return [BrowserRestoreResult("Placed window", True)]

        with Harness(self, self.native(Down=["d0", "d1", "d2", "d3"], Up=["u0"]), ["Down", "Up"], restore=restore) as h:
            with self.assertRaises(ProviderRestoreError) as caught:
                h.startup(snapshot)
        profiles = [call.args[0]["profiles"][0]["profile"] for call in h.restore.call_args_list]
        self.assertEqual(profiles, ["Down", "Up"])  # one request for Down, then the next profile
        failed = [item for item in caught.exception.results if item.item_id.startswith("Down/")]
        self.assertEqual(len(failed), 4)
        for item in failed:
            self.assertEqual(item.identity.state, EvidenceState.FAILED)
            self.assertTrue(item.identity.retryable)
            self.assertIn("closed without a response", item.identity.detail)
        self.assertIn("Restored 1/5", str(caught.exception))

    def test_an_instant_connect_error_keeps_the_per_window_behaviour(self):
        snapshot = snapshot_of(profile("Default", [saved_window(f"w{index}", index) for index in range(3)]))
        with Harness(self, self.native(Default=["w0", "w1", "w2"]), ["Default"],
                     restore=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                         BrowserTransportUnavailable("Chrome native host is unavailable: refused"))) as h:
            with self.assertRaises(ProviderRestoreError) as caught:
                h.startup(snapshot)
        self.assertEqual(h.restore.call_count, 3)
        self.assertEqual(len(caught.exception.results), 3)

    def test_only_a_spent_deadline_or_a_silent_close_marks_the_host_stalled(self):
        missing = Path(tempfile.mkdtemp()) / "none.sock"
        with self.assertRaises(BrowserTransportUnavailable) as refused:
            _request_path(missing, "ping", timeout=1, check_operation=False)
        self.assertNotIsInstance(refused.exception, BrowserHostStalled)
        with patch("workspace_state.browser.time.monotonic", side_effect=[0, 100, 100, 100]), \
                self.assertRaises(BrowserHostStalled):
            _request_path(missing, "ping", timeout=1, check_operation=False)

    def test_other_errors_keep_the_per_window_behaviour(self):
        snapshot = snapshot_of(profile("Default", [saved_window(f"w{index}", index) for index in range(3)]))
        with Harness(self, self.native(Default=["w0", "w1", "w2"]), ["Default"],
                     restore=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                         BrowserUnavailable("window placement rejected"))) as h:
            with self.assertRaises(ProviderRestoreError) as caught:
                h.startup(snapshot)
        self.assertEqual(h.restore.call_count, 3)
        self.assertEqual(len(caught.exception.results), 3)


if __name__ == "__main__":
    unittest.main()
