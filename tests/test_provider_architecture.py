import copy
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import browser, native_host, restore, social_apps, vscode
from workspace_state.util import CommandError
from test_vscode import project, record, PLACEMENT


class ProviderArchitectureTests(unittest.TestCase):
    def test_native_host_removes_own_path_but_preserves_replacement(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'host.sock'
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                server.bind(str(path))
                identity = native_host._socket_identity(server)
                server.close()
                native_host._unlink_owned_socket(path, identity)
                self.assertFalse(path.exists())

    def test_unverified_shell_is_not_pristine_and_never_receives_text(self):
        state = {0: {'name': 'work', 'id': '@1', 'panes': {
            0: {'command': 'zsh', 'id': '%1', 'pid': 123, 'cwd': '/tmp'},
        }}}
        saved = {'windows': [{'index': 0, 'name': 'work', 'panes': [
            {'index': 0, 'cwd': '/tmp', 'codex': {'session_id': '11111111-1111-4111-8111-111111111111'}},
        ]}]}
        self.assertFalse(restore._pristine_bootstrap(state))
        with patch.object(restore, 'codex_for_pane', return_value=None), patch.object(restore, 'run') as run:
            with self.assertRaisesRegex(CommandError, 'input|bootstrap'):
                restore._reconcile_tmux('work', saved, state, dry_run=False, repair_processes=True)
            run.assert_not_called()

    def test_chrome_capture_requires_native_identity_not_same_title_geometry(self):
        windows = [{'id': f'window-{i}', 'runtime_window_id': i, 'tabs': [],
                    'bounds': {'left': 0, 'top': 0, 'width': 100, 'height': 100}} for i in (1, 2)]
        profiles = [{'profile': 'Default', 'windows': windows}]
        shell = {'windows': [{'id': i, 'app_id': 'google-chrome', 'title': 'Same',
                             'workspace': i - 10, 'monitor': 0,
                             'geometry': {'x': 0, 'y': 0, 'width': 100, 'height': 100}}
                            for i in (10, 11)]}
        with patch.object(browser, '_identify_native_window', side_effect=[(11, {}), (10, {})]), patch.object(browser, 'request_browser'):
            browser._attach_desktop_placements(profiles, shell, ['One', 'Two'])
        self.assertEqual([window['workspace'] for window in windows], ['Two', 'One'])
        self.assertTrue(all('runtime_window_id' not in window for window in windows))

    def test_chrome_ambiguous_capture_does_not_fall_back_to_geometry(self):
        profiles = [{'profile': 'Default', 'windows': [{'id': 'saved', 'runtime_window_id': 1, 'tabs': []}]}]
        with patch.object(browser, '_identify_native_window', side_effect=browser.BrowserUnavailable('ambiguous')), patch.object(browser, 'request_browser'):
            with self.assertRaises(browser.BrowserUnavailable):
                browser._attach_desktop_placements(profiles, {'windows': []}, [])

    def test_selected_vscode_bootstrap_profile_is_validated_before_launch(self):
        items = [project(), project(kind='empty', profile_id='gone', profile_name='Gone')]
        def verify(item):
            if item['profile']['id'] == 'gone':
                raise CommandError('profile missing')
        with patch.object(vscode, '_wait_for_live_windows', return_value=[]), patch.object(vscode, '_verify_profile', side_effect=verify), patch.object(vscode, '_check_resource'), patch.object(vscode, 'launch_graphical_service') as launch:
            with self.assertRaises(CommandError):
                vscode.restore_vscode(record(*items), no_place=True, timeout=.1)
        launch.assert_not_called()

    def test_missing_dirty_editor_recovery_is_not_verified(self):
        saved = project(dirty_count=1)
        saved['editor_uris'] = ['untitled:Missing']
        current = project()
        live = {**current, 'instance': 'one', 'endpoint': Path('/tmp/one.sock'), 'shell': {'id': 1, **PLACEMENT}}
        with patch.object(vscode, '_wait_for_live_windows', return_value=[live]), patch.object(vscode, '_request', side_effect=lambda _, method, **kw: {'ready': True} if method == 'probe' else current):
            with self.assertRaisesRegex(CommandError, 'recovery|editor'):
                vscode.restore_vscode(record(saved), no_place=True)

    def test_social_maximized_geometry_uses_compositor_state(self):
        target = dict(PLACEMENT, state='maximized')
        window = dict(target, id=1, app_ids=['slack'], geometry={**target['geometry'], 'height': 900})
        clock = [0.0]
        with patch.object(social_apps, 'capture_shell', return_value={'available': True, 'windows': [window]}), patch.object(social_apps.time, 'monotonic', side_effect=lambda: clock[0]), patch.object(social_apps.time, 'sleep', side_effect=lambda delta: clock.__setitem__(0, clock[0] + delta)), patch.object(social_apps, '_place_social_window') as move:
            result = social_apps._restore_window(social_apps.APP_BY_ID['slack'], target, claimed=set(), deadline=.5, no_place=False, settle_seconds=0, notify=lambda _: None)
        self.assertEqual(result, 1)
        move.assert_not_called()

    def test_inactive_popup_capture_never_focuses_or_mutates_tabs(self):
        with patch.object(browser, 'request_browser', return_value=[{'id': 12, 'focused': False}]) as request:
            with self.assertRaisesRegex(browser.BrowserUnavailable, 'Inactive Chrome popup'):
                browser._identify_native_window(profile='Default', chrome_window_id=12,
                                                app_id='google-chrome', window_type='popup', preserve_focus=True)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['list_windows'])

    def test_shared_evidence_pending_is_not_success_and_serializes(self):
        from workspace_state.provider_results import EvidenceState as S, PhaseEvidence as E, ProviderItemResult, ProviderCount, placement_accepted
        result = ProviderItemResult('vscode', '1', E(S.VERIFIED), E(S.VERIFIED), E(S.WAITING, 'deferred', True))
        self.assertFalse(result.success)
        self.assertTrue(result.retryable)
        self.assertEqual(result.to_dict()['placement'], {'state': 'waiting', 'detail': 'deferred', 'retryable': True})
        self.assertEqual(ProviderCount(0, [result]), 0)
        self.assertEqual(ProviderCount(0, [result]).results, (result,))
        self.assertTrue(placement_accepted({'status': 'deferred', 'placed': False}))

    def test_shared_placement_observes_state_and_normal_geometry(self):
        from workspace_state.provider_results import placement_matches
        target = dict(PLACEMENT, state='fullscreen')
        window = dict(target, geometry={'x': -100, 'width': 999, 'height': 999})
        self.assertTrue(placement_matches(window, target))
        self.assertFalse(placement_matches(dict(window, monitor=99), target))
        self.assertFalse(placement_matches(dict(window, state='normal'), target))
        self.assertFalse(placement_matches(dict(window, state='normal'), dict(target, state='normal')))

    def test_social_failure_does_not_skip_later_saved_occurrences(self):
        from test_social_apps import records, visible, placement
        from workspace_state.provider_results import ProviderRestoreError
        saved = records()
        saved['slack'] = {'running': True, 'mode': 'windowed', 'windows': [placement(), placement()]}
        observed = {'available': True, 'windows': [visible(window_id=1), visible(window_id=2)]}
        def item(*args, **kwargs):
            if not kwargs['claimed']:
                kwargs['on_selected'](1)
                raise CommandError('first failed')
            self.assertEqual(kwargs['claimed'], {1})
            return 2
        with patch.object(social_apps, 'capture_shell', return_value=observed), patch.object(social_apps, '_restore_window', side_effect=item) as attempt:
            with self.assertRaises(ProviderRestoreError) as caught:
                social_apps.restore_social_apps(saved, no_place=True)
        self.assertEqual(attempt.call_count, 2)
        self.assertEqual(len(caught.exception.results), 2)
        self.assertEqual(sum(result.success for result in caught.exception.results), 1)

    def test_partial_chrome_profile_capture_cannot_drop_unobserved_native_window(self):
        profiles = [{'profile': 'Default', 'windows': [{'id': 'one', 'runtime_window_id': 1, 'tabs': []}]}]
        shell = {'windows': [{'id': value, 'app_id': 'google-chrome'} for value in (10, 11)]}
        with patch.object(browser, '_identify_native_window', return_value=(10, {})), patch.object(browser, 'request_browser'):
            with self.assertRaisesRegex(browser.BrowserUnavailable, 'Not every native Chrome window'):
                browser._attach_desktop_placements(profiles, shell, [])
