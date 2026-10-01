import unittest
from pathlib import Path
from unittest.mock import patch
from workspace_state import browser


class BrowserQuiescenceTests(unittest.TestCase):
    def run_wait(self, responses=(), *, windows=1, paths=1, available=True, release_expired=False):
        clock = [0.0]
        shell = {'available': available, 'windows': [{'id': i, 'app_id': 'google-chrome'} for i in range(windows)]}
        with patch.object(browser, '_host_paths', return_value=[Path(f'fake-{i}') for i in range(paths)]), patch.object(browser, 'capture_shell', return_value=shell), patch.object(browser, '_request_path', side_effect=responses) as request, patch.object(browser.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(browser.time, 'sleep', side_effect=lambda delta: clock.__setitem__(0, clock[0] + delta)):
            browser.wait_for_quiescence(timeout=.5, release_expired=release_expired)
        return request

    def status(self, mutations=0, identifications=0, windows=1):
        return {'capabilities': ['native_mutation_status'], 'active_mutations': mutations,
                'active_identifications': identifications, 'window_count': windows}

    def test_no_chrome_needs_no_companion_rpc(self):
        self.run_wait(windows=0, paths=0).assert_not_called()

    def test_waits_for_request_and_identification_completion(self):
        request = self.run_wait([self.status(mutations=1), self.status(identifications=1), self.status()])
        self.assertEqual(request.call_count, 3)
        self.assertEqual({call.args[1] for call in request.call_args_list}, {'ping'})

    def test_missing_or_unresponsive_companion_blocks_capture(self):
        with self.assertRaises(browser.BrowserUnavailable):
            self.run_wait(paths=0)
        with self.assertRaisesRegex(browser.BrowserUnavailable, 'unreachable'):
            self.run_wait(browser.BrowserUnavailable('unreachable'))

    def test_old_protocol_or_orphan_identification_fails_closed(self):
        with self.assertRaisesRegex(browser.BrowserUnavailable, 'lacks native mutation'):
            self.run_wait([{}] * 3)
        with self.assertRaisesRegex(browser.BrowserUnavailable, 'identification lease'):
            self.run_wait([self.status(identifications=1)] * 3)

    def test_partial_profile_coverage_and_unknown_shell_fail_closed(self):
        with self.assertRaisesRegex(browser.BrowserUnavailable, 'Not every native'):
            self.run_wait([self.status()] * 3, windows=2)
        with self.assertRaisesRegex(browser.BrowserUnavailable, 'GNOME window state'):
            self.run_wait([self.status(windows=0)] * 3, windows=0, available=False)

    def test_barrier_cleans_only_expired_owned_leases_then_reobserves(self):
        expired = dict(self.status(identifications=1),
                       capabilities=['native_mutation_status', 'identification_lease_lifecycle'],
                       expired_identifications=1)
        request = self.run_wait([expired, {'released': 1}, self.status()], release_expired=True)
        self.assertEqual([c.args[1] for c in request.call_args_list],
                         ['ping', 'release_expired_identifications', 'ping'])

    def test_read_only_observation_never_cleans_expired_leases(self):
        expired = dict(self.status(identifications=1),
                       capabilities=['native_mutation_status', 'identification_lease_lifecycle'],
                       expired_identifications=1)
        with self.assertRaises(browser.BrowserUnavailable):
            self.run_wait([expired] * 3)

    def test_cleanup_response_does_not_prove_quiescence_and_is_not_repeated(self):
        expired = dict(self.status(identifications=1),
                       capabilities=['native_mutation_status', 'identification_lease_lifecycle'],
                       expired_identifications=1)
        with self.assertRaises(browser.BrowserUnavailable):
            self.run_wait([expired, {'released': 1}, expired, expired], release_expired=True)

    def test_live_unknown_or_busy_leases_cannot_be_released_by_barrier(self):
        for mutations, expired in [(0, 0), (1, 1), (0, True), (0, 2)]:
            status = dict(self.status(mutations=mutations, identifications=1),
                          capabilities=['native_mutation_status', 'identification_lease_lifecycle'],
                          expired_identifications=expired)
            with self.subTest(mutations=mutations, expired=expired), self.assertRaises(browser.BrowserUnavailable):
                self.run_wait([status] * 3, release_expired=True)
