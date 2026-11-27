"""Fragment-only placement preserves native content without adopting a recipe."""
from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import browser, browser_reconciliation as r, cli, provider_progress
from workspace_state.provider_results import EvidenceState as S, PhaseEvidence, ProviderItemResult
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


def partial_catalog():
    """Seven saved windows; five pair, two single-tab windows navigated elsewhere."""
    saved = [overlap_window('window-1', 1, ['https://w1.example.test/a', 'https://w1.example.test/b']),
             overlap_window('window-2', 2, ['https://w2.example.test/a', 'https://w2.example.test/b'], grouped=True),
             overlap_window('window-3', 3, ['https://w3.example.test/a', 'https://w3.example.test/b']),
             overlap_window('window-4', 4, ['https://w4.example.test/a', 'https://w4.example.test/b'], grouped=True),
             overlap_window('window-5', 5, ['https://w5.example.test/a', 'https://w5.example.test/b']),
             overlap_window('window-6', 6, ['https://w6.example.test/old']),
             overlap_window('window-7', 7, ['https://w7.example.test/old'])]
    live = [overlap_window(None, 0, ['https://w1.example.test/a', 'https://w1.example.test/b'], runtime=81),
            overlap_window(None, 0, ['https://w2.example.test/a', 'https://w2.example.test/b'], grouped=True, runtime=82),
            overlap_window(None, 0, ['https://w3.example.test/a', 'https://w3.example.test/c'], runtime=83),
            overlap_window(None, 0, ['https://w4.example.test/a', 'https://w4.example.test/z'], grouped=True, runtime=84),
            overlap_window(None, 0, ['https://w5.example.test/b', 'https://w5.example.test/y', 'https://w5.example.test/x'], runtime=85),
            overlap_window(None, 0, ['https://elsewhere6.example.test/'], runtime=86),
            overlap_window(None, 0, ['https://elsewhere7.example.test/'], runtime=87)]
    return overlap_catalog(saved, live)


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

    def test_non_http_fragments_remain_exact_and_an_identical_catalog_is_placed(self):
        snapshot, native = fixture()
        old = snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0]
        native['Default']['windows'][0]['tabs'] = deepcopy(old['tabs'])
        self.assert_native_content_placed(snapshot, native)
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

    def test_window_type_and_incognito_are_required_and_extra_native_windows_are_left_alone(self):
        for change in (lambda n: n.update(type='popup'), lambda n: n.update(incognito=True)):
            snapshot, native = fixture()
            change(native['Default']['windows'][0])
            catalog, receipt = r.reconcile_navigation(snapshot, native)
            self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-10']})
            self.assertEqual(catalog['profiles'], [])
        snapshot, native = fixture()
        extra = overlap_window(None, 0, ['https://unrelated.example.test/'], runtime=99)
        native['Default']['windows'].append(extra)
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(receipt['native_windows'], {'Default': {'window-10': 71}})
        self.assertEqual(receipt['unpaired_windows'], {})
        # Duplicate native identities fail this profile's windows, not the other profiles.
        native['Default']['windows'].append(deepcopy(extra))
        _catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-10']})
        self.assertIn('ambiguous', receipt['unpaired_reasons']['Default'])

    def test_unreadable_or_missing_native_profile_leaves_its_windows_unpaired(self):
        snapshot, native = fixture()
        for profiles in ({}, {'Default': None}, {'Default': dict(native['Default'], profile_directory='Other')}):
            catalog, receipt = r.reconcile_navigation(snapshot, profiles)
            self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-10']})

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
        # Exactly equal windows are interchangeable even when they have different destinations.
        for window in native['Default']['windows']:
            window['tabs'] = deepcopy(saved[0]['tabs'])
        for differences in ({'workspace_index': 3}, {'monitor_intent': {'connector': 'HDMI-2'}}):
            with self.subTest(differences=differences):
                original = saved[1]
                saved[1] = original | differences
                catalog, receipt = r.reconcile_navigation(snapshot, native)
                self.assertEqual(receipt['unpaired_windows'], {})
                self.assertEqual(set(receipt['native_windows']['Default'].values()), {71, 72})
                saved[1] = original

    def test_fragment_only_ambiguity_with_different_destinations_fails_only_those_windows(self):
        snapshot, native = fixture()
        saved = snapshot['browsers']['google_chrome']['profiles'][0]['windows']
        first = saved[0]
        second = dict(deepcopy(first), id='window-6', workspace_index=3, workspace='Other')
        second['tabs'][1]['url'] = 'https://mail.example.test/mail/u/1/#sent/third'
        saved.append(second)
        live_a, live_b = deepcopy(native['Default']['windows'][0]), deepcopy(native['Default']['windows'][0])
        live_a['tabs'][1]['url'] = 'https://mail.example.test/mail/u/1/#inbox/x'
        live_b.update(runtime_window_id=72)
        live_b['tabs'][1]['url'] = 'https://mail.example.test/mail/u/1/#inbox/y'
        native['Default']['windows'] = [live_a, live_b]
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-10', 'window-6']})
        self.assertEqual(catalog['profiles'], [])

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
        # The tied windows stay unpaired; only the unique window pairs.
        catalog, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['native_windows'], {'Default': {'window-3': 83}})
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-1', 'window-2']})
        self.assertEqual([w['id'] for w in catalog['profiles'][0]['windows']], ['window-3'])
        live[1] = overlap_window(None, 0, shared, runtime=82)
        catalog, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['native_windows'], {'Default': {'window-3': 83}})
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-1', 'window-2']})
        saved[1].update(workspace='Desk 0', workspace_index=0)
        catalog, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['native_windows']['Default']['window-3'], 83)
        self.assertEqual({receipt['native_windows']['Default'][label] for label in ('window-1', 'window-2')}, {81, 82})
        self.assertEqual(receipt['unpaired_windows'], {})

    def test_partial_pairing_keeps_only_paired_windows(self):
        snapshot, native = partial_catalog()
        catalog, receipt = r.reconcile_navigation(snapshot, native)
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-6', 'window-7']})
        self.assertEqual(receipt['native_windows']['Default'],
                         {f'window-{n}': 80 + n for n in range(1, 6)})
        windows = catalog['profiles'][0]['windows']
        self.assertEqual([w['id'] for w in windows], [f'window-{n}' for n in range(1, 6)])
        self.assertEqual(len({w['_reconcile_window_id'] for w in windows}), 5)
        self.assertTrue(all(w['_preserve_current_content'] for w in windows))
        r.verify_navigation_reconciliation(catalog, native)

    def test_no_pair_or_conflict_leaves_every_window_unpaired(self):
        saved = [overlap_window(f'window-{n}', n, [f'https://old{n}.example.test/']) for n in (1, 2)]
        live = [overlap_window(None, 0, [f'https://new{n}.example.test/'], runtime=80 + n) for n in (1, 2)]
        pairs, left_saved, left_live = r._overlap_pairs(saved, live)
        self.assertEqual((pairs, len(left_saved), len(left_live)), ([], 2, 2))
        _, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-1', 'window-2']})
        # Identical native windows cannot serve two different saved destinations.
        saved = [overlap_window('window-1', 0, ['https://a.example.test/1', 'https://a.example.test/2']),
                 overlap_window('window-2', 1, ['https://b.example.test/1'])]
        live = [overlap_window(None, 0, ['https://a.example.test/1', 'https://a.example.test/2'], runtime=n)
                for n in (81, 82)]
        self.assertEqual(r._overlap_pairs(saved, live)[0], [])
        _, receipt = r.reconcile_navigation(*overlap_catalog(saved, live))
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-1', 'window-2']})

    def test_unequal_inventory_keeps_positive_pairs_and_never_guesses_the_rest(self):
        snapshot, native = partial_catalog()
        for extra_live in (False, True):
            with self.subTest(extra_live=extra_live):
                current = deepcopy(native)
                if extra_live:
                    current['Default']['windows'].append(overlap_window(
                        None, 0, ['https://unrelated.example.test/'], runtime=99))
                else:
                    current['Default']['windows'].pop()
                catalog, receipt = r.reconcile_navigation(snapshot, current)
                self.assertEqual(receipt['native_windows']['Default'],
                                 {f'window-{n}': 80 + n for n in range(1, 6)})
                self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-6', 'window-7']})
                r.verify_navigation_reconciliation(catalog, current)

    def test_late_content_changes_are_accepted_but_missing_or_retyped_windows_are_rejected(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        for change in (lambda n: n['tabs'][2].update(url='https://mail.example.test/mail/u/1/#sent/second'),
                       lambda n: n['runtime_identity']['tabs'][2].__setitem__(0, 999),
                       lambda n: n['runtime_identity']['groups'].__setitem__(0, 701),
                       lambda n: n['tabs'][2].update(url='https://mail.example.test/new-path'),
                       lambda n: n['tabs'].append(dict(n['tabs'][0], url='https://new.example.test/tab')),
                       lambda n: n['tabs'].pop()):
            changed = deepcopy(native)
            change(changed['Default']['windows'][0])
            r.verify_navigation_reconciliation(catalog, changed)
            self.assertEqual(r.preservation_problems(catalog, changed), {})
        extra = deepcopy(native)
        extra['Default']['windows'].append(overlap_window(None, 1, ['https://new.example.test/x'], runtime=99))
        r.verify_navigation_reconciliation(catalog, extra)
        for change in (lambda n: n.update(runtime_window_id=72),
                       lambda n: n.update(type='popup'),
                       lambda n: n.update(incognito=True)):
            changed = deepcopy(native)
            change(changed['Default']['windows'][0])
            with self.assertRaises(r.ReconciliationRequired):
                r.verify_navigation_reconciliation(catalog, changed)
            self.assertEqual(set(r.preservation_problems(catalog, changed)), {('Default', 71)})
        changed = deepcopy(native)
        changed['Default']['profile_directory'] = 'Profile 9'
        with self.assertRaisesRegex(r.ReconciliationRequired, 'profile identity'):
            r.verify_navigation_reconciliation(catalog, changed)

    def test_per_window_problems_name_only_the_affected_windows(self):
        saved = [overlap_window(f'window-{n}', n, [f'https://w{n}.example.test/a', f'https://w{n}.example.test/b'])
                 for n in (1, 2, 3)]
        live = [overlap_window(None, 0, [f'https://w{n}.example.test/a#loaded', f'https://w{n}.example.test/b'], runtime=80 + n)
                for n in (1, 2, 3)]
        catalog, _ = r.reconcile_navigation(*overlap_catalog(saved, live))
        gone = deepcopy(live)
        del gone[1]
        gone[0]['tabs'][0]['url'] = 'https://w1.example.test/changed'
        _, native = overlap_catalog(saved, gone)
        self.assertEqual(r.preservation_problems(catalog, native), {('Default', 82): 'current native window is missing'})
        r.verify_navigation_reconciliation(catalog, native, 'Default', 81)
        r.verify_navigation_reconciliation(catalog, native, 'Default', 83)
        with self.assertRaisesRegex(r.ReconciliationRequired, 'missing'):
            r.verify_navigation_reconciliation(catalog, native, 'Default', 82)

    def test_missing_runtime_identity_fails_only_its_own_profile(self):
        def profile(name, label, runtime):
            window = overlap_window(label, 1, [f'https://{name}.example.test/a', f'https://{name}.example.test/b'])
            live = overlap_window(None, 0, [f'https://{name}.example.test/a', f'https://{name}.example.test/b'],
                                  runtime=runtime)
            base = {'profile': name, 'profile_directory': name, 'app_id': 'google-chrome'}
            return {**base, 'windows': [window]}, {**base, 'windows': [live]}
        (saved_a, live_a), (saved_b, live_b) = profile('Alpha', 'window-1', 81), profile('Beta', 'window-2', 82)
        del live_a['windows'][0]['runtime_identity']
        snapshot = {'browsers': {'google_chrome': {'available': True, 'errors': [], 'profiles': [saved_a, saved_b]}}}
        catalog, receipt = r.reconcile_navigation(snapshot, {'Alpha': live_a, 'Beta': live_b})
        self.assertEqual(receipt['unpaired_windows'], {'Alpha': ['window-1']})
        self.assertIn('identities are incomplete', receipt['unpaired_reasons']['Alpha'])
        self.assertEqual(receipt['native_windows']['Beta'], {'window-2': 82})
        self.assertEqual([item['profile'] for item in catalog['profiles']], ['Beta'])


class FragmentPreservationIntegrationTests(unittest.TestCase):
    def test_manual_restore_pairs_native_windows_without_startup_ownership(self):
        for argv in (['restore', '--no-place'], ['restore', 'browsers', '--no-place']):
            snapshot, native = fixture()
            args = cli.parser().parse_args(argv)
            with tempfile.TemporaryDirectory() as root, \
                 patch.object(cli, 'CATEGORIES', ('browsers', 'vscode')), \
                 patch.object(cli, '_startup_directory', return_value=Path(root)), \
                 patch.object(cli, 'connected_profiles', return_value=['Default']), \
                 patch.object(cli, 'browser_companion_info', return_value={
                     'protocol_version': 2, 'capabilities': list(browser.BROWSER_REQUIRED_CAPABILITIES)}), \
                 patch.object(cli, 'request_browser', side_effect=lambda action, *a, **k:
                              deepcopy(native['Default'])) as request, \
                 patch.object(cli, 'restore_vscode', return_value=0), \
                 patch.object(cli, 'restore_browser', return_value=[browser.BrowserRestoreResult('paired', True)]) as restore:
                with self.subTest(argv=argv):
                    self.assertEqual(cli._restore(snapshot, args)['browsers'], 1)
            window = restore.call_args.args[0]['profiles'][0]['windows'][0]
            self.assertEqual(window['_reconcile_window_id'], 71)
            self.assertTrue(request.called)

    def test_explicit_recreation_preflight_refuses_urls_visible_in_another_profile(self):
        snapshot, native = partial_catalog()
        saved = r.browser_state(snapshot)['profiles'][0]['windows'][5]
        other = overlap_window(None, 0, [saved['tabs'][0]['url']], runtime=99)
        self.assertFalse(r.can_recreate_ungrouped(saved, {'Other': {'window-1': 99}},
                                                  {'Other': {'windows': [other]}}))


if __name__ == '__main__':
    unittest.main()
