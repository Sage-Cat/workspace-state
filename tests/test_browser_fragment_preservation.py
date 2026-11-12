"""Fragment-only placement preserves native content without adopting a recipe."""
from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import browser, browser_reconciliation as r, cli
from workspace_state.provider_results import EvidenceState as S
import test_browser_reconciliation as existing_tests


def fixture():
    window = {
        'id': 'window-10', 'type': 'normal', 'incognito': False,
        'state': 'maximized', 'workspace': 'Research', 'workspace_index': 2,
        'monitor': {'index': 0}, 'geometry': {'x': 0, 'y': 0, 'width': 1000, 'height': 700},
        'groups': [{'id': 'group-1', 'title': '', 'color': 'green', 'collapsed': False}],
        'tabs': [{'url': url, 'pinned': False, 'active': index == 2,
                  'group': 'group-1' if index < 4 else None}
                 for index, url in enumerate([
                     'https://docs.example.test/a', 'https://mail.example.test/mail/u/1/#sent/first',
                     'https://mail.example.test/mail/u/1/#sent/second',
                     'https://docs.example.test/b', 'https://chat.example.test/conversation'])],
    }
    saved = {'available': True, 'errors': [], 'profiles': [{
        'profile': 'Default', 'profile_directory': 'Default', 'app_id': 'google-chrome',
        'windows': [window]}]}
    native = deepcopy(saved['profiles'][0])
    live = native['windows'][0]
    live['runtime_window_id'] = 71
    live['runtime_identity'] = {'tabs': [[100 + i, 700 if i < 4 else -1] for i in range(5)], 'groups': [700]}
    live['tabs'][2]['url'] = 'https://mail.example.test/mail/u/1/#inbox/second'
    return {'browsers': {'google_chrome': saved}}, {'Default': native}


def overlap_window(label, workspace, urls, *, grouped=False, runtime=None):
    window = {'type': 'normal', 'incognito': False, 'state': 'maximized',
              'workspace': f'Desk {workspace}', 'workspace_index': workspace, 'monitor': {'index': 0},
              'geometry': {'x': 0, 'y': 0, 'width': 1000, 'height': 700},
              'groups': [{'id': 'group-1', 'title': 'Synthetic', 'color': 'blue', 'collapsed': False}] if grouped else [],
              'tabs': [{'url': url, 'pinned': False, 'active': index == 0,
                        'group': 'group-1' if grouped and index == 0 else None}
                       for index, url in enumerate(urls)]}
    if label is not None:
        window['id'] = label
    if runtime is not None:
        window['runtime_window_id'] = runtime
        window['runtime_identity'] = {
            'tabs': [[runtime * 100 + index, runtime if grouped and index == 0 else -1] for index in range(len(urls))],
            'groups': [runtime] if grouped else []}
    return window


def overlap_catalog(saved, live):
    profile = {'profile': 'Default', 'profile_directory': 'Default', 'app_id': 'google-chrome'}
    snapshot = {'browsers': {'google_chrome': {'available': True, 'errors': [], 'profiles': [
        {**profile, 'windows': deepcopy(saved)}]}}}
    return snapshot, {'Default': {**profile, 'windows': deepcopy(live)}}


class FragmentPlacementTests(unittest.TestCase):
    def test_complete_grouped_catalog_accepts_only_fragment_drift_without_adoption(self):
        snapshot, native = fixture()
        before = deepcopy((snapshot, native))
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual((snapshot, native), before)
        target = catalog['profiles'][0]['windows'][0]
        self.assertEqual(target['tabs'], snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'])
        self.assertEqual(target['_reconcile_window_id'], 71)
        self.assertTrue(target['_preserve_current_content'])
        self.assertEqual(target['_runtime_identity'], native['Default']['windows'][0]['runtime_identity'])
        self.assertEqual(receipt['content_basis'], 'current-native-session')
        self.assertFalse(receipt['canonical_checkpoint_changed'])
        self.assertEqual(cli._browser_restore_token_prefix(snapshot), cli._browser_restore_token_prefix(
            {'browsers': {'google_chrome': catalog}}))

    def assert_native_content_placed(self, snapshot, native):
        before = deepcopy((snapshot, native))
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual((snapshot, native), before)
        target = catalog['profiles'][0]['windows'][0]
        live = native['Default']['windows'][0]
        self.assertEqual((target['tabs'], target['groups']), (live['tabs'], live['groups']))
        for key in ('workspace', 'workspace_index', 'monitor', 'geometry', 'state'):
            self.assertEqual(target[key], snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0][key])
        self.assertEqual(receipt['native_windows'], {'Default': {'window-10': 71}})
        self.assertFalse(receipt['canonical_checkpoint_changed'])
        r.verify_navigation_reconciliation(catalog, native)

    def test_changed_tab_urls_place_the_current_native_content(self):
        for url in ('https://mail.example.test/mail/u/2/#inbox/second',
                    'https://mail.example.test/mail/u/1/?mode=other#inbox/second',
                    'https://mail.example.test/other/#inbox/second',
                    'https://another.example.test/mail/u/1/#inbox/second'):
            with self.subTest(url=url):
                snapshot, native = fixture()
                native['Default']['windows'][0]['tabs'][1]['url'] = url
                self.assert_native_content_placed(snapshot, native)

    def test_non_http_fragments_remain_exact_and_equivalent_exact_catalog_is_ordinary(self):
        snapshot, native = fixture()
        old = snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0]
        native['Default']['windows'][0]['tabs'] = deepcopy(old['tabs'])
        self.assertIsNone(r.reconcile_navigation(snapshot, native))
        old['tabs'][2]['url'] = 'file:///tmp/fixture#old'
        native['Default']['windows'][0]['tabs'][2]['url'] = 'file:///tmp/fixture#new'
        self.assert_native_content_placed(snapshot, native)

    def test_changed_groups_pins_and_order_keep_native_content(self):
        changes = [lambda n: n['groups'][0].update(color='red'),
                   lambda n: n['groups'][0].update(title='New'),
                   lambda n: n['groups'][0].update(collapsed=True),
                   lambda n: n['tabs'][3].update(group=None),
                   lambda n: n['tabs'][4].update(pinned=True),
                   lambda n: n['tabs'].reverse()]
        for change in changes:
            snapshot, native = fixture()
            change(native['Default']['windows'][0])
            self.assert_native_content_placed(snapshot, native)

    def test_window_type_incognito_and_full_inventory_are_required(self):
        for change in (lambda n: n.update(type='popup'), lambda n: n.update(incognito=True)):
            snapshot, native = fixture()
            change(native['Default']['windows'][0])
            self.assertIsNone(r.reconcile_navigation(snapshot, native))
        snapshot, native = fixture()
        native['Default']['windows'].append(deepcopy(native['Default']['windows'][0]))
        self.assertIsNone(r.reconcile_navigation(snapshot, native))

    def test_duplicate_buckets_require_identical_destinations(self):
        snapshot, native = fixture()
        saved = snapshot['browsers']['google_chrome']['profiles'][0]['windows']
        saved.append(dict(deepcopy(saved[0]), id='window-6'))
        live = deepcopy(native['Default']['windows'][0])
        live.update(runtime_window_id=72, runtime_identity={
            'tabs': [[200 + i, 701 if i < 4 else -1] for i in range(5)], 'groups': [701]})
        native['Default']['windows'].insert(0, live)
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(set(receipt['native_windows']['Default'].values()), {71, 72})
        r.verify_navigation_reconciliation(catalog, native)
        saved[1]['workspace_index'] = 3
        with self.assertRaisesRegex(r.ReconciliationRequired, 'different saved destinations'):
            r.reconcile_navigation(snapshot, native)

    def test_overlap_pairs_by_mutual_unique_best_and_elimination(self):
        inbox = 'https://mail.example.test/inbox'
        saved = [overlap_window('window-1', 0, [inbox, 'https://a.example.test/1', 'https://a.example.test/2'], grouped=True),
                 overlap_window('window-2', 1, [inbox, 'https://b.example.test/1']),
                 overlap_window('window-3', 2, ['https://c.example.test/1', 'https://c.example.test/2'], grouped=True),
                 overlap_window('window-4', 3, ['https://d.example.test/old'])]
        live = [overlap_window(None, 0, ['https://d.example.test/new'], runtime=84),
                overlap_window(None, 0, [inbox, 'https://a.example.test/1', 'https://a.example.test/3'], grouped=True, runtime=81),
                overlap_window(None, 0, ['https://c.example.test/2', 'https://c.example.test/9'], runtime=83),
                overlap_window(None, 0, [inbox, 'https://b.example.test/1', 'https://b.example.test/2'], runtime=82)]
        snapshot, native = overlap_catalog(saved, live)
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(receipt['native_windows'], {'Default': {
            'window-1': 81, 'window-2': 82, 'window-3': 83, 'window-4': 84}})
        by_label = {window['id']: window for window in catalog['profiles'][0]['windows']}
        self.assertEqual(by_label['window-3']['tabs'], live[2]['tabs'])
        self.assertEqual(by_label['window-3']['groups'], [])
        self.assertEqual(by_label['window-3']['workspace_index'], 2)
        r.verify_navigation_reconciliation(catalog, native)

    def test_overlap_ties_and_identical_native_windows_never_guess(self):
        shared = ['https://mail.example.test/inbox', 'https://docs.example.test/plan']
        saved = [overlap_window('window-1', 0, shared + ['https://a.example.test/1']),
                 overlap_window('window-2', 1, shared + ['https://b.example.test/1']),
                 overlap_window('window-3', 2, ['https://c.example.test/1'])]
        live = [overlap_window(None, 0, shared, runtime=81), overlap_window(None, 0, shared + ['https://z.example.test'], runtime=82),
                overlap_window(None, 0, ['https://c.example.test/1', 'https://c.example.test/2'], runtime=83)]
        self.assertIsNone(r.reconcile_navigation(*overlap_catalog(saved, live)))
        live[1] = overlap_window(None, 0, shared, runtime=82)
        self.assertIsNone(r.reconcile_navigation(*overlap_catalog(saved, live)))
        saved[1].update(workspace='Desk 0', workspace_index=0)
        catalog, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['native_windows']['Default']['window-3'], 83)
        self.assertEqual({receipt['native_windows']['Default'][label] for label in ('window-1', 'window-2')}, {81, 82})

    def test_late_fragment_navigation_is_preserved_but_replacement_objects_are_rejected(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        native['Default']['windows'][0]['tabs'][2]['url'] = 'https://mail.example.test/mail/u/1/#sent/second'
        r.verify_navigation_reconciliation(catalog, native)  # A page may revert its fragment while loading.
        for change in (lambda n: n.update(runtime_window_id=72),
                       lambda n: n['runtime_identity']['tabs'][2].__setitem__(0, 999),
                       lambda n: n['runtime_identity']['groups'].__setitem__(0, 701),
                       lambda n: n['tabs'][2].update(url='https://mail.example.test/new-path')):
            changed = deepcopy(native)
            change(changed['Default']['windows'][0])
            with self.assertRaises(r.ReconciliationRequired):
                r.verify_navigation_reconciliation(catalog, changed)

    def test_missing_runtime_identity_cannot_authorize_preservation(self):
        snapshot, native = fixture()
        del native['Default']['windows'][0]['runtime_identity']
        with self.assertRaisesRegex(r.ReconciliationRequired, 'identities are incomplete'):
            r.reconcile_navigation(snapshot, native)


class FragmentPreservationIntegrationTests(unittest.TestCase):
    def test_manual_restore_retains_strict_mode_without_startup_ownership(self):
        snapshot, _ = fixture()
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, '_startup_directory', return_value=Path(root)), \
             patch.object(cli, 'connected_profiles', return_value=['Default']), \
             patch.object(cli, 'browser_companion_info', return_value={
                 'protocol_version': 2, 'capabilities': list(browser.BROWSER_REQUIRED_CAPABILITIES)}), \
             patch.object(cli, 'request_browser') as request, \
             patch.object(cli, 'restore_browser', return_value=[browser.BrowserRestoreResult('strict', True)]) as restore:
            self.assertEqual(cli._restore_browsers(snapshot, Namespace(workspace=None,
                dry_run=False, no_place=True, login_status=False)), 1)
        request.assert_not_called()
        self.assertNotIn('_preserve_current_content', restore.call_args.args[0]['profiles'][0]['windows'][0])

    def test_optional_capture_failure_leaves_individual_strict_restore_available(self):
        snapshot, native = fixture()
        with ExitStack() as stack:
            _, _, _, request, restore, _ = existing_tests.BrowserReconciliationCliTests().harness(stack, snapshot, native)
            def failed_capture(action, *args, **kwargs):
                if action == 'capture':
                    raise browser.BrowserUnavailable('read-only capture unavailable')
                return {'exists': False}
            request.side_effect = failed_capture
            self.assertEqual(cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                no_place=False, login_status=False), start_browser=True), 1)
            self.assertNotIn('_preserve_current_content', restore.call_args.args[0]['profiles'][0]['windows'][0])

    def test_startup_uses_complete_preservation_plan_and_never_cleans_native_windows(self):
        snapshot, native = fixture()
        original = deepcopy(snapshot)
        with ExitStack() as stack:
            harness = existing_tests.BrowserReconciliationCliTests()
            root, owner, _, request, restore, cleanup = harness.harness(stack, snapshot, native)
            self.assertEqual(cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                no_place=False, login_status=False), start_browser=True), 1)
            self.assertEqual(snapshot, original)
            cleanup.assert_not_called()
            window = restore.call_args.args[0]['profiles'][0]['windows'][0]
            self.assertEqual(window['_reconcile_window_id'], 71)
            self.assertTrue(window['_preserve_current_content'])
            receipt = json.loads((root / 'browser-reconciliation.json').read_text())
            self.assertEqual(receipt['state'], 'verified-reuse-only')
            self.assertEqual(receipt['content_basis'], 'current-native-session')
            self.assertEqual(receipt['operation_context'], owner.to_dict())
            self.assertTrue(all(call.args[1] == {'runtime_identity': True}
                                for call in request.call_args_list if call.args[0] == 'capture'))

    def test_startup_places_newer_native_content_without_changing_the_recipe(self):
        snapshot, native = fixture()
        live = native['Default']['windows'][0]
        live['groups'][0]['title'] = 'Renamed'
        live['tabs'][4]['url'] = 'https://chat.example.test/another-conversation'
        original = deepcopy(snapshot)
        with ExitStack() as stack:
            harness = existing_tests.BrowserReconciliationCliTests()
            root, _owner, _, _request, restore, cleanup = harness.harness(stack, snapshot, native)
            self.assertEqual(cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                no_place=False, login_status=False), start_browser=True), 1)
            self.assertEqual(snapshot, original)
            cleanup.assert_not_called()
            window = restore.call_args.args[0]['profiles'][0]['windows'][0]
            self.assertEqual((window['tabs'], window['groups']), (live['tabs'], live['groups']))
            self.assertEqual((window['workspace_index'], window['_reconcile_window_id']), (2, 71))
            receipt = json.loads((root / 'browser-reconciliation.json').read_text())
            self.assertEqual(receipt['state'], 'verified-reuse-only')

    def test_restore_token_follows_the_saved_recipe_not_the_active_native_tab(self):
        # A retry after the user switched tabs must find its own earlier claims.
        snapshot, native = fixture()
        live = native['Default']['windows'][0]
        live['tabs'][4]['url'] = 'https://chat.example.test/another-conversation'
        prefixes = []
        for active in (0, 1):
            for index, tab in enumerate(live['tabs']):
                tab['active'] = index == active
            with ExitStack() as stack:
                harness = existing_tests.BrowserReconciliationCliTests()
                *_, restore, _cleanup = harness.harness(stack, snapshot, deepcopy(native))
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=False, login_status=False), start_browser=True)
                self.assertTrue(restore.call_args.args[0]['profiles'][0]['windows'][0]['_preserve_current_content'])
                prefixes.append(restore.call_args.kwargs['restore_token_prefix'])
        self.assertEqual(prefixes, [cli._browser_restore_token_prefix(snapshot)] * 2)

    def test_provider_reports_current_preservation_and_rechecks_before_move(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        reply = {'window_id': 71, 'created': False, 'reused': True, 'urls_restored': False,
                 'content_preserved': True, 'content_basis': 'current-native-session'}
        actions = []
        def request(action, payload, **kwargs):
            actions.append(action)
            return deepcopy(native['Default']) if action == 'capture' else dict(reply, exists=True)
        def place(**kwargs):
            kwargs['content_guard']()
            return True
        with patch.object(browser, 'request_browser', side_effect=request), \
             patch.object(browser, 'connected_profiles', return_value=['Default']), \
             patch.object(browser, 'expect_window', return_value='expected'), \
             patch.object(browser, 'cancel_expected_window'), \
             patch.object(browser, 'remap_monitor', side_effect=lambda p: p), \
             patch.object(browser, 'remap_workspace', side_effect=lambda p: p), \
             patch.object(browser, '_place_browser_window', side_effect=place) as placement:
            result = browser.restore_browser(catalog, restore_catalog=catalog)[0]
        self.assertTrue(result.success)
        self.assertEqual(result.evidence.content.state, S.VERIFIED)
        self.assertIn('Current native tabs and groups preserved', result.evidence.content.detail)
        self.assertNotIn('Exact loaded', result.evidence.content.detail)
        self.assertEqual(actions, ['restore_window', 'capture', 'restore_status'])
        self.assertEqual(placement.call_args.kwargs['chrome_window_id'], 71)
        # A preserved window verifies the login restore, so periodic checkpoints resume Chrome.
        from workspace_state.startup import StageMarker
        self.assertTrue(StageMarker('browsers', 'ready', provider_results=(result.evidence.to_dict(),)).verified)

    def test_changed_tab_after_prepare_blocks_native_move(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        mutated = deepcopy(native['Default'])
        mutated['windows'][0]['runtime_identity']['tabs'][2][0] = 999
        def request(action, payload, **kwargs):
            if action == 'capture':
                return mutated
            return {'window_id': 71, 'created': False, 'reused': True, 'urls_restored': False,
                    'content_preserved': True, 'content_basis': 'current-native-session'}
        submitted = []
        def place(**kwargs):
            kwargs['content_guard']()
            submitted.append(kwargs)
            return True
        with patch.object(browser, 'request_browser', side_effect=request), \
             patch.object(browser, 'connected_profiles', return_value=['Default']), \
             patch.object(browser, 'expect_window', return_value='expected'), \
             patch.object(browser, 'cancel_expected_window'), \
             patch.object(browser, 'remap_monitor', side_effect=lambda p: p), \
             patch.object(browser, 'remap_workspace', side_effect=lambda p: p), \
             patch.object(browser, '_place_browser_window', side_effect=place):
            result = browser.restore_browser(catalog, restore_catalog=catalog)[0]
        self.assertFalse(result.success)
        self.assertEqual(result.evidence.placement.state, S.FAILED)
        self.assertFalse(submitted)

    def test_existing_preservation_claim_survives_fragment_reversion_in_ordinary_retry(self):
        snapshot, _ = fixture()
        chrome = snapshot['browsers']['google_chrome']
        reply = {'window_id': 71, 'created': False, 'reused': True, 'exists': True,
                 'urls_restored': False, 'content_preserved': True, 'content_basis': 'current-native-session'}
        def place(**kwargs):
            kwargs['content_guard']()
            return True
        with patch.object(browser, 'request_browser', return_value=reply) as request, \
             patch.object(browser, 'expect_window', return_value='expected'), \
             patch.object(browser, 'cancel_expected_window'), \
             patch.object(browser, 'remap_monitor', side_effect=lambda p: p), \
             patch.object(browser, 'remap_workspace', side_effect=lambda p: p), \
             patch.object(browser, '_place_browser_window', side_effect=place):
            result = browser.restore_browser(chrome)[0]
        self.assertTrue(result.success)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['restore_window', 'restore_status'])
        self.assertIn('Current native', result.evidence.content.detail)


if __name__ == '__main__':
    unittest.main()
