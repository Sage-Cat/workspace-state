"""Synthetic retained-capture proofs; these never operate on a real browser."""
from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_state import browser_reconciliation as reconciliation, browser, cli, operations
from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult


def fixture():
    def window(label, url, group=None):
        return {'id': label, 'type': 'normal', 'incognito': False,
                'tabs': [{'url': url, 'pinned': False, 'group': group}],
                'groups': ([{'id': group, 'title': 'Work', 'color': 'blue', 'collapsed': False}]
                           if group else []),
                'workspace_index': 2, 'monitor': {'index': 0},
                'geometry': {'x': 0, 'y': 0, 'width': 800, 'height': 600}}
    profile = {'profile': 'Default', 'profile_directory': 'Default', 'app_id': 'chrome',
               'windows': [window('window-1', 'https://example.test/new', 'g1'),
                           window('window-2', 'https://example.test/other')]}
    observed = {'available': True, 'errors': [], 'profiles': [profile]}
    saved = deepcopy(observed)
    saved['profiles'][0]['windows'][0]['tabs'][0]['url'] = 'https://example.test/old'
    snapshot = {'created_at': '2026-10-03T20:00:00+00:00',
                'browsers': {'google_chrome': saved},
                'capture_context': {'schema_version': 2, 'captured_at': '2026-10-03T20:00:00.125+00:00',
                                    'capture_id': '00000000-0000-4000-8000-000000000001',
                                    'completed_at': '2026-10-03T20:00:04.500+00:00',
                                    'snapshot_created_at': '2026-10-03T20:00:00+00:00',
                                    'provider_evidence': {'browsers': {'state': 'captured',
                                                                    'content_digest': reconciliation.digest(observed)}}},
                'capture_errors': {'preserved_categories': [
                    'retained the saved browsers recipe because restoration did not complete this login']}}
    saved['latest_observation'] = reconciliation.capture_observation(snapshot, saved, deepcopy(observed))
    live = deepcopy(profile)
    for i, item in enumerate(live['windows']):
        item['runtime_window_id'] = 100 + i
    return snapshot, {'Default': live}


def observation(snapshot):
    return snapshot['browsers']['google_chrome']['latest_observation']


def bind_fixture(snapshot):
    """Reissue synthetic evidence after an intentional fixture content change."""
    saved = snapshot['browsers']['google_chrome']
    observation(snapshot)['browser_digest'] = reconciliation.digest(observation(snapshot)['browser_state'])
    observation(snapshot)['retained_recipe_digest'] = reconciliation.recipe_digest(saved)
    snapshot['capture_context']['provider_evidence']['browsers']['content_digest'] = observation(snapshot)['browser_digest']


class BrowserReconciliationTests(unittest.TestCase):
    def assertRejected(self, snapshot, native):
        before = deepcopy((snapshot, native))
        with self.assertRaises(reconciliation.ReconciliationRequired) as raised:
            reconciliation.reconcile(snapshot, native)
        self.assertIn('preserved; no replacement was created', str(raised.exception))
        self.assertEqual((snapshot, native), before)

    def test_exact_newer_catalog_reuses_only_without_mutating_checkpoint_or_live_input(self):
        snapshot, native = fixture()
        before = deepcopy((snapshot, native))
        result, receipt = reconciliation.reconcile(snapshot, native)
        self.assertEqual((snapshot, native), before)
        self.assertEqual(receipt['native_windows'], {'Default': {'window-1': 100, 'window-2': 101}})
        self.assertFalse(receipt['canonical_checkpoint_changed'])
        self.assertEqual(receipt['source_checkpoint_digest'], reconciliation.digest(snapshot))
        self.assertEqual(receipt['observation_digest'], reconciliation.digest(observation(snapshot)))
        self.assertEqual(result['profiles'][0]['windows'][0]['_reconcile_window_id'], 100)
        result['profiles'][0]['windows'][0]['tabs'].clear()
        self.assertEqual((snapshot, native), before)

    def test_runtime_ids_group_ids_window_order_and_titles_are_not_identity(self):
        snapshot, native = fixture()
        windows = native['Default']['windows']
        windows[0]['groups'][0]['id'] = 'runtime-group-77'
        windows[0]['tabs'][0]['group'] = 'runtime-group-77'
        for i, item in enumerate(windows):
            item.update(runtime_window_id=501 + i, id='different-label', title='Loading', focused=True)
        windows.reverse()
        _, receipt = reconciliation.reconcile(snapshot, native)
        self.assertEqual(receipt['native_windows']['Default'], {'window-1': 501, 'window-2': 502})

    def test_multiple_profiles_allow_equal_runtime_ids_only_within_distinct_profiles(self):
        snapshot, native = fixture()
        saved = snapshot['browsers']['google_chrome']
        for state in (saved, observation(snapshot)['browser_state']):
            extra = deepcopy(state['profiles'][0])
            extra.update(profile='Second', profile_directory='Profile 1')
            state['profiles'].append(extra)
        native['Second'] = deepcopy(native['Default'])
        native['Second'].update(profile='Second', profile_directory='Profile 1')
        bind_fixture(snapshot)
        _, receipt = reconciliation.reconcile(snapshot, native)
        self.assertEqual(set(receipt['native_windows']), {'Default', 'Second'})
        self.assertEqual(receipt['native_windows']['Default'], receipt['native_windows']['Second'])

    def test_no_observation_is_not_a_reconciliation_request(self):
        snapshot, native = fixture()
        del snapshot['browsers']['google_chrome']['latest_observation']
        self.assertIsNone(reconciliation.reconcile(snapshot, native))

    def test_incomplete_or_unbound_capture_rejected(self):
        mutations = {
            'schema': lambda s: observation(s).update(schema_version=3),
            'missing timestamp': lambda s: observation(s).pop('captured_at'),
            'naive timestamp': lambda s: observation(s).update(captured_at='2026-10-03T20:00:00'),
            'bad timestamp': lambda s: observation(s).update(captured_at='not-a-time'),
            'wrong capture time': lambda s: s['capture_context'].update(captured_at='2026-10-03T21:00:00+00:00'),
            'stale outer timestamp': lambda s: s.update(created_at='2026-10-04T20:00:00+00:00'),
            'missing retention evidence': lambda s: s['capture_errors'].update(preserved_categories=[]),
            'failed provider': lambda s: s['capture_context']['provider_evidence']['browsers'].update(state='failed'),
            'unavailable': lambda s: observation(s)['browser_state'].update(available=False),
            'capture errors': lambda s: observation(s)['browser_state'].update(errors=['partial capture']),
            'missing profiles': lambda s: observation(s)['browser_state'].update(profiles=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot, native = fixture()
                mutate(snapshot)
                self.assertRejected(snapshot, native)

    def test_profile_identity_must_match_retained_observation_and_native_inventory(self):
        for target in ('observed', 'native'):
            for key in ('profile', 'profile_directory', 'app_id'):
                with self.subTest(target=target, key=key):
                    snapshot, native = fixture()
                    profile = (observation(snapshot)['browser_state']['profiles'][0]
                               if target == 'observed' else native['Default'])
                    profile[key] = 'other'
                    self.assertRejected(snapshot, native)
        for extra in (False, True):
            snapshot, native = fixture()
            if extra:
                native['Extra'] = deepcopy(native['Default'])
            else:
                native.clear()
            self.assertRejected(snapshot, native)

    def test_native_content_changes_reject_even_when_counts_and_overlap_match(self):
        mutations = {
            'URL': lambda w: w['tabs'][0].update(url='https://example.test/different'),
            'pin': lambda w: w['tabs'][0].update(pinned=True),
            'group title': lambda w: w['groups'][0].update(title='Changed'),
            'group color': lambda w: w['groups'][0].update(color='red'),
            'group collapsed': lambda w: w['groups'][0].update(collapsed=True),
            'ungrouped': lambda w: (w['tabs'][0].update(group=None), w.update(groups=[])),
            'incognito': lambda w: w.update(incognito=True),
            'window type': lambda w: w.update(type='popup'),
            'missing URL': lambda w: w['tabs'][0].pop('url'),
            'missing group': lambda w: w.update(groups=[]),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot, native = fixture()
                mutate(native['Default']['windows'][0])
                self.assertRejected(snapshot, native)

    def test_extra_missing_duplicate_windows_and_duplicate_runtime_ids_reject(self):
        for mode in ('missing', 'extra', 'duplicate signature', 'duplicate runtime id', 'boolean runtime id'):
            with self.subTest(mode=mode):
                snapshot, native = fixture()
                windows = native['Default']['windows']
                if mode == 'missing': windows.pop()
                elif mode == 'extra': windows.append(deepcopy(windows[0]))
                elif mode == 'duplicate signature':
                    windows[1] = deepcopy(windows[0]); windows[1]['runtime_window_id'] = 102
                elif mode == 'duplicate runtime id': windows[1]['runtime_window_id'] = 100
                else: windows[1]['runtime_window_id'] = True
                self.assertRejected(snapshot, native)

    def test_ambiguous_observation_and_missing_placement_reject(self):
        for mode in ('duplicate label', 'duplicate content', 'missing workspace', 'boolean workspace', 'missing monitor', 'missing geometry'):
            with self.subTest(mode=mode):
                snapshot, native = fixture()
                windows = observation(snapshot)['browser_state']['profiles'][0]['windows']
                if mode == 'duplicate label': windows[1]['id'] = windows[0]['id']
                elif mode == 'duplicate content':
                    windows[1] = deepcopy(windows[0]); windows[1]['id'] = 'another'
                elif mode == 'missing workspace': windows[0].pop('workspace_index')
                elif mode == 'boolean workspace': windows[0]['workspace_index'] = True
                elif mode == 'missing monitor': windows[0].pop('monitor')
                else: windows[0].pop('geometry')
                self.assertRejected(snapshot, native)

    def test_tab_order_and_group_membership_are_content_not_just_url_sets(self):
        for mode in ('order', 'membership'):
            with self.subTest(mode=mode):
                snapshot, native = fixture()
                expected = observation(snapshot)['browser_state']['profiles'][0]['windows'][0]
                expected['tabs'].append({'url': 'https://example.test/second', 'pinned': False, 'group': None})
                native['Default']['windows'][0] = deepcopy(expected)
                live = native['Default']['windows'][0]
                live['runtime_window_id'] = 100
                if mode == 'order': live['tabs'].reverse()
                else: live['tabs'][1]['group'] = 'g1'
                self.assertRejected(snapshot, native)

    def test_typed_content_and_complete_finite_geometry_required(self):
        mutations = {
            'missing incognito': lambda w: w.pop('incognito'),
            'string incognito': lambda w: w.update(incognito='false'),
            'missing pinned': lambda w: w['tabs'][0].pop('pinned'),
            'string pinned': lambda w: w['tabs'][0].update(pinned='false'),
            'missing collapsed': lambda w: w['groups'][0].pop('collapsed'),
            'missing color': lambda w: w['groups'][0].pop('color'),
            'missing title': lambda w: w['groups'][0].pop('title'),
            'empty geometry': lambda w: w.update(geometry={}),
            'NaN geometry': lambda w: w['geometry'].update(x=float('nan')),
            'infinite geometry': lambda w: w['geometry'].update(width=float('inf')),
            'zero width': lambda w: w['geometry'].update(width=0),
            'boolean width': lambda w: w['geometry'].update(width=True),
            'negative monitor': lambda w: w['monitor'].update(index=-1),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                snapshot, native = fixture()
                mutate(observation(snapshot)['browser_state']['profiles'][0]['windows'][0])
                self.assertRejected(snapshot, native)


class ReconciliationWindowCountTests(unittest.TestCase):
    def test_newer_complete_observation_can_have_more_windows_than_old_recipe(self):
        snapshot, native = fixture()
        snapshot['browsers']['google_chrome']['profiles'][0]['windows'].pop()
        bind_fixture(snapshot)
        original = deepcopy(snapshot)
        observed, proof = reconciliation.reconcile(snapshot, native)
        self.assertEqual(len(observed['profiles'][0]['windows']), 2)
        self.assertEqual(len(proof['native_windows']['Default']), 2)
        self.assertEqual(snapshot, original)


class BrowserReconciliationCliTests(unittest.TestCase):
    def harness(self, stack, snapshot, native):
        root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        status = root / 'status.json'
        status.write_text('{"operation_state":"running"}')
        owner = Mock(mode='startup')
        stack.enter_context(patch('workspace_state.startup.startup_suspended', return_value=False))
        owner.matches.return_value = True
        owner.to_dict.return_value = {'operation_id': 'synthetic-operation'}
        stack.enter_context(patch.object(operations, 'current', return_value=owner))
        stack.enter_context(patch.object(cli, 'status_path', return_value=status))
        stack.enter_context(patch.object(cli, '_startup_directory', return_value=root))
        stack.enter_context(patch.object(cli, 'connected_profiles', return_value=['Default']))
        stack.enter_context(patch.object(cli, 'ensure_browser_profiles', return_value=[]))
        stack.enter_context(patch.object(cli, 'wait_for_browser_settle'))
        companion = stack.enter_context(patch.object(cli, 'browser_companion_info', return_value={
            'protocol_version': 2, 'capabilities': list(BROWSER_REQUIRED_CAPABILITIES)}))
        request = stack.enter_context(patch.object(cli, 'request_browser', side_effect=lambda action, params, **kw:
            deepcopy(native[kw['profile']]) if action == 'capture' else {'exists': False}))
        restore = stack.enter_context(patch.object(cli, 'restore_browser', return_value=[BrowserRestoreResult('reused', True)]))
        stack.enter_context(patch.object(cli, '_write_attempt_marker'))
        stack.enter_context(patch.object(cli, 'save', side_effect=AssertionError('must not save canonical checkpoint')))
        cleanup = stack.enter_context(patch.object(cli, '_close_startup_browser_duplicates'))
        return root, owner, companion, request, restore, cleanup

    def test_startup_uses_observation_only_for_attempt_and_writes_scoped_receipt(self):
        snapshot, native = fixture()
        original = deepcopy(snapshot)
        with ExitStack() as stack:
            root, owner, _, _, restore, cleanup = self.harness(stack, snapshot, native)
            count = cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                no_place=True, login_status=False), start_browser=True)
            self.assertEqual(count, 2)
            self.assertEqual(snapshot, original)
            cleanup.assert_not_called()
            self.assertEqual(restore.call_count, 2)
            self.assertEqual([call.args[0]['profiles'][0]['windows'][0]['_reconcile_window_id']
                              for call in restore.call_args_list], [100, 101])
            receipt = json.loads((root / 'browser-reconciliation.json').read_text())
            self.assertEqual(receipt['operation_context'], owner.to_dict.return_value)
            self.assertFalse(receipt['canonical_checkpoint_changed'])
            self.assertEqual(sorted(p.name for p in root.iterdir()), ['browser-items', 'browser-reconciliation.json', 'status.json'])

    def test_missing_changed_or_expired_owner_never_calls_restore(self):
        for mode in ('missing', 'shutdown', 'changed', 'expired'):
            with self.subTest(mode=mode), ExitStack() as stack:
                snapshot, native = fixture()
                root, owner, _, _, restore, cleanup = self.harness(stack, snapshot, native)
                if mode == 'missing':
                    stack.enter_context(patch.object(operations, 'current', return_value=None))
                elif mode == 'shutdown': owner.mode = 'shutdown'
                elif mode == 'changed': owner.matches.side_effect = [True, False]
                else: owner.check.side_effect = RuntimeError('expired')
                with self.assertRaises(RuntimeError):
                    cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                        no_place=True, login_status=False), start_browser=True)
                restore.assert_not_called()
                cleanup.assert_not_called()
                self.assertFalse((root / 'browser-reconciliation.json').exists())

    def test_authority_revoked_during_first_restore_stops_before_marker_or_second(self):
        snapshot, native = fixture()
        with ExitStack() as stack:
            root, owner, _, _, restore, _ = self.harness(stack, snapshot, native)
            marker = stack.enter_context(patch.object(cli, '_write_attempt_marker'))
            def revoke(*args, **kwargs):
                owner.matches.return_value = False
                return [BrowserRestoreResult('reused', True)]
            restore.side_effect = revoke
            with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=True, login_status=False), start_browser=True)
            self.assertEqual(restore.call_count, 1)
            marker.assert_not_called()
            receipt = json.loads((root / 'browser-reconciliation.json').read_text())
            self.assertEqual(receipt['state'], 'matched-reuse-only')

    def test_suspended_or_terminal_startup_never_launches(self):
        for condition in ('suspended', 'completed', 'failed'):
            with self.subTest(condition=condition), ExitStack() as stack:
                snapshot, native = fixture()
                root, _, _, _, restore, _ = self.harness(stack, snapshot, native)
                launch = stack.enter_context(patch.object(cli, 'ensure_browser_profiles'))
                if condition == 'suspended':
                    stack.enter_context(patch('workspace_state.startup.startup_suspended', return_value=True))
                else:
                    (root / 'status.json').write_text(json.dumps({'operation_state': condition}))
                with self.assertRaisesRegex(RuntimeError, 'ownership changed'):
                    cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                        no_place=True, login_status=False), start_browser=True)
                launch.assert_not_called()
                restore.assert_not_called()

    def test_outdated_companion_does_not_restore(self):
        snapshot, native = fixture()
        with ExitStack() as stack:
            _, _, companion, _, restore, _ = self.harness(stack, snapshot, native)
            companion.return_value = {'protocol_version': 2, 'capabilities':
                sorted(BROWSER_REQUIRED_CAPABILITIES - {'reconciliation_reuse_only'})}
            with self.assertRaisesRegex(RuntimeError, 'outdated'):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=True, login_status=False), start_browser=True)
            restore.assert_not_called()

    def test_live_mismatch_never_restores_closes_or_publishes_success_receipt(self):
        snapshot, native = fixture()
        native['Default']['windows'][0]['tabs'][0]['url'] = 'https://example.test/new-user-edit'
        before = deepcopy(snapshot)
        with ExitStack() as stack:
            root, _, _, request, restore, cleanup = self.harness(stack, snapshot, native)
            with self.assertRaises(reconciliation.ReconciliationRequired):
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                    no_place=True, login_status=False), start_browser=True)
            self.assertEqual([call.args[0] for call in request.call_args_list], ['capture'])
            restore.assert_not_called()
            cleanup.assert_not_called()
            self.assertFalse((root / 'browser-reconciliation.json').exists())
            self.assertEqual(snapshot, before)

    def test_scoped_restore_rejected_before_launch(self):
        snapshot, _ = fixture()
        with patch.object(cli, 'ensure_browser_profiles') as launch:
            with self.assertRaisesRegex(RuntimeError, 'full-profile'):
                cli._restore_browsers(snapshot, Namespace(workspace=1, dry_run=False), start_browser=True)
            launch.assert_not_called()


class ReconciliationPlacementTests(unittest.TestCase):
    def test_revoked_native_verification_cancels_expectation_without_placement(self):
        snapshot, native = fixture()
        observed, _ = reconciliation.reconcile(snapshot, native)
        guard = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(browser, 'remap_workspace', side_effect=lambda x:x))
            stack.enter_context(patch.object(browser, 'remap_monitor', side_effect=lambda x:x))
            stack.enter_context(patch.object(browser, 'expect_window', return_value='expected'))
            cancel = stack.enter_context(patch.object(browser, 'cancel_expected_window'))
            def reply(*args, **kwargs):
                guard.side_effect = RuntimeError('revoked')
                return {'window_id':100, 'created':False, 'reused':True, 'urls_restored':True}
            stack.enter_context(patch.object(browser, 'request_browser', side_effect=reply))
            place = stack.enter_context(patch.object(browser, '_place_browser_window'))
            with self.assertRaisesRegex(RuntimeError, 'revoked'):
                browser.restore_browser(observed, restore_token_prefix='test', authority_guard=guard)
            place.assert_not_called()
            cancel.assert_called_once_with('expected')

    def test_revocation_during_identify_reply_releases_owned_token(self):
        guard = Mock()
        with ExitStack() as stack:
            def reply(action, payload, **kwargs):
                guard.side_effect = RuntimeError('revoked')
                return {'window_id':100, 'token':payload['token'], 'marker_tab_id':200}
            stack.enter_context(patch.object(browser, 'request_browser', side_effect=reply))
            release = stack.enter_context(patch.object(browser, '_release_window_identification'))
            shell = stack.enter_context(patch.object(browser, 'capture_shell'))
            with self.assertRaisesRegex(RuntimeError, 'revoked'):
                browser._identify_native_window(profile='Default', chrome_window_id=100,
                    app_id='google-chrome', authority_guard=guard)
            release.assert_called_once()
            self.assertEqual(release.call_args.args[1]['window_id'], 100)
            self.assertTrue(release.call_args.args[1]['token'])
            shell.assert_not_called()

    def test_revocation_after_identification_still_releases_lease_without_move(self):
        guard = Mock()
        lease = {'window_id':100, 'token':'synthetic'}
        with ExitStack() as stack:
            def identified(**kwargs):
                guard.side_effect = RuntimeError('revoked')
                return 42, lease
            stack.enter_context(patch.object(browser, '_identify_native_window', side_effect=identified))
            release = stack.enter_context(patch.object(browser, '_release_window_identification'))
            move = stack.enter_context(patch.object(browser, 'move_window_result'))
            with self.assertRaisesRegex(RuntimeError, 'revoked'):
                browser._place_browser_window(profile='Default', chrome_window_id=100,
                    app_id='google-chrome', placement={'workspace':2}, authority_guard=guard)
            release.assert_called_once_with('Default', lease)
            move.assert_not_called()

    def test_revocation_during_staging_prevents_final_workspace_handoff(self):
        guard = Mock()
        with ExitStack() as stack:
            stack.enter_context(patch.object(browser, '_identify_native_window', return_value=(42, {})))
            stack.enter_context(patch.object(browser, '_release_window_identification'))
            stack.enter_context(patch.object(browser, 'capture_shell', return_value={'active_workspace':0}))
            move = stack.enter_context(patch.object(browser, 'move_window_result', return_value={'status':'verified'}))
            def observed(*args):
                guard.side_effect = RuntimeError('revoked')
                return True
            stack.enter_context(patch.object(browser, '_wait_for_native_placement', side_effect=observed))
            with self.assertRaisesRegex(RuntimeError, 'revoked'):
                browser._place_browser_window(profile='Default', chrome_window_id=100,
                    app_id='google-chrome', placement={'workspace':2}, authority_guard=guard)
            self.assertEqual(move.call_count, 1)
            self.assertEqual(move.call_args.args[1]['workspace'], 0)

    def test_unverified_reconciled_content_never_moves_window(self):
        for result in ({"urls_restored": False, "url_errors": ["Changed URL"]},
                       {"urls_restored": True, "group_warnings": ["Group changed"]}):
            with self.subTest(result=result), ExitStack() as stack:
                snapshot, native = fixture()
                observed, _ = reconciliation.reconcile(snapshot, native)
                stack.enter_context(patch.object(browser, 'remap_workspace', side_effect=lambda x:x))
                stack.enter_context(patch.object(browser, 'remap_monitor', side_effect=lambda x:x))
                stack.enter_context(patch.object(browser, 'expect_window', return_value='expected'))
                stack.enter_context(patch.object(browser, 'cancel_expected_window'))
                request = stack.enter_context(patch.object(browser, 'request_browser', return_value={
                    'window_id':100, 'created':False, 'reused':True, **result}))
                place = stack.enter_context(patch.object(browser, '_place_browser_window'))
                results = browser.restore_browser(observed, restore_token_prefix='test')
                self.assertTrue(all(not r.success for r in results))
                place.assert_not_called()
                self.assertTrue(all(c.kwargs['profile']=='Default' and c.args[1]['reuse_only'] is True
                                    for c in request.call_args_list))
