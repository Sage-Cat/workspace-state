"""Isolated regression for retained Chrome content that verifies after restore returns."""
from contextlib import ExitStack
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_state import browser, login_status, operations, provider_progress as progress
from workspace_state import cli, login_finalize
from workspace_state.provider_results import EvidenceState as S
from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker
from workspace_state.util import atomic_json
from test_browser_reconciliation import fixture
from workspace_state.browser_reconciliation import reconcile


_place_browser_window = browser._place_browser_window


class PendingPlacementTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.root)}))
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        login_status.runtime_root().mkdir()
        (login_status.runtime_root() / 'login-generation').write_text('abc123')
        login_status.initialize('abc123')
        self.context = operations.OperationContext.from_dict(self.document()['operation_context'])
        operations.bind(self.context)
        snapshot, self.native = fixture()
        self.catalog, _ = reconcile(snapshot, self.native)
        self.one = deepcopy(self.catalog)
        self.one['profiles'][0]['windows'] = self.one['profiles'][0]['windows'][:1]
        self.stack.enter_context(patch.object(browser, 'remap_workspace', side_effect=lambda x:x))
        self.stack.enter_context(patch.object(browser, 'remap_monitor', side_effect=lambda x:x))
        self.stack.enter_context(patch.object(browser, 'expect_window', return_value='expected'))
        self.stack.enter_context(patch.object(browser, 'cancel_expected_window'))
        self.place = self.stack.enter_context(patch.object(browser, '_place_browser_window', return_value=True))

    def document(self):
        return json.loads(login_status.status_path().read_text())

    def initial(self):
        with patch.object(browser, 'request_browser', return_value={
            'window_id':100, 'created':False, 'reused':True,
            'urls_restored':False, 'urls_pending':True, 'url_errors':['Tab 1 is still loading'],
        }):
            return browser.restore_browser(self.one, restore_token_prefix='test',
                restore_catalog=self.catalog, authority_guard=self.context.check)[0]

    def test_pending_exact_content_defers_unsubmitted_placement_instead_of_failing(self):
        result = self.initial()
        self.assertFalse(result.success)
        self.assertEqual(result.evidence.content.state, S.WAITING)
        self.assertEqual(result.evidence.placement.state, S.WAITING)
        self.assertTrue(result.evidence.placement.request_id)
        self.assertIn('content', result.evidence.placement.detail.lower())
        self.place.assert_not_called()

    def seed(self, result):
        for category, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(category, 'ready', 'Verified')
        login_status.update_stage('login-finalization', 'running', 'Waiting')
        login_status.update_stage('browsers', 'waiting', 'Loading exact content')
        login_status.publish_provider_results('browsers', [result.evidence])
        directory = cli._startup_directory()
        write_stage_marker(directory / 'browsers.done', StageMarker('browsers', 'waiting', 'synthetic',
            operation_context=self.context.to_dict(), provider_results=(result.evidence.to_dict(),)))
        self.item_marker = directory / 'browser-items' / 'synthetic.done'
        write_stage_marker(self.item_marker, read_stage_marker(directory / 'browsers.done'))
        atomic_json(directory / 'browser-reconciliation.json', {'operation_context': self.context.to_dict(),
            'state':'waiting-reuse-only', 'native_windows':{'Default':{'window-1':100, 'window-2':101}},
            'canonical_checkpoint_changed':False})
        cli._publish_workspace_attempt_completion()
        return result.evidence.placement.request_id

    def reply(self, action, payload, **kwargs):
        if action == 'restore_status':
            return {'exists':True, 'window_id':100, 'urls_restored':True, 'group_warnings':[]}
        if action == 'capture':
            return deepcopy(self.native[kwargs['profile']])
        self.fail(f'Unexpected native mutation: {action}')

    def patches(self):
        stack = ExitStack()
        stack.enter_context(patch.object(browser, 'connected_profiles', return_value=['Default']))
        request = stack.enter_context(patch.object(browser, 'request_browser', side_effect=self.reply))
        return stack, request

    def test_later_verified_content_submits_exact_move_once_and_updates_receipts(self):
        token = self.seed(self.initial())
        stack, request = self.patches()
        with stack:
            result = progress.continue_pending_browser_placements(self.context)
            second = progress.continue_pending_browser_placements(self.context)
        self.assertEqual(result, {'continued':1, 'updated':True})
        self.assertEqual(second['continued'], 0)
        self.place.assert_called_once()
        self.assertEqual(self.place.call_args.kwargs['chrome_window_id'], 100)
        self.assertEqual(self.place.call_args.kwargs['placement']['workspace'], 2)
        self.assertEqual([call.args[0] for call in request.call_args_list], ['restore_status', 'capture'])
        stage = next(stage for stage in self.document()['stages'] if stage['id']=='browsers')
        self.assertEqual(stage['state'], 'ready')
        self.assertTrue(read_stage_marker(self.item_marker).verified)
        self.assertTrue(read_stage_marker(cli._startup_marker('browsers')).verified)
        receipt = json.loads((cli._startup_directory() / 'browser-reconciliation.json').read_text())
        self.assertEqual(receipt['state'], 'verified-reuse-only')
        self.assertFalse(receipt['canonical_checkpoint_changed'])
        self.assertEqual(token, read_stage_marker(self.item_marker).provider_results[0]['placement']['request_id'])

    def test_read_only_observer_never_submits_missing_placement(self):
        self.seed(self.initial())
        with patch.object(browser, 'request_browser', side_effect=self.reply) as request, \
             patch.object(progress, '_query_request') as query:
            progress.reconcile_pending(self.context)
        query.assert_not_called()
        self.place.assert_not_called()
        self.assertEqual([call.args[0] for call in request.call_args_list], ['restore_status'])
        self.assertEqual(next(stage for stage in self.document()['stages'] if stage['id']=='browsers')['state'], 'waiting')

    def test_slow_content_stays_waiting_without_native_identification_or_move(self):
        self.seed(self.initial())
        stack, request = self.patches()
        with stack:
            request.side_effect = lambda *args, **kwargs: {
                'exists':True, 'window_id':100, 'urls_restored':False, 'urls_pending':True}
            outcome = progress.continue_pending_browser_placements(self.context)
        self.assertTrue(outcome['updated'])
        self.place.assert_not_called()
        self.assertEqual(request.call_count, 1)

    def test_full_catalog_change_refuses_move_and_keeps_specific_failure(self):
        for change in ('url', 'group', 'extra', 'identity'):
            with self.subTest(change=change):
                # Fresh scope per subtest keeps a failed receipt from another attempt irrelevant.
                case = PendingPlacementTests('test_pending_exact_content_defers_unsubmitted_placement_instead_of_failing')
                case.setUp()
                try:
                    case.seed(case.initial())
                    if change=='url':
                        case.native['Default']['windows'][1]['tabs'][0]['url']='https://example.test/user-edit'
                    elif change=='group':
                        case.native['Default']['windows'][0]['groups'][0]['title']='Changed'
                    elif change=='extra':
                        case.native['Default']['windows'].append(deepcopy(case.native['Default']['windows'][1]))
                    else:
                        case.native['Default']['windows'][0]['runtime_window_id']=999
                    stack, _ = case.patches()
                    with stack:
                        progress.continue_pending_browser_placements(case.context)
                    case.place.assert_not_called()
                    stage=next(stage for stage in case.document()['stages'] if stage['id']=='browsers')
                    self.assertEqual(stage['state'], 'failed')
                    self.assertIn('placement was not submitted', stage['provider_results'][0]['placement']['detail'])
                finally:
                    case.doCleanups()
                    operations.bind(self.context)

    def test_cancel_new_owner_expired_or_suspended_never_moves(self):
        token = self.seed(self.initial())
        before = self.document()
        for condition in ('cancelled', 'new-owner', 'expired', 'suspended'):
            with self.subTest(condition=condition), ExitStack() as stack:
                document=deepcopy(before)
                if condition=='cancelled':
                    document['operation_state']='cancelled'
                elif condition=='new-owner':
                    document['operation_id']='new-owner'
                elif condition=='expired':
                    stack.enter_context(patch.object(operations.OperationContext, 'check', side_effect=TimeoutError('expired')))
                else:
                    stack.enter_context(patch('workspace_state.startup.startup_suspended', return_value=True))
                atomic_json(login_status.status_path(), document)
                request = stack.enter_context(patch.object(browser, 'request_browser'))
                outcome=progress.continue_pending_browser_placements(self.context)
                self.assertEqual(outcome['continued'], 0)
                request.assert_not_called()
                self.place.assert_not_called()
        self.assertTrue(browser._continuation_path(token).exists())

    def test_owner_change_during_native_read_never_moves_or_publishes(self):
        self.seed(self.initial())
        replacement = {}
        stack, request = self.patches()
        def reply(*args, **kwargs):
            login_status.initialize_shutdown('abc123', 'new-operation')
            replacement.update(self.document())
            return self.reply(*args, **kwargs)
        with stack:
            request.side_effect=reply
            outcome=progress.continue_pending_browser_placements(self.context)
        self.assertEqual(outcome['continued'], 0)
        self.assertEqual(self.document(), replacement)
        self.place.assert_not_called()

    def test_corrupt_payload_never_authorizes_move(self):
        token=self.seed(self.initial())
        path=browser._continuation_path(token)
        record=json.loads(path.read_text())
        record['payload']['window']['workspace_index']=5
        atomic_json(path, record)
        stack, request=self.patches()
        with stack:
            progress.continue_pending_browser_placements(self.context)
        self.place.assert_not_called()
        request.assert_not_called()

    def test_lost_status_publication_does_not_repeat_native_move(self):
        self.seed(self.initial())
        stack, _=self.patches()
        with stack:
            with patch.object(login_status, '_locked_update', return_value=False):
                first=progress.continue_pending_browser_placements(self.context)
            second=progress.continue_pending_browser_placements(self.context)
        self.assertFalse(first['updated'])
        self.assertTrue(second['updated'])
        self.place.assert_called_once()

    def test_accepted_native_handoff_becomes_observable_without_repeating_move(self):
        original_token=self.seed(self.initial())
        self.place.side_effect=browser.BrowserPlacementPending('Native receipt accepted', 'native-request-1')
        stack, _=self.patches()
        with stack:
            progress.continue_pending_browser_placements(self.context)
            progress.continue_pending_browser_placements(self.context)
            with patch.object(progress, '_query_request', return_value={
                'token':'native-request-1', 'status':'verified'}):
                progress.reconcile_pending(self.context)
        self.place.assert_called_once()
        marker=read_stage_marker(self.item_marker)
        self.assertTrue(marker.verified)
        self.assertEqual(marker.provider_results[0]['placement']['request_id'], 'native-request-1')
        self.assertNotEqual(original_token, 'native-request-1')

    def test_group_warning_during_content_observation_is_a_specific_failure(self):
        self.seed(self.initial())
        with patch.object(browser, 'request_browser', return_value={
            'exists':True, 'window_id':100, 'urls_restored':True,
            'group_warnings':['Original group membership changed'],
        }):
            progress.reconcile_pending(self.context)
        stage=next(stage for stage in self.document()['stages'] if stage['id']=='browsers')
        self.assertEqual(stage['provider_results'][0]['content']['state'], 'failed')
        self.assertIn('Original group membership changed', stage['message'])
        self.place.assert_not_called()

    def test_finalizer_waits_for_pending_handoff_without_replay(self):
        self.seed(self.initial())
        stack, _=self.patches()
        with stack:
            login_finalize._wait_pending_providers()
        self.place.assert_called_once()
        self.assertEqual(self.document()['operation_state'], 'running')

    def strict_initial(self, **reply):
        self.place.side_effect = _place_browser_window
        strict = deepcopy(self.one)
        del strict['profiles'][0]['windows'][0]['_reconcile_window_id']
        with patch.object(browser, 'request_browser', return_value={
            'window_id':100, 'created':False, 'reused':True,
            'urls_restored':True, 'group_warnings':[], **reply,
        }):
            return browser.restore_browser(strict, restore_token_prefix='strict',
                restore_catalog=self.catalog, authority_guard=self.context.check)[0]

    def strict_timeout(self):
        with patch.object(browser, '_identify_native_window',
                          side_effect=browser.BrowserTransportUnavailable('native host deadline')):
            return self.strict_initial()

    def native_placement(self):
        stack = ExitStack()
        identify = stack.enter_context(patch.object(browser, '_identify_native_window',
            return_value=(9, {'window_id':100, 'token':'owned-token'})))
        stack.enter_context(patch.object(browser, '_release_window_identification'))
        stack.enter_context(patch.object(browser, 'capture_shell',
            return_value={'capabilities':['staged_placement_handoff']}))
        move = stack.enter_context(patch.object(browser, 'move_window_result',
            return_value={'status':'verified'}))
        return stack, identify, move

    def test_strict_startup_identify_and_release_timeouts_retain_exact_claim(self):
        for failure in ('identify', 'release'):
            with self.subTest(failure=failure):
                stack, identify, move = self.native_placement()
                with stack:
                    if failure == 'identify':
                        identify.side_effect = browser.BrowserTransportUnavailable('deadline')
                    else:
                        stack.enter_context(patch.object(browser, '_release_window_identification',
                            side_effect=browser.BrowserTransportUnavailable('deadline')))
                    result = self.strict_initial()
                self.assertEqual(result.evidence.content.state, S.VERIFIED)
                self.assertEqual(result.evidence.placement.state, S.WAITING)
                token = result.evidence.placement.request_id
                self.assertTrue(token.startswith(browser.CONTENT_PLACEMENT_PREFIX))
                payload = json.loads(browser._continuation_path(token).read_text())['payload']
                self.assertEqual(payload['window_id'], 100)
                self.assertEqual(payload['window']['workspace_index'], 2)
                self.assertNotIn('_reconcile_window_id', payload['window'])
                move.assert_not_called()

    def test_strict_timeout_later_places_saved_destination_once_without_restore_replay(self):
        token = self.seed(self.strict_timeout())
        stack, request = self.patches()
        native, identify, move = self.native_placement()
        with stack, native:
            progress.continue_pending_browser_placements(self.context)
            progress.continue_pending_browser_placements(self.context)
        identify.assert_called_once()
        move.assert_called_once()
        self.assertEqual(move.call_args.args[0], 9)
        self.assertEqual(move.call_args.args[1]['workspace'], 2)
        self.assertEqual(move.call_args.args[1]['geometry'], self.one['profiles'][0]['windows'][0]['geometry'])
        self.assertEqual([call.args[0] for call in request.call_args_list],
                         ['restore_status', 'capture', 'restore_status', 'capture'])
        self.assertEqual(json.loads(browser._continuation_path(token).read_text())['result']['status'], 'verified')

    def test_repeated_strict_pre_submission_timeout_stays_pending_without_terminal_receipt(self):
        token = self.seed(self.strict_timeout())
        stack, _ = self.patches()
        native, identify, move = self.native_placement()
        identify.side_effect = browser.BrowserTransportUnavailable('host still unavailable')
        with stack, native:
            for _ in range(2):
                progress.continue_pending_browser_placements(self.context)
        self.assertEqual(identify.call_count, 2)
        move.assert_not_called()
        self.assertNotIn('result', json.loads(browser._continuation_path(token).read_text()))
        stage = next(stage for stage in self.document()['stages'] if stage['id']=='browsers')
        self.assertEqual(stage['state'], 'waiting')
        self.assertEqual(stage['provider_results'][0]['placement']['request_id'], token)

    def test_strict_claim_changes_and_post_identification_content_changes_block_move(self):
        for change in ('missing', 'status-id', 'capture-id', 'url', 'group', 'group-warning', 'after-cleanup'):
            with self.subTest(change=change):
                case = PendingPlacementTests('test_pending_exact_content_defers_unsubmitted_placement_instead_of_failing')
                case.setUp()
                try:
                    case.seed(case.strict_timeout())
                    captures = 0
                    def reply(action, payload, **kwargs):
                        nonlocal captures
                        if action == 'restore_status':
                            return {'exists':change != 'missing',
                                'window_id':999 if change == 'status-id' else 100,
                                'urls_restored':True,
                                'group_warnings':['Original group changed'] if change == 'group-warning' else []}
                        self.assertEqual(action, 'capture')
                        captures += 1
                        live = deepcopy(case.native[kwargs['profile']])
                        claimed = live['windows'][0]
                        if change == 'capture-id':
                            claimed['runtime_window_id'] = 999
                        elif change == 'url' or (change == 'after-cleanup' and captures > 1):
                            claimed['tabs'][0]['url'] = 'https://example.test/user-edit'
                        elif change == 'group':
                            claimed['groups'][0]['title'] = 'Changed'
                        return live
                    stack, _ = case.patches()
                    native, _, move = case.native_placement()
                    with stack, native, patch.object(browser, 'request_browser', side_effect=reply):
                        progress.continue_pending_browser_placements(case.context)
                    move.assert_not_called()
                    stage = next(stage for stage in case.document()['stages'] if stage['id']=='browsers')
                    self.assertEqual(stage['state'], 'failed')
                finally:
                    case.doCleanups()
                    operations.bind(self.context)

    def test_strict_post_submission_transport_error_and_rejection_are_never_retried(self):
        for error in (browser.BrowserTransportUnavailable('post-submit transport error'),
                      TimeoutError('post-submit deadline'), None):
            with self.subTest(error=error):
                case = PendingPlacementTests('test_pending_exact_content_defers_unsubmitted_placement_instead_of_failing')
                case.setUp()
                try:
                    token = case.seed(case.strict_timeout())
                    stack, request = case.patches()
                    native, _, move = case.native_placement()
                    move.side_effect = error
                    move.return_value = {'status':'rejected'}
                    with stack, native:
                        progress.continue_pending_browser_placements(case.context)
                        progress.continue_pending_browser_placements(case.context)
                        result = browser.continue_browser_placement(token, case.context)
                    self.assertEqual(result['status'], 'failed')
                    move.assert_called_once()
                    self.assertEqual([call.args[0] for call in request.call_args_list],
                        ['restore_status', 'capture', 'restore_status', 'capture'])
                finally:
                    case.doCleanups()
                    operations.bind(self.context)

    def test_generic_identification_failure_is_terminal_without_continuation(self):
        with patch.object(browser, '_identify_native_window',
                          side_effect=browser.BrowserUnavailable('invalid window identification')):
            result = self.strict_initial()
        self.assertEqual(result.evidence.placement.state, S.FAILED)
        self.assertIsNone(result.evidence.placement.request_id)

    def test_current_content_preservation_timeout_does_not_create_strict_continuation(self):
        with patch.object(browser, '_identify_native_window',
                          side_effect=browser.BrowserTransportUnavailable('deadline')):
            result = self.strict_initial(urls_restored=False, content_preserved=True,
                                         content_basis='current-native-session')
        self.assertEqual(result.evidence.placement.state, S.FAILED)
        self.assertIsNone(result.evidence.placement.request_id)

    def test_reconciled_profile_probe_transport_failure_propagates_without_inventory_failure(self):
        self.seed(self.initial())
        with patch.object(browser, 'request_browser', side_effect=self.reply), \
             patch.object(browser, '_host_paths', return_value=[Path('/synthetic/native.sock')]), \
             patch.object(browser, '_request_path', side_effect=browser.BrowserTransportUnavailable('probe deadline')):
            progress.continue_pending_browser_placements(self.context)
        stage = next(stage for stage in self.document()['stages'] if stage['id']=='browsers')
        self.assertEqual(stage['state'], 'waiting')
        self.assertNotIn('inventory changed', stage['provider_results'][0]['placement']['detail'])
        self.place.assert_not_called()

    def test_native_host_transport_failures_are_typed_but_rejections_are_not(self):
        for failure in ('io', 'deadline', 'closed', 'rejected', 'invalid-json'):
            with self.subTest(failure=failure):
                connection = Mock()
                connection.recv.return_value = {
                    'closed': b'', 'rejected': b'{"ok":false,"error":"claim rejected"}\n',
                    'invalid-json': b'invalid\n',
                }.get(failure, b'')
                if failure == 'io':
                    connection.connect.side_effect = OSError('connection failed')
                with patch.object(browser.socket, 'socket', return_value=connection):
                    with self.assertRaises(browser.BrowserUnavailable) as raised:
                        browser._request_path(Path('/synthetic/native.sock'), 'ping',
                                              timeout=0 if failure == 'deadline' else 2)
                self.assertEqual(isinstance(raised.exception, browser.BrowserTransportUnavailable),
                                 failure in {'io', 'deadline', 'closed'})
                connection.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
