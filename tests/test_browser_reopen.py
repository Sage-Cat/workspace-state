"""`wsctl browser reopen` opens a missing saved window only on explicit request."""
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import fcntl
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import browser, browser_reconciliation as r, cli
import test_browser_fragment_preservation as fixtures


def catalog(*, grouped_missing=False):
    """Five windows paired with Chrome's own session and one saved window that is gone."""
    snapshot, native = fixtures.partial_catalog()
    windows = r.browser_state(snapshot)['profiles'][0]['windows']
    windows.pop()  # window-7
    del native['Default']['windows'][5:]
    if grouped_missing:
        missing = windows[5]
        missing['groups'] = [{'id': 'group-9', 'title': 'Pinned', 'color': 'blue', 'collapsed': False}]
        missing['tabs'][0]['group'] = 'group-9'
    return snapshot, native


class Chrome:
    def __init__(self, test, snapshot, native, *, connected=('Default',), capabilities=None, claim=False,
                 late_window=False, placement=True, fail_after_create=False):
        self.test, self.snapshot, self.native = test, snapshot, native
        self.connected, self.claim, self.late_window = list(connected), claim, late_window
        self.placement = placement
        self.fail_after_create, self.claims = fail_after_create, {}
        self.capabilities = capabilities or [*browser.BROWSER_REQUIRED_CAPABILITIES, 'recreate_window']
        self.calls, self.created, self.placed = [], [], []

    def request(self, action, payload=None, **kwargs):
        self.calls.append(action)
        if action == 'capture':
            if self.fail_after_create and self.created:
                raise browser.BrowserUnavailable('Chrome stopped answering')
            current = deepcopy(self.native['Default'])
            current['windows'].extend(self.created)
            if self.late_window and self.calls.count('capture') > 1:
                current['windows'].append(fixtures.overlap_window(None, 0, ['https://late.example.test/'], runtime=99))
            return current
        if action == 'restore_status':
            return self.claims.get(payload['restore_token'], {'exists': self.claim})
        if action == 'restore_window':
            self.test.assertTrue(payload['reuse_only'] and payload['preserve_current_content'])
            self.claims[payload['restore_token']] = {
                'exists': True, 'window_id': payload['expected_window_id'], 'content_preserved': True,
                'content_basis': 'current-native-session', 'created': False, 'reused': True}
            return self.claims[payload['restore_token']]
        self.test.assertEqual(action, 'recreate_window')
        self.test.assertIn(payload['window']['id'], ('window-6', 'window-7'))
        self.test.assertNotIn('reuse_only', payload)
        runtime = 86 + len(self.created)
        window = fixtures.overlap_window(None, 0, [payload['window']['tabs'][0]['url']], runtime=runtime)
        self.created.append(window)
        self.claims[payload['restore_token']] = {'exists': True, 'window_id': runtime, 'created': True,
                                                 'urls_restored': True}
        return self.claims[payload['restore_token']]

    def __enter__(self):
        self.stack = stack = ExitStack()
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.object(cli, '_startup_directory', return_value=self.root))
        stack.enter_context(patch('workspace_state.storage.load', return_value=self.snapshot))
        stack.enter_context(patch('workspace_state.workspace_switch.suppress', side_effect=lambda value: value))
        stack.enter_context(patch.object(cli, 'connected_profiles', side_effect=lambda: list(self.connected)))
        stack.enter_context(patch.object(cli, 'browser_companion_info', return_value={
            'protocol_version': 2, 'capabilities': self.capabilities}))
        stack.enter_context(patch.object(cli, 'request_browser', side_effect=self.request))
        stack.enter_context(patch.object(browser, 'request_browser', side_effect=self.request))
        self.launch = stack.enter_context(patch.object(cli, 'ensure_browser_profiles'))
        self.stage = stack.enter_context(patch.object(cli, 'update_stage'))
        stack.enter_context(patch.object(browser, 'expect_window', return_value='expected'))
        stack.enter_context(patch.object(browser, 'cancel_expected_window'))
        stack.enter_context(patch.object(browser, 'expected_window_status', return_value='placed'))
        stack.enter_context(patch.object(browser, 'remap_monitor', side_effect=lambda p: p))
        stack.enter_context(patch.object(browser, 'remap_workspace', side_effect=lambda p: p))
        def place(**options):
            self.placed.append(options['chrome_window_id'])
            if self.placement == 'handoff':
                raise browser.BrowserPlacementPending('awaiting placement', 'native-handoff:1')
            return self.placement
        stack.enter_context(patch.object(browser, '_place_browser_window', side_effect=place))
        stack.enter_context(patch('workspace_state.provider_progress._query_request', return_value={
            'token': 'native-handoff:1', 'status': 'verified'}))
        return self

    def __exit__(self, *exc):
        return self.stack.__exit__(*exc)

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            args = cli.parser().parse_args(['browser', 'reopen', *argv])
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()


class BrowserReopenTests(unittest.TestCase):
    def test_a_missing_regular_window_is_opened_and_placed_without_touching_other_state(self):
        snapshot, native = catalog()
        before = deepcopy((snapshot, native))
        with Chrome(self, snapshot, native) as chrome:
            code, out, err = chrome.run()
            self.assertEqual(code, 0, err)
            self.assertEqual(chrome.calls.count('recreate_window'), 1)
            self.assertEqual(chrome.placed, [86])
            self.assertIn('Default/window-6', out)
            chrome.stage.assert_not_called()
            chrome.launch.assert_not_called()
            self.assertEqual(os.listdir(chrome.root), ['restore.lock'])
        self.assertEqual((snapshot, native), before)

    def test_failed_placement_keeps_the_new_window_and_fails_only_that_item(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native, placement=False) as chrome:
            code, _out, err = chrome.run()
        self.assertEqual(code, 3)
        self.assertIn('Default/window-6', err)
        self.assertNotIn('close_restored_window', chrome.calls)
        self.assertEqual(chrome.placed, [86])

    def test_an_accepted_compositor_handoff_is_observed(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native, placement='handoff') as chrome:
            code, out, _err = chrome.run()
        self.assertEqual(code, 0)
        self.assertIn('placement verified', out)

    def test_a_retained_observation_recipe_reopens_like_an_ordinary_one(self):
        snapshot, native = catalog()
        r.browser_state(snapshot)['latest_observation'] = {'schema_version': 2}
        with Chrome(self, snapshot, native) as chrome:
            code, _out, err = chrome.run()
        self.assertEqual(code, 0, err)
        self.assertEqual(chrome.calls.count('recreate_window'), 1)

    def test_manual_restore_places_paired_windows_of_a_retained_observation(self):
        snapshot, native = catalog()
        r.browser_state(snapshot)['latest_observation'] = {'schema_version': 2}
        r.browser_state(snapshot)['profiles'][0]['windows'].pop()
        del native['Default']['windows'][5:]
        with Chrome(self, snapshot, native) as chrome:
            args = cli.parser().parse_args(['restore', 'browsers'])
            count = cli._restore(snapshot, args)['browsers']
        self.assertEqual(count, 5)
        self.assertEqual(sorted(chrome.placed), [81, 82, 83, 84, 85])

    def test_the_claim_prefix_ignores_a_retained_observation(self):
        snapshot, _native = catalog()
        before = cli._browser_restore_token_prefix(snapshot)
        r.browser_state(snapshot)['latest_observation'] = {'schema_version': 2}
        self.assertEqual(cli._browser_restore_token_prefix(snapshot), before)

    def test_a_restore_after_a_reopen_places_the_reopened_window_too(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native) as chrome:
            self.assertEqual(chrome.run()[0], 0)
            self.assertEqual(chrome.placed, [86])
            chrome.placed.clear()
            args = cli.parser().parse_args(['restore', 'browsers'])
            count = cli._restore(snapshot, args)['browsers']
            self.assertEqual(count, 6)
            self.assertEqual(sorted(chrome.placed), [81, 82, 83, 84, 85, 86])
            self.assertEqual(chrome.calls.count('recreate_window'), 1)

    def test_a_blocked_profile_is_not_reopened(self):
        snapshot, native = catalog()
        snapshot['_workspace_switch_blocked_profiles'] = {'Default'}
        with Chrome(self, snapshot, native) as chrome:
            code, _out, err = chrome.run('--profile', 'Default')
            self.assertEqual(code, 1)
            self.assertIn('interrupted workspace switch', err)
            code, _out, err = chrome.run()
            self.assertNotEqual(code, 0)
        self.assertNotIn('recreate_window', chrome.calls)

    def test_a_blocked_profile_outside_the_selection_does_not_fail_the_command(self):
        snapshot, native = catalog()
        other = deepcopy(r.browser_state(snapshot)['profiles'][0])
        other['profile'] = 'Other'
        for number, window in enumerate(other['windows']):
            window['id'] = f'other-{number}'
        r.browser_state(snapshot)['profiles'].append(other)
        snapshot['_workspace_switch_blocked_profiles'] = {'Other'}
        for argv in (['--profile', 'Default'], ['--window', 'window-6'], []):
            with self.subTest(argv=argv), Chrome(self, snapshot, native) as chrome:
                code, _out, err = chrome.run(*argv)
                self.assertEqual(code, 3 if not argv else 0, err)
                self.assertEqual('interrupted workspace switch' in err, not argv)

    def test_a_failure_after_the_first_window_is_reported_with_exit_three(self):
        snapshot, native = catalog()
        windows = r.browser_state(snapshot)['profiles'][0]['windows']
        windows.append(fixtures.overlap_window('window-7', 7, ['https://w7.example.test/old']))
        with Chrome(self, snapshot, native, fail_after_create=True) as chrome:
            code, out, err = chrome.run()
        self.assertEqual(code, 3)
        self.assertIn('Default/window-6', out)
        self.assertIn('Chrome stopped answering', err)
        self.assertEqual(chrome.calls.count('recreate_window'), 1)

    def test_dry_run_only_says_what_would_open(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native) as chrome:
            code, out, _err = chrome.run('--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('would reopen Chrome Default/window-6 (1 tabs)', out)
        self.assertEqual(chrome.created, [])
        self.assertNotIn('recreate_window', chrome.calls)

    def test_nothing_missing_changes_nothing(self):
        snapshot, native = catalog()
        r.browser_state(snapshot)['profiles'][0]['windows'].pop()
        del native['Default']['windows'][5:]
        with Chrome(self, snapshot, native) as chrome:
            code, out, _err = chrome.run()
        self.assertEqual(code, 0)
        self.assertIn('No saved Chrome window needs to be reopened', out)
        self.assertNotIn('recreate_window', chrome.calls)

    def test_a_grouped_window_is_refused_without_advising_this_command(self):
        snapshot, native = catalog(grouped_missing=True)
        with Chrome(self, snapshot, native) as chrome:
            code, _out, err = chrome.run()
        self.assertEqual(code, 3)
        self.assertIn('Open it in Chrome; grouped windows are not reopened automatically', err)
        self.assertNotIn('wsctl browser reopen', err)
        self.assertNotIn('recreate_window', chrome.calls)

    def test_late_window_live_claim_and_overlap_refuse_before_creation(self):
        for name, options in (('late-window', {'late_window': True}), ('live-claim', {'claim': True})):
            snapshot, native = catalog()
            with self.subTest(name), Chrome(self, snapshot, native, **options) as chrome:
                code, _out, err = chrome.run()
                self.assertEqual(code, 3, err)
                self.assertNotIn('recreate_window', chrome.calls)
        snapshot, native = catalog()
        missing_url = r.browser_state(snapshot)['profiles'][0]['windows'][5]['tabs'][0]['url']
        native['Default']['windows'].append(fixtures.overlap_window(None, 0, [missing_url], runtime=90))
        with Chrome(self, snapshot, native) as chrome:
            code, out, err = chrome.run()
        # A live window that shares the saved tabs pairs with it: it is already open.
        self.assertEqual(code, 0, err)
        self.assertNotIn('recreate_window', chrome.calls)

    def test_chrome_not_running_is_reported_and_never_started(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native, connected=()) as chrome:
            code, _out, err = chrome.run()
            chrome.launch.assert_not_called()
        self.assertEqual(code, 1)
        self.assertIn('Nothing was started', err)

    def test_outdated_companion_and_unknown_window_change_nothing(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native, capabilities=list(browser.BROWSER_REQUIRED_CAPABILITIES)) as chrome:
            code, _out, err = chrome.run()
        self.assertEqual((code, 'outdated' in err), (1, True))
        with Chrome(self, snapshot, native) as chrome:
            code, _out, err = chrome.run('--window', 'window-99')
        self.assertEqual((code, 'window-99' in err), (1, True))
        self.assertNotIn('recreate_window', chrome.calls)

    def test_a_running_restore_stops_the_command_at_once(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native) as chrome:
            descriptor = os.open(chrome.root / 'restore.lock', os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                code, _out, err = chrome.run()
            finally:
                os.close(descriptor)
        self.assertEqual(code, 1)
        self.assertIn('A restore is running', err)
        self.assertEqual(chrome.calls, [])

    def test_selection_by_profile_and_window(self):
        snapshot, native = catalog()
        with Chrome(self, snapshot, native) as chrome:
            code, out, _err = chrome.run('--profile', 'Default', '--window', 'window-6', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('window-6', out)
        with Chrome(self, snapshot, native) as chrome:
            code, out, _err = chrome.run('--window', 'window-1', '--dry-run')
        self.assertIn('Chrome Default/window-1 is already open in native window 81', out)


if __name__ == '__main__':
    unittest.main()
