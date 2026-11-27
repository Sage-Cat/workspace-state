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

    def test_manual_restore_all_keeps_the_regular_browser_path(self):
        snapshot, _ = fixture()
        args = cli.parser().parse_args(['restore', '--no-place'])
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, 'CATEGORIES', ('browsers', 'vscode')), \
             patch.object(cli, '_startup_directory', return_value=Path(root)), \
             patch.object(cli, 'connected_profiles', return_value=['Default']), \
             patch.object(cli, 'browser_companion_info', return_value={
                 'protocol_version': 2, 'capabilities': list(browser.BROWSER_REQUIRED_CAPABILITIES)}), \
             patch.object(cli, 'request_browser') as request, \
             patch.object(cli, 'restore_vscode', return_value=0), \
             patch.object(cli, 'restore_browser', return_value=[browser.BrowserRestoreResult('strict', True)]) as restore:
            self.assertEqual(cli._restore(snapshot, args)['browsers'], 1)
        request.assert_not_called()
        self.assertNotIn('_preserve_current_content', restore.call_args.args[0]['profiles'][0]['windows'][0])

    def test_manual_restore_requires_explicit_recreation_and_checks_late_inventory(self):
        for scenario in ('no-opt-in', 'recreated', 'late-window', 'live-claim', 'placement-failed',
                         'handoff', 'existing-placement-failed', 'group-warning'):
            with self.subTest(scenario=scenario):
                snapshot, native = partial_catalog()
                r.browser_state(snapshot)['profiles'][0]['windows'].pop()
                del native['Default']['windows'][5:]
                self._manual_partial_restore(snapshot, native, scenario=scenario)

    def test_recreation_option_requires_one_known_ungrouped_slot(self):
        snapshot, _ = partial_catalog()
        parser = cli.parser()
        for argv in (
            ['restore'],
            ['restore', 'browsers', '--dry-run'],
            ['restore', 'browsers', '--workspace', 'Desk 5'],
            ['restore', 'browsers', '--recreate-window', 'Default/window-5'],
        ):
            args = parser.parse_args([*argv, '--recreate-window', 'Default/window-6'])
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, 'unfiltered'):
                cli._restore(snapshot, args)
        for value in ('Default', '/window-6', 'Default/', 'Default/window-6/other'):
            with self.subTest(value=value), self.assertRaises(cli.argparse.ArgumentTypeError):
                cli._browser_recreation_slot(value)
        for value, error in (('Default/window-2', 'ungrouped'), ('Default/unknown', 'Expected one')):
            args = parser.parse_args(['restore', 'browsers', '--recreate-window', value])
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, error):
                cli._restore(snapshot, args)

    def test_old_companion_refuses_explicit_recreation_before_browser_mutation(self):
        snapshot, _ = partial_catalog()
        args = cli.parser().parse_args(['restore', 'browsers', '--recreate-window', 'Default/window-6'])
        with patch.object(cli, 'connected_profiles', return_value=['Default']), \
             patch.object(cli, 'browser_companion_info', return_value={
                 'protocol_version': 2, 'capabilities': list(browser.BROWSER_REQUIRED_CAPABILITIES)}), \
             patch.object(cli, 'request_browser') as request, \
             patch.object(cli, 'restore_browser') as restore:
            with self.assertRaisesRegex(browser.BrowserUnavailable, 'before using --recreate-window'):
                cli._restore(snapshot, args)
        request.assert_not_called()
        restore.assert_not_called()

    def test_explicit_recreation_preflight_refuses_urls_visible_in_another_profile(self):
        snapshot, native = partial_catalog()
        saved = r.browser_state(snapshot)['profiles'][0]['windows'][5]
        other = overlap_window(None, 0, [saved['tabs'][0]['url']], runtime=99)
        self.assertFalse(r.can_recreate_ungrouped(saved, {'Other': {'window-1': 99}},
                                                  {'Other': {'windows': [other]}}))

    def _manual_partial_restore(self, snapshot, native, *, scenario):
        original = deepcopy((snapshot, native))
        strict = {'window-1': 81, 'window-2': 82}
        preserved = {'window-3': 83, 'window-4': 84, 'window-5': 85}
        claimed = {}
        placed = []
        recreated = []

        def request(action, payload, **kwargs):
            if action == 'capture':
                current = deepcopy(native['Default'])
                if recreated:
                    current['windows'].append(recreated[0])
                if scenario == 'late-window' and set(placed) == {81, 82, 83, 84, 85}:
                    current['windows'].append(overlap_window(
                        None, 0, ['https://unaccounted.example.test/'], runtime=99))
                return current
            label = payload['restore_token'].split(':')[-1]
            if action == 'restore_status':
                if label in strict:
                    group_warnings = (['Group changed'] if scenario == 'group-warning'
                                      and label == 'window-2' else [])
                    return {'exists': True, 'window_id': strict[label], 'urls_restored': True,
                            'group_warnings': group_warnings}
                if label == 'window-6' and scenario == 'live-claim':
                    return {'exists': True, 'window_id': 84}
                return claimed.get(label, {'exists': False})
            self.assertEqual(action, 'recreate_window' if label == 'window-6' else 'restore_window')
            self.assertNotIn(label, strict)
            catalog = {entry['window']['id']: entry['window'] for entry in payload['restore_catalog']}
            if label in preserved:
                self.assertEqual(set(catalog), set(preserved))
                self.assertTrue(payload['reuse_only'])
                self.assertEqual(payload['expected_window_id'], preserved[label])
                result = {'exists': True, 'window_id': preserved[label], 'content_preserved': True,
                          'content_basis': 'current-native-session', 'reused': True, 'created': False}
            else:
                self.assertEqual(label, 'window-6')
                self.assertEqual(set(catalog), set(strict) | set(preserved) | {label})
                self.assertEqual(placed, [81, 82, 83, 84, 85])
                self.assertNotIn('reuse_only', payload)
                self.assertFalse(recreated)
                recreated.append(overlap_window(None, 0, [payload['window']['tabs'][0]['url']], runtime=86))
                result = {'exists': True, 'window_id': 86, 'urls_restored': True, 'created': True}
            claimed[label] = result
            return result

        def place(**options):
            if options.get('content_guard'):
                options['content_guard']()
            placed.append(options['chrome_window_id'])
            if scenario == 'handoff':
                raise browser.BrowserPlacementPending('awaiting placement', f'native-handoff:{placed[-1]}')
            if scenario == 'existing-placement-failed' and placed[-1] == 84:
                return False
            return not (scenario == 'placement-failed' and placed[-1] == 86 and placed.count(86) == 1)

        def query(token, timeout):
            self.assertGreater(timeout, 0)
            # A compositor-confirmed failure must leave this exact window pending.
            return {'token': token, 'status': 'failed' if token.endswith(':84') else 'verified'}

        argv = ['restore', 'browsers']
        if scenario != 'no-opt-in':
            argv.extend(['--recreate-window', 'Default/window-6'])
        args = cli.parser().parse_args(argv)
        with tempfile.TemporaryDirectory() as root, \
             patch.object(cli, '_startup_directory', return_value=Path(root)), \
             patch.object(cli, 'connected_profiles', return_value=['Default']), \
             patch.object(cli, 'browser_companion_info', return_value={
                 'protocol_version': 2, 'capabilities': [*browser.BROWSER_REQUIRED_CAPABILITIES, 'recreate_window']}), \
             patch.object(cli, 'request_browser', side_effect=request), \
             patch.object(browser, 'request_browser', side_effect=request), \
             patch.object(browser, 'expect_window', return_value='expected'), \
             patch.object(browser, 'cancel_expected_window'), \
             patch.object(browser, 'expected_window_status', return_value='placed'), \
             patch.object(browser, 'remap_monitor', side_effect=lambda p: p), \
             patch.object(browser, 'remap_workspace', side_effect=lambda p: p), \
             patch.object(browser, '_place_browser_window', side_effect=place), \
             patch.object(cli, '_close_startup_browser_duplicates') as cleanup, \
             patch.object(provider_progress, '_query_request', side_effect=query) as poll:
            count = cli._restore(snapshot, args)['browsers']
            self.assertEqual(count, {'recreated': 6, 'group-warning': 4}.get(scenario, 5))
            self.assertEqual(count.results[-1].success,
                             scenario in ('recreated', 'handoff', 'existing-placement-failed'))
            if scenario in ('recreated', 'placement-failed', 'handoff', 'existing-placement-failed'):
                self.assertEqual(len(recreated), 1)
                self.assertEqual(placed, list(range(81, 87)))
                if scenario in ('recreated', 'placement-failed'):
                    self.assertEqual(cli._restore(snapshot, args)['browsers'], 6)
                    self.assertEqual(len(recreated), 1)
            else:
                self.assertFalse(recreated)
                self.assertEqual(placed, [81, 83, 84, 85] if scenario == 'group-warning' else list(range(81, 86)))
            if scenario == 'handoff':
                self.assertEqual({call.args[0] for call in poll.call_args_list},
                                 {f'native-handoff:{window_id}' for window_id in range(81, 87)})
                details = {item.item_id: item.placement for item in count.results}
                self.assertEqual(details['Default/window-6'].state, S.VERIFIED)
                self.assertEqual(details['Default/window-4'].state, S.FAILED)
            else:
                poll.assert_not_called()
            self.assertFalse((Path(root) / 'browser-reconciliation.json').exists())
            cleanup.assert_not_called()
        self.assertEqual((snapshot, native), original)

    def test_unreadable_profile_fails_its_own_windows_without_restoring(self):
        snapshot, native = fixture()
        with ExitStack() as stack:
            _, _, _, request, restore, _ = existing_tests.BrowserReconciliationCliTests().harness(stack, snapshot, native)
            def failed_capture(action, *args, **kwargs):
                if action == 'capture':
                    raise browser.BrowserUnavailable('read-only capture unavailable')
                return {'exists': False}
            request.side_effect = failed_capture
            with self.assertRaisesRegex(cli.ProviderRestoreError, 'Default/window-10.*read-only capture unavailable'):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=False, login_status=False), start_browser=True)
            restore.assert_not_called()

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
        from workspace_state.provider_progress import stage_verified
        self.assertTrue(stage_verified({'state': 'ready', 'provider_results': [result.evidence.to_dict()]}))

    def run_placement(self, catalog, capture):
        def request(action, payload, **kwargs):
            if action == 'capture':
                return capture()
            return {'window_id': 71, 'created': False, 'reused': True, 'urls_restored': False,
                    'exists': True, 'content_preserved': True, 'content_basis': 'current-native-session'}
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
        return result, submitted

    def test_changed_tab_after_prepare_still_submits_native_move(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        mutated = deepcopy(native['Default'])
        mutated['windows'][0]['runtime_identity']['tabs'][2][0] = 999
        mutated['windows'][0]['tabs'][3]['url'] = 'https://docs.example.test/redirected'
        result, submitted = self.run_placement(catalog, lambda: mutated)
        self.assertTrue(result.success)
        self.assertEqual(len(submitted), 1)

    def test_claimed_window_missing_after_prepare_blocks_native_move(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        mutated = deepcopy(native['Default'])
        mutated['windows'][0]['runtime_window_id'] = 72
        result, submitted = self.run_placement(catalog, lambda: mutated)
        self.assertFalse(result.success)
        self.assertEqual(result.evidence.placement.state, S.FAILED)
        self.assertFalse(submitted)

    def test_other_window_change_and_new_window_do_not_block_native_move(self):
        saved = [overlap_window(f'window-{n}', n, [f'https://w{n}.example.test/a', f'https://w{n}.example.test/b'])
                 for n in (1, 2)]
        live = [overlap_window(None, 0, [f'https://w{n}.example.test/a#loaded', f'https://w{n}.example.test/b'], runtime=70 + n)
                for n in (1, 2)]
        full, _ = r.reconcile_navigation(*overlap_catalog(saved, live))
        mutated = deepcopy(live)
        mutated[1]['tabs'].append(dict(mutated[1]['tabs'][0], url='https://w2.example.test/new'))
        mutated[1]['tabs'][0]['url'] = 'https://w2.example.test/redirected'
        mutated.append(overlap_window(None, 3, ['https://unrelated.example.test/'], runtime=99))
        _, native = overlap_catalog(saved, mutated)
        # Place only window 71; the other catalog window changed and a new one appeared.
        only = {'profiles': [dict(full['profiles'][0], windows=full['profiles'][0]['windows'][:1])]}
        self.assertEqual(only['profiles'][0]['windows'][0]['_reconcile_window_id'], 71)
        def request(action, payload, **kwargs):
            if action == 'capture':
                return deepcopy(native['Default'])
            return {'window_id': 71, 'created': False, 'reused': True, 'urls_restored': False,
                    'exists': True, 'content_preserved': True, 'content_basis': 'current-native-session'}
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
            result = browser.restore_browser(only, restore_catalog=full)[0]
        self.assertTrue(result.success)
        self.assertEqual(len(submitted), 1)

    def test_content_mutating_between_captures_does_not_block_any_window(self):
        saved = [overlap_window(f'window-{n}', n, [f'https://w{n}.example.test/a', f'https://w{n}.example.test/b'])
                 for n in (1, 2, 3)]
        live = [overlap_window(None, 0, [f'https://w{n}.example.test/a#loaded', f'https://w{n}.example.test/b'], runtime=80 + n)
                for n in (1, 2, 3)]
        catalog, _ = r.reconcile_navigation(*overlap_catalog(saved, live))
        calls, tokens = [], {}
        def request(action, payload, **kwargs):
            if action == 'capture':
                calls.append(1)
                changed = deepcopy(live)
                for index, window in enumerate(changed):  # Pages keep loading between captures.
                    window['tabs'][0]['url'] = f'https://redirect{len(calls)}.example.test/{index}'
                    window['tabs'].append(dict(window['tabs'][1], url=f'https://late{len(calls)}.example.test/'))
                    window['runtime_identity']['tabs'][0][0] += 1000 * len(calls)
                changed.append(overlap_window(None, 4, ['https://unrelated.example.test/'], runtime=200 + len(calls)))
                return overlap_catalog(saved, changed)[1]['Default']
            if action == 'restore_window':
                tokens[payload['restore_token']] = payload['expected_window_id']
            return {'window_id': tokens.get(payload['restore_token'], 0), 'created': False, 'reused': True,
                    'urls_restored': False, 'exists': True, 'content_preserved': True,
                    'content_basis': 'current-native-session'}
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
            results = browser.restore_browser(catalog, restore_catalog=catalog)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.success for result in results))
        self.assertEqual(sorted(call.kwargs['chrome_window_id'] for call in placement.call_args_list), [81, 82, 83])
        self.assertGreaterEqual(len(calls), 3)

    def test_problem_helpers_tolerate_malformed_native_state(self):
        snapshot, native = fixture()
        catalog, _ = r.reconcile_navigation(snapshot, native)
        bad = deepcopy(native)
        bad['Default']['windows'] = None
        self.assertEqual(r.preservation_problems(catalog, bad), {('Default', 71): 'current native window is missing'})
        bad['Default']['windows'] = ['junk', None]
        self.assertEqual(len(r.preservation_problems(catalog, bad)), 1)

    def test_final_verification_fails_only_the_missing_window(self):
        saved = [overlap_window(f'window-{n}', n, [f'https://w{n}.example.test/a', f'https://w{n}.example.test/b'])
                 for n in (1, 2, 3)]
        live = [overlap_window(None, 0, [f'https://w{n}.example.test/a#loaded', f'https://w{n}.example.test/b'], runtime=80 + n)
                for n in (1, 2, 3)]
        snapshot, native = overlap_catalog(saved, live)
        with ExitStack() as stack:
            harness = existing_tests.BrowserReconciliationCliTests()
            *_, request, restore, _cleanup = harness.harness(stack, snapshot, native)
            captures = []
            def request_side_effect(action, params, **kwargs):
                if action == 'capture':
                    captures.append(1)
                    current = deepcopy(native['Default'])
                    if len(captures) > 1:  # Final verification: window 82 is gone, 81 loads a new page.
                        current['windows'] = [w for w in current['windows'] if w['runtime_window_id'] != 82]
                        current['windows'][0]['tabs'][0]['url'] = 'https://w1.example.test/redirected'
                    return current
                return {'exists': False}
            request.side_effect = request_side_effect
            restore.side_effect = lambda *a, **k: [browser.BrowserRestoreResult('reused', True, evidence=ProviderItemResult(
                'chrome', f"Default/{a[0]['profiles'][0]['windows'][0]['id']}",
                PhaseEvidence(S.VERIFIED), PhaseEvidence(S.VERIFIED), PhaseEvidence(S.VERIFIED)))]
            with self.assertRaises(cli.ProviderRestoreError) as raised:
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=False, login_status=False), start_browser=True)
        by_item = {item.item_id: item for item in raised.exception.results}
        self.assertEqual(by_item['Default/window-2'].placement.state, S.FAILED)
        self.assertFalse(by_item['Default/window-2'].success)
        self.assertEqual(by_item['Default/window-2'].placement.detail, 'current native window is missing')
        self.assertEqual(len(by_item), 3)
        self.assertEqual([by_item[f'Default/window-{n}'].success for n in (1, 3)], [True, True])
        # Classified like any other per-item failure: some items verified means degraded.
        with patch.object(cli, 'update_stage') as stage:
            self.assertEqual(cli._publish_category_outcome('browsers', snapshot, error=raised.exception), 'degraded')
        self.assertEqual(stage.call_args.args[1], 'degraded')
        message = str(raised.exception)
        self.assertIn('Default/window-2: Browser reconciliation needs review: current native window is missing', message)
        self.assertIn('Restored 2/3 Chrome window(s)', message)
        self.assertNotIn('window-1', message)
        self.assertNotIn('window-3', message)

    def test_unpaired_windows_fail_individually_and_paired_ones_are_verified(self):
        snapshot, native = partial_catalog()
        with ExitStack() as stack:
            root, _owner, _, request, restore, _cleanup = existing_tests.BrowserReconciliationCliTests().harness(
                stack, snapshot, native)
            captures = []
            def request_side_effect(action, params, **kwargs):
                if action == 'capture':
                    captures.append(1)
                    return deepcopy(native['Default'])
                return {'exists': False}
            request.side_effect = request_side_effect
            restore.side_effect = lambda *a, **k: [browser.BrowserRestoreResult('ok', True, evidence=ProviderItemResult(
                'chrome', f"Default/{a[0]['profiles'][0]['windows'][0]['id']}",
                PhaseEvidence(S.VERIFIED), PhaseEvidence(S.VERIFIED), PhaseEvidence(S.VERIFIED)))]
            with self.assertRaises(cli.ProviderRestoreError) as raised:
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=False, login_status=False), start_browser=True)
            restored = [call.args[0]['profiles'][0]['windows'][0]['id'] for call in restore.call_args_list]
            receipt_calls = [call for call in request.call_args_list
                             if call.args[0] not in {'capture', 'restore_status'}]
            self.assertEqual([w['id'] for w in restore.call_args.kwargs['restore_catalog']['profiles'][0]['windows']],
                             [f'window-{n}' for n in range(1, 6)])
            receipt = json.loads((root / 'browser-reconciliation.json').read_text())
        self.assertEqual(restored, [f'window-{n}' for n in range(1, 6)])
        self.assertEqual(receipt_calls, [])  # No native call for an unpaired window.
        self.assertEqual(receipt['state'], 'failed')
        self.assertEqual(receipt['verified_windows'], 5)
        self.assertEqual(receipt['unpaired_windows'], {'Default': ['window-6', 'window-7']})
        self.assertEqual(len(receipt['failures']), 2)
        self.assertEqual(len(captures), 2)  # Preflight plus the final verification.
        by_item = {item.item_id: item for item in raised.exception.results}
        self.assertEqual(len(by_item), 7)
        for label in ('window-6', 'window-7'):
            item = by_item[f'Default/{label}']
            self.assertEqual(item.placement.state, S.FAILED)
            self.assertEqual(item.placement.detail, 'no current native window pairs with its saved tabs unambiguously')
            self.assertFalse(item.success)
        self.assertTrue(all(by_item[f'Default/window-{n}'].success for n in range(1, 6)))
        message = str(raised.exception)
        self.assertIn('Restored 5/7 Chrome window(s)', message)
        self.assertIn('Default/window-6: Browser reconciliation needs review: no current native window', message)
        with patch.object(cli, 'update_stage') as stage:
            self.assertEqual(cli._publish_category_outcome('browsers', snapshot, error=raised.exception), 'degraded')
        self.assertEqual(stage.call_args.args[1], 'degraded')

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
