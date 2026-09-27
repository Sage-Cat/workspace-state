"""Native Chrome session settling ignores page activity, not tab identity."""
import unittest
from unittest.mock import patch

from workspace_state.browser import BrowserUnavailable, wait_for_browser_settle


class BrowserSettleTests(unittest.TestCase):
    def run_settle(self, request, *, profiles=None, timeout=1, stable_for=0.3):
        clock = [0.0]

        def sleep(delay):
            clock[0] = round(clock[0] + delay, 6)

        with (
            patch('workspace_state.browser.time.monotonic', side_effect=lambda: clock[0]),
            patch('workspace_state.browser.time.sleep', side_effect=sleep),
            patch('workspace_state.browser.request_browser', side_effect=lambda *args, **kwargs: request(clock[0], **kwargs)),
        ):
            wait_for_browser_settle(profiles or {'Default'}, timeout=timeout, stable_for=stable_for)
        return clock[0]

    def test_background_loading_titles_focus_and_geometry_do_not_delay_settle(self):
        def request(now, **_kwargs):
            windows = [{
                'id': 1, 'full_signature': 'ordered-exact-tabs', 'tabs': ['https://example.com/saved'],
                'active_title': f'Updated at {now}', 'focused': now % 0.2 == 0,
                'state': 'normal' if now % 0.2 == 0 else 'maximized',
                'bounds': {'left': now}, 'urls_loaded': now % 0.2 == 0,
                'tab_states': [{'status': 'loading' if now % 0.2 == 0 else 'unloaded'}],
            }, {'id': 2, 'full_signature': 'other-tabs', 'tabs': ['chrome://newtab/']}]
            return windows if now % 0.2 == 0 else list(reversed(windows))

        self.assertEqual(self.run_settle(request), 0.3)

    def test_window_membership_or_exact_tab_signature_changes_restart_stability(self):
        for field in ('id', 'full_signature', 'tabs'):
            with self.subTest(field=field):
                def request(now, **_kwargs):
                    window = {'id': 1, 'full_signature': 'initial-tabs', 'tabs': ['https://example.com/one']}
                    if now >= 0.2:
                        window[field] = {'id': 2, 'full_signature': 'changed-tabs', 'tabs': ['https://example.com/two']}[field]
                    return [window]

                self.assertEqual(self.run_settle(request), 0.5)

    def test_timeout_reports_only_profiles_whose_identity_keeps_changing(self):
        def request(now, *, profile, **_kwargs):
            return [{'id': 1, 'full_signature': str(now) if profile == 'Default' else 'stable'}]

        with self.assertRaisesRegex(BrowserUnavailable, r'profile\(s\): Default$'):
            self.run_settle(request, profiles={'AlreadyStable', 'Default'}, timeout=0.5, stable_for=0.2)

    def test_malformed_window_lists_fail_without_waiting(self):
        for invalid in (None, {}, [None], [{'full_signature': 'missing-id'}]):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(BrowserUnavailable, 'invalid window list'):
                self.run_settle(lambda _now, **_kwargs: invalid)

    def test_empty_native_session_and_lazy_tabs_can_settle(self):
        for windows in ([], [{'id': 1, 'full_signature': 'saved', 'urls_loaded': False,
                               'tab_states': [{'status': 'unloaded', 'discarded': True}]}]):
            with self.subTest(windows=windows):
                self.assertEqual(self.run_settle(lambda _now, **_kwargs: windows), 0.3)
