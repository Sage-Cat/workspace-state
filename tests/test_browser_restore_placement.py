"""A failed tab load must not strand an identified Chrome window."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from workspace_state import browser
from workspace_state.browser import (BrowserPlacementPending, BrowserUnavailable,
                                     _place_browser_window,
                                     restore_browser)


class BrowserRestorePlacementTests(unittest.TestCase):
    def test_lost_identification_reply_releases_locally_owned_token(self):
        def request(action, _payload, **_kwargs):
            if action == 'identify_window':
                raise BrowserUnavailable('identify reply lost')
            return {'released': True}
        with (
            patch.object(browser.uuid, 'uuid4', return_value=SimpleNamespace(hex='owned-token')),
            patch.object(browser, 'request_browser', side_effect=request) as rpc,
            patch.object(browser, 'capture_shell') as capture,
        ):
            with self.assertRaisesRegex(BrowserUnavailable, 'identify reply lost'):
                browser._identify_native_window(profile='Default', chrome_window_id=42, app_id='google-chrome')
        self.assertEqual([call.args[0] for call in rpc.call_args_list],
                         ['identify_window', 'release_window_identification'])
        self.assertEqual(rpc.call_args.args[1], {'window_id': 42, 'token': 'owned-token', 'focus': False})
        self.assertEqual(rpc.call_args.kwargs['timeout'], 2)
        capture.assert_not_called()

    def test_identification_failure_releases_after_shell_or_focus_exception(self):
        for failure in ('shell', 'focus'):
            with self.subTest(failure=failure):
                def request(action, payload, **_kwargs):
                    if action == 'identify_window':
                        return {'window_id': 42, 'token': payload['token'], 'marker_tab_id': 99}
                    if action == 'focus_window':
                        raise BrowserUnavailable('focus failed')
                    return {'released': True}
                with (
                    patch.object(browser, 'request_browser', side_effect=request) as rpc,
                    patch.object(browser, 'capture_shell',
                                 side_effect=RuntimeError('shell failed') if failure == 'shell' else None,
                                 return_value={'windows': []}),
                    patch.object(browser.time, 'sleep'),
                ):
                    with self.assertRaisesRegex(RuntimeError, failure + ' failed'):
                        browser._identify_native_window(profile='Default', chrome_window_id=42, app_id='google-chrome')
                self.assertEqual(rpc.call_args.args[0], 'release_window_identification')
                self.assertEqual(rpc.call_args.args[1]['marker_tab_id'], 99)

    def test_mismatched_identification_reply_cannot_redirect_cleanup(self):
        for reply in (None, {'window_id': 43, 'token': 'owned-token'},
                      {'window_id': 42, 'token': 'another-token'}):
            with (
                self.subTest(reply=reply),
                patch.object(browser.uuid, 'uuid4', return_value=SimpleNamespace(hex='owned-token')),
                patch.object(browser, 'request_browser', side_effect=[reply, {'released': True}]) as rpc,
                patch.object(browser, 'capture_shell') as capture,
            ):
                with self.assertRaisesRegex(BrowserUnavailable, 'invalid window identification'):
                    browser._identify_native_window(profile='Default', chrome_window_id=42, app_id='google-chrome')
                self.assertEqual(rpc.call_args.args[1], {'window_id': 42, 'token': 'owned-token', 'focus': False})
                capture.assert_not_called()

    def test_unconfirmed_identification_cleanup_blocks_placement_even_if_retry_succeeds(self):
        for response in ({'released': False, 'reason': 'marker remains'}, {}, None):
            with (
                self.subTest(response=response),
                patch.object(browser, '_identify_native_window',
                             return_value=(9, {'window_id': 42, 'token': 'owned-token'})),
                patch.object(browser, 'request_browser', side_effect=[response, {'released': True}]) as rpc,
                patch.object(browser, 'capture_shell') as capture,
                patch.object(browser, 'move_window_result') as move,
            ):
                with self.assertRaisesRegex(BrowserUnavailable, 'cleanup was not confirmed'):
                    _place_browser_window(profile='Default', chrome_window_id=42,
                                          app_id='google-chrome', placement={'workspace': 0})
                self.assertEqual(rpc.call_count, 2)
                capture.assert_not_called()
                move.assert_not_called()


    def test_restore_identifies_inactive_normal_window_without_focus_or_workspace_change(self):
        calls = []
        def request(action, payload, **kwargs):
            calls.append((action, payload))
            if action == 'identify_window':
                return {'window_id': 42, 'token': payload['token'], 'marker_tab_id': 99}
            self.assertEqual(action, 'release_window_identification')
            return {'released': True}
        shell = {'available': True, 'active_workspace': 0,
                 'capabilities': ['staged_placement_handoff'],
                 'windows': [{'id': 9, 'app_ids': ['google-chrome'], 'workspace': 4,
                              'active': False, 'title': 'owned-token'}]}
        with patch.object(browser.uuid, 'uuid4', return_value=SimpleNamespace(hex='owned-token')), patch.object(
            browser, 'request_browser', side_effect=request), patch.object(
            browser, 'capture_shell', return_value=shell), patch.object(browser.time, 'sleep'), patch.object(
            browser, 'move_window_result', return_value={'status': 'verified'}) as move:
            self.assertTrue(_place_browser_window(profile='Default', chrome_window_id=42,
                                                 app_id='google-chrome', placement={'workspace': 3}))
        self.assertEqual([action for action, _ in calls], ['identify_window', 'release_window_identification'])
        self.assertTrue(all(payload['focus'] is False for _, payload in calls))
        move.assert_called_once_with(9, {'workspace': 3, 'stage_before_handoff': True})

    def test_marker_cleanup_precedes_the_single_final_handoff(self):
        events = []
        def request(action, payload, **kwargs):
            events.append(action)
            self.assertFalse(payload['focus'])
            return {'released': True}
        def move(_identifier, target):
            events.append(('move', target['workspace']))
            return {'status': 'verified', 'token': 'request', 'handoff_owned': True}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={'active_workspace': 0, 'capabilities': ['staged_placement_handoff']}),
            patch('workspace_state.browser.request_browser', side_effect=request),
            patch('workspace_state.browser.move_window_result', side_effect=move),
        ):
            self.assertTrue(_place_browser_window(profile='Default', chrome_window_id=42,
                                                 app_id='google-chrome', placement={'workspace': 3}))
        self.assertEqual(events, ['release_window_identification', ('move', 3)])

    def test_unowned_receipt_cannot_prove_final_workspace(self):
        target = {'workspace': 3, 'monitor': 0, 'state': 'normal'}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={'active_workspace': 0, 'capabilities': ['staged_placement_handoff']}),
            patch('workspace_state.browser.move_window_result', return_value={
                'status': 'accepted', 'token': 'intermediate-stage',
            }) as move,
            patch('workspace_state.browser.request_browser', return_value={'released': True}) as request,
        ):
            with self.assertRaises(BrowserUnavailable) as raised:
                _place_browser_window(profile='Default', chrome_window_id=42,
                                      app_id='google-chrome', placement=target)
        self.assertNotIsInstance(raised.exception, BrowserPlacementPending)
        self.assertIn('without owning its final workspace handoff', str(raised.exception))
        self.assertEqual(move.call_count, 1)
        self.assertEqual(move.call_args.args[1]['workspace'], 3)
        request.assert_called_once_with('release_window_identification',
                                        {'window_id': 42, 'focus': False}, profile='Default', timeout=2)


    def test_locked_desktop_submits_one_owned_final_target_without_temporary_receipt(self):
        target = {'workspace': 3, 'monitor': 0, 'state': 'normal'}
        with (
            patch('workspace_state.browser._identify_native_window', return_value=(9, {'window_id': 42})),
            patch('workspace_state.browser.capture_shell', return_value={
                'active_workspace': 0, 'capabilities': ['staged_placement_handoff'],
                'monitor_policy': {'screen_unavailable': True}}),
            patch('workspace_state.browser.move_window_result', return_value={
                'status': 'deferred', 'token': 'final-workspace', 'handoff_owned': True}) as move,
            patch('workspace_state.browser.request_browser', return_value={'released': True}),
        ):
            with self.assertRaises(BrowserPlacementPending) as raised:
                _place_browser_window(profile='Default', chrome_window_id=42,
                                      app_id='google-chrome', placement=target)
        self.assertEqual(raised.exception.request_id, 'native-handoff:final-workspace')
        move.assert_called_once_with(9, dict(target, stage_before_handoff=True))

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
