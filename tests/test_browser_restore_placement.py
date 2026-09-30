"""A failed tab load must not strand an identified Chrome window."""
import unittest
from unittest.mock import patch

from workspace_state.browser import (BrowserPlacementPending, BrowserUnavailable,
                                     _place_browser_window, _wait_for_native_placement,
                                     restore_browser)


class BrowserRestorePlacementTests(unittest.TestCase):
    def test_transient_frame_match_does_not_finish_before_resize_settles(self):
        clock = [0.0]
        def capture():
            return {'windows': [{'id': 9, 'width': 800 if clock[0] < .05 or clock[0] >= .3 else 882}]}
        with (
            patch('workspace_state.browser.time.monotonic', side_effect=lambda: clock[0]),
            patch('workspace_state.browser.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)),
            patch('workspace_state.browser.capture_shell', side_effect=capture),
            patch('workspace_state.browser._window_matches_resolved_placement',
                  side_effect=lambda window, target: window['width'] == target['width']),
        ):
            self.assertTrue(_wait_for_native_placement(9, {'width': 800}, 1))
        self.assertGreaterEqual(clock[0], .7)
        self.assertLess(clock[0], 1)

    def test_unstable_frame_cannot_extend_the_placement_deadline(self):
        clock = [0.0]
        with (
            patch('workspace_state.browser.time.monotonic', side_effect=lambda: clock[0]),
            patch('workspace_state.browser.time.sleep', side_effect=lambda delay: clock.__setitem__(0, clock[0] + delay)),
            patch('workspace_state.browser.capture_shell', return_value={'windows': [{'id': 9}]}),
            patch('workspace_state.browser._window_matches_resolved_placement', return_value=True),
        ):
            self.assertFalse(_wait_for_native_placement(9, {}, .2))
        self.assertLessEqual(clock[0], .25)

    def test_marker_cleanup_precedes_both_staging_and_final_handoff(self):
        events = []
        def request(action, payload, **kwargs):
            events.append(action)
            self.assertFalse(payload['focus'])
        def move(_identifier, target):
            events.append(('move', target['workspace']))
            return {'status': 'accepted', 'token': 'request'}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={'active_workspace': 0}),
            patch('workspace_state.browser.request_browser', side_effect=request),
            patch('workspace_state.browser.move_window_result', side_effect=move),
            patch('workspace_state.browser._wait_for_native_placement', return_value=True),
        ):
            self.assertTrue(_place_browser_window(profile='Default', chrome_window_id=42,
                                                 app_id='google-chrome', placement={'workspace': 3}))
        self.assertEqual(events, ['release_window_identification', ('move', 0), ('move', 3)])

    def test_late_staging_receipt_cannot_prove_unsubmitted_final_workspace(self):
        target = {'workspace': 3, 'monitor': 0, 'state': 'normal'}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={'active_workspace': 0}),
            patch('workspace_state.browser.move_window_result', return_value={
                'status': 'accepted', 'token': 'intermediate-stage',
            }) as move,
            patch('workspace_state.browser._wait_for_native_placement', return_value=False),
            patch('workspace_state.browser.request_browser') as request,
        ):
            with self.assertRaises(BrowserUnavailable) as raised:
                _place_browser_window(profile='Default', chrome_window_id=42,
                                      app_id='google-chrome', placement=target)
        self.assertNotIsInstance(raised.exception, BrowserPlacementPending)
        self.assertIn('final workspace placement was not submitted', str(raised.exception))
        self.assertEqual(move.call_count, 1)
        self.assertEqual(move.call_args.args[1]['workspace'], 0)
        request.assert_called_once_with('release_window_identification',
                                        {'window_id': 42, 'focus': False}, profile='Default', timeout=2)

    def test_only_submitted_final_workspace_receipt_can_remain_pending(self):
        target = {'workspace': 3, 'monitor': 0, 'state': 'normal'}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={'active_workspace': 0}),
            patch('workspace_state.browser.move_window_result', side_effect=[
                {'status': 'accepted', 'token': 'intermediate-stage'},
                {'status': 'accepted', 'token': 'final-workspace'},
            ]) as move,
            patch('workspace_state.browser._wait_for_native_placement', side_effect=[True, False]),
            patch('workspace_state.browser.request_browser'),
        ):
            with self.assertRaises(BrowserPlacementPending) as raised:
                _place_browser_window(profile='Default', chrome_window_id=42,
                                      app_id='google-chrome', placement=target)
        self.assertEqual(raised.exception.request_id, 'final-workspace')
        self.assertEqual([call.args[1]['workspace'] for call in move.call_args_list], [0, 3])

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
