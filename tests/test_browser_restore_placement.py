"""A failed tab load must not strand an identified Chrome window."""
import unittest
from unittest.mock import patch

from workspace_state.browser import BrowserUnavailable, restore_browser


class BrowserRestorePlacementTests(unittest.TestCase):
    def test_unverified_window_is_placed_but_remains_failed_and_is_never_closed(self):
        chrome = {'profiles': [{'profile': 'Default', 'windows': [{
            'id': 'saved', 'workspace': 'Work', 'workspace_index': 1,
            'monitor': {'index': 0}, 'geometry': {}, 'state': 'normal',
            'tabs': [{'url': 'https://example.com/saved'}],
        }]}]}
        for created in (False, True):
            for outcome in (True, False, BrowserUnavailable('placement unavailable')):
                with (
                    self.subTest(created=created, outcome=outcome),
                    patch('workspace_state.browser.remap_monitor', side_effect=lambda value: value),
                    patch('workspace_state.browser.remap_workspace', side_effect=lambda value: value),
                    patch('workspace_state.browser.expect_window', return_value='expect-1'),
                    patch('workspace_state.browser.cancel_expected_window') as cancel,
                    patch('workspace_state.browser.expected_window_status') as status,
                    patch('workspace_state.browser.request_browser', return_value={
                        'window_id': 42, 'created': created, 'reused': not created,
                        'urls_restored': False, 'url_errors': ['Tab 1 redirected to login'],
                    }) as request,
                    patch('workspace_state.browser._place_browser_window',
                          side_effect=outcome if isinstance(outcome, Exception) else None,
                          return_value=outcome) as place,
                ):
                    result = restore_browser(chrome)[0]
                self.assertFalse(result.success)
                self.assertIn('exact tab URLs could not be verified: Tab 1 redirected to login', result.message)
                self.assertIn('window placed' if outcome is True else 'placement failed', result.message)
                self.assertIn('window preserved', result.message)
                self.assertEqual(place.call_args.kwargs['chrome_window_id'], 42)
                self.assertEqual(place.call_args.kwargs['placement']['workspace'], 1)
                cancel.assert_called_once_with('expect-1')
                status.assert_not_called()
                self.assertEqual(request.call_count, 1, 'unverified windows must never be closed')
                self.assertEqual(request.call_args.args[0], 'restore_window')

    def test_url_failure_does_not_prevent_placing_later_windows(self):
        chrome = {'profiles': [{'profile': 'Default', 'windows': [{
            'id': name, 'workspace': 'Work', 'workspace_index': 1,
            'monitor': {'index': 0}, 'geometry': {}, 'tabs': [],
        } for name in ('first', 'second')]}]}
        with (
            patch('workspace_state.browser.remap_monitor', side_effect=lambda value: value),
            patch('workspace_state.browser.remap_workspace', side_effect=lambda value: value),
            patch('workspace_state.browser.expect_window', return_value='expect'),
            patch('workspace_state.browser.cancel_expected_window'),
            patch('workspace_state.browser.request_browser', side_effect=[
                {'window_id': 41, 'reused': True, 'urls_restored': False},
                {'window_id': 42, 'reused': True, 'urls_restored': True},
            ]),
            patch('workspace_state.browser._place_browser_window', return_value=True) as place,
        ):
            results = restore_browser(chrome)
        self.assertEqual([result.success for result in results], [False, True])
        self.assertEqual([call.kwargs['chrome_window_id'] for call in place.call_args_list], [41, 42])
