"""Behaviour that must survive the removal of Chrome content restoration."""
from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_state import browser, cli, operations
from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES
from workspace_state.provider_results import EvidenceState
import test_browser_fragment_preservation as fixtures


class IdenticalNativeWindowsTests(unittest.TestCase):
    """Chrome's own session restore usually brings back exactly the saved windows."""

    def run_startup(self, snapshot, native):
        placed, calls = [], []
        claims = {}

        def companion(action, payload, **kwargs):
            calls.append(action)
            if action == 'capture':
                return deepcopy(native[kwargs['profile']])
            token = payload.get('restore_token')
            if action == 'restore_status':
                return claims.get(token, {'exists': False})
            self.assertEqual(action, 'restore_window')
            window_id = payload.get('expected_window_id', native['Default']['windows'][0]['runtime_window_id'])
            if payload.get('reuse_only') and payload.get('preserve_current_content'):
                result = {'exists': True, 'window_id': window_id, 'content_preserved': True,
                          'content_basis': 'current-native-session', 'created': False, 'reused': True}
            else:
                result = {'exists': True, 'window_id': window_id, 'urls_restored': True,
                          'created': False, 'reused': True, 'group_warnings': []}
            claims[token] = result
            return result

        def place(**options):
            if options.get('content_guard'):
                options['content_guard']()
            placed.append((options['chrome_window_id'], options['placement']['workspace']))
            return True

        with ExitStack() as stack:
            root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            status = root / 'status.json'
            status.write_text('{"operation_state":"running"}')
            owner = Mock(mode='startup')
            owner.matches.return_value = True
            owner.to_dict.return_value = {'operation_id': 'synthetic-operation'}
            stack.enter_context(patch.object(operations, 'current', return_value=owner))
            stack.enter_context(patch.object(cli, 'status_path', return_value=status))
            stack.enter_context(patch.object(cli, '_startup_directory', return_value=root))
            stack.enter_context(patch.object(cli, 'connected_profiles', return_value=['Default']))
            stack.enter_context(patch.object(cli, 'ensure_browser_profiles', return_value=[]))
            stack.enter_context(patch.object(cli, 'wait_for_browser_settle'))
            stack.enter_context(patch.object(cli, 'browser_companion_info', return_value={
                'protocol_version': 2, 'capabilities': list(BROWSER_REQUIRED_CAPABILITIES)}))
            stack.enter_context(patch.object(cli, 'request_browser', side_effect=companion))
            stack.enter_context(patch.object(browser, 'request_browser', side_effect=companion))
            stack.enter_context(patch.object(browser, 'expect_window', return_value='expected'))
            stack.enter_context(patch.object(browser, 'cancel_expected_window'))
            stack.enter_context(patch.object(browser, 'expected_window_status', return_value='placed'))
            stack.enter_context(patch.object(browser, 'remap_monitor', side_effect=lambda p: p))
            stack.enter_context(patch.object(browser, 'remap_workspace', side_effect=lambda p: p))
            stack.enter_context(patch.object(browser, '_place_browser_window', side_effect=place))
            count = cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                no_place=False, login_status=False), start_browser=True)
        return count, placed, calls

    def test_windows_identical_to_the_saved_ones_are_verified_and_placed(self):
        snapshot, native = fixtures.fixture()
        live = native['Default']['windows'][0]
        live['tabs'][2]['url'] = snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][2]['url']
        original = deepcopy(snapshot)
        count, placed, calls = self.run_startup(snapshot, native)
        self.assertEqual(count, 1)
        self.assertTrue(all(item.success and item.content.state == EvidenceState.VERIFIED
                            and item.placement.state == EvidenceState.VERIFIED for item in count.results))
        self.assertEqual(placed, [(71, 2)])
        self.assertNotIn('close_restored_window', calls)
        self.assertNotIn('recreate_window', calls)
        self.assertEqual(snapshot, original)


COMPANION_0_6_0 = frozenset("""
    capture list_windows restore_window recreate_window restore_status identify_window focus_window
    release_window_identification close_restored_window scoped_creation_marker workspace_switch
    exact_url_restore exact_url_pending reuse_only_groups installed_build_activation lazy_tab_restore
    repair_restored_tabs recover_original_window inspect_original_window original_groups_required
    unclaimed_original_guard exact_capture_identity runtime_build native_mutation_status
    reconciliation_reuse_only preserve_native_fragment_navigation identification_lease_lifecycle
    asynchronous_restore_verification cooperative_restore_abort
""".split())


class CompanionCompatibilityTests(unittest.TestCase):
    def test_the_cli_requires_only_what_the_companion_advertises_and_reopen_needs_recreation(self):
        import re
        source = (Path(__file__).resolve().parent.parent / 'chrome-extension' / 'service-worker.js').read_text()
        listing = re.search(r"capabilities: \[(.*?)\]", source, re.S).group(1)
        advertised = set(re.findall(r"'([a-z_]+)'", listing))
        self.assertLessEqual(BROWSER_REQUIRED_CAPABILITIES, advertised)
        self.assertIn('recreate_window', advertised)
        # The CLI must also work with the installed companion 0.6.0, whose advertised list is
        # copied from chrome-extension/service-worker.js at 556fb6e.
        self.assertLessEqual(BROWSER_REQUIRED_CAPABILITIES, COMPANION_0_6_0)
        self.assertIn('recreate_window', COMPANION_0_6_0)
        self.assertEqual(browser.BROWSER_PROTOCOL_VERSION, 2)


class WorkspaceSwitchTests(unittest.TestCase):
    """Turning a workspace on reopens its parked window through the marker."""

    def chrome(self):
        window = fixtures.overlap_window('window-1', 2, ['https://a.example.test/', 'https://b.example.test/'])
        return {'profiles': [{'profile': 'Default', 'profile_directory': 'Default',
                              'app_id': 'google-chrome', 'windows': [window]}]}

    def test_on_asks_the_marker_to_reopen_places_and_confirms_without_other_mutation(self):
        calls, placed = [], []

        def companion(action, payload, **kwargs):
            calls.append((action, payload.get('workspace_switch')))
            if action == 'restore_window':
                self.assertTrue(payload['workspace_switch'])
                self.assertEqual(payload['marker'], browser._workspace_marker(payload['restore_token']))
                return {'window_id': 5, 'created': True, 'urls_restored': True, 'workspace_placed': False}
            return {'confirmed': True}

        with patch.object(browser, 'request_browser', side_effect=companion), \
             patch.object(browser, 'browser_companion_info', return_value={'capabilities': ['workspace_switch']}), \
             patch.object(browser, 'remap_monitor', side_effect=lambda p: p), \
             patch.object(browser, 'remap_workspace', side_effect=lambda p: p), \
             patch.object(browser, '_place_browser_window',
                          side_effect=lambda **o: placed.append(o['chrome_window_id']) or True):
            results = browser.restore_browser(self.chrome(), workspace='Desk 2', workspace_switch=True,
                                              restore_token_prefix='switch-token')
        self.assertTrue(all(item.success for item in results))
        self.assertEqual(placed, [5])
        self.assertEqual([name for name, _ in calls], ['restore_window', 'confirm_workspace_placement'])

    def test_off_marks_and_cancels_only_through_marker_requests(self):
        window = self.chrome()['profiles'][0]['windows'][0]
        window['_browser_window_id'] = 9
        with patch.object(browser, 'request_browser', return_value={'prepared': True, 'cancelled': True}) as request:
            browser.prepare_workspace_window('Default', window, 'token')
            browser.cancel_workspace_window('Default', 'token')
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['prepare_workspace_window', 'cancel_workspace_window'])


if __name__ == '__main__':
    unittest.main()
