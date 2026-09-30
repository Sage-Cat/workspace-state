from argparse import Namespace
from contextlib import ExitStack
from dataclasses import replace
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import cli, login_status, operations, provider_progress as progress
from workspace_state.provider_results import EvidenceState as S, PhaseEvidence as E, ProviderItemResult, ProviderRestoreError
from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker
from workspace_state.util import atomic_json


class ProviderProgressTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {'XDG_RUNTIME_DIR': str(self.root),
                                                        'XDG_STATE_HOME': str(self.root / 'state')}))
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        login_status.runtime_root().mkdir()
        (login_status.runtime_root() / 'login-generation').write_text('abc123')
        self.assertTrue(login_status.initialize('abc123'))
        self.context = operations.OperationContext.from_dict(self.document()['operation_context'])
        for category, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(category, 'ready', 'Verified')
        self.stack.enter_context(patch.object(cli, '_publish_workspace_restored'))
        self.stack.enter_context(patch.object(cli, 'load', return_value={'sessions': [], 'created_at': 'saved'}))
        self.launch = self.stack.enter_context(patch.object(cli, '_restore'))

    def document(self):
        return json.loads(login_status.status_path().read_text())

    def stage(self, category='browsers'):
        return next(item for item in self.document()['stages'] if item['id'] == category)

    def item(self, token='request-1', state=S.WAITING):
        return ProviderItemResult('chrome', token, E(S.VERIFIED), E(S.VERIFIED),
                                  E(state, 'Awaiting exact request', True, token))

    def seed(self, items=None):
        items = items or [self.item()]
        if self.document().get('operation_state') in {'failed', 'completed'}:
            self.context = operations.OperationContext.create('abc123', 'startup', attempt=self.context.attempt + 1)
            document = self.document()
            document.update(operation_context=self.context.to_dict(), operation_id=self.context.operation_id,
                            operation_state='running')
            atomic_json(login_status.status_path(), document)
            atomic_json(login_status.operation_path(), {'schema_version':1, 'operation_context':self.context.to_dict()})
            operations.bind(self.context)
        login_status.update_stage('browsers', 'waiting', 'Awaiting exact requests')
        self.assertTrue(login_status.publish_provider_results('browsers', items))
        write_stage_marker(cli._startup_marker('browsers'), StageMarker(
            'browsers', 'waiting', 'saved', operation_context=self.context.to_dict(),
            provider_results=tuple(item.to_dict() for item in items)))
        cli._publish_workspace_attempt_completion()

    def expire(self):
        self.context = replace(self.context, deadline=time.monotonic() - 1)
        value = self.document()
        value['operation_context'] = self.context.to_dict()
        atomic_json(login_status.status_path(), value)
        atomic_json(login_status.operation_path(), {'schema_version': 1, 'operation_context': self.context.to_dict()})
        operations.bind(self.context)

    def test_accepted_deferred_and_legacy_placed_never_become_ready(self):
        for response in ({'status': 'accepted'}, {'status': 'deferred'},
                         {'status': 'placed', 'placed': True},
                         {'status': 'verified', 'deferred': True}):
            with self.subTest(response=response):
                self.seed()
                with patch.object(progress, '_query_request', return_value={'token': 'request-1', **response}):
                    result = progress.reconcile_pending()
                self.assertTrue(result['pending'])
                self.assertEqual(self.stage()['state'], 'waiting')
                self.assertEqual(self.stage('workspace')['state'], 'waiting')
                self.assertNotEqual(self.document()['overall_state'], 'ready')
                self.assertFalse(read_stage_marker(cli._startup_marker('browsers')).verified)
        self.launch.assert_not_called()

    def test_failed_hud_row_names_unrecovered_group_and_verified_count(self):
        good = self.item('Default/window-1', S.VERIFIED)
        bad = replace(self.item('Default/window-2', S.VERIFIED),
                      attention=('Original tab group is unavailable; saved intent is preserved',))
        self.seed([good, bad])
        stage = self.stage()
        self.assertEqual(stage['state'], 'failed')
        self.assertIn('1/2 verified', stage['message'])
        self.assertIn('Default/window-2: Original tab group is unavailable', stage['message'])

    def test_failure_summary_retains_transport_fallback_and_bounds_text(self):
        self.assertEqual(progress.failure_summary([], 'Companion\n unavailable'), 'Companion unavailable')
        item = self.item(state=S.FAILED).to_dict()
        item['placement']['detail'] = 'Placement stalled ' * 50
        self.assertLessEqual(len(progress.failure_summary([item], 'Fallback')), 240)
        self.assertIn('Placement stalled', progress.failure_summary([item], 'Fallback'))

    def test_verified_updates_marker_workspace_and_operation_without_relaunch(self):
        self.seed()
        with patch.object(progress, '_query_request', return_value={'token': 'request-1', 'status': 'verified'}):
            result = progress.reconcile_pending()
        self.assertFalse(result['pending'])
        self.assertEqual(self.stage()['state'], 'ready')
        self.assertEqual(self.stage('workspace')['state'], 'ready')
        self.assertEqual(self.document()['overall_state'], 'ready')
        self.assertEqual(self.document()['operation_state'], 'completed')
        self.assertTrue(read_stage_marker(cli._startup_marker('browsers')).verified)
        self.launch.assert_not_called()

    def test_loading_chrome_content_finishes_by_read_only_observation(self):
        token = '["Default","startup:Default:saved",7]'
        item = ProviderItemResult('chrome', 'Default/saved', E(S.VERIFIED),
                                  E(S.WAITING, 'Still loading', True, token), E(S.VERIFIED), reused=True)
        self.seed([item])
        item_marker = cli._startup_directory() / 'browser-items' / 'saved.done'
        write_stage_marker(item_marker, StageMarker('browsers', 'waiting', 'saved',
                           operation_context=self.context.to_dict(), provider_results=(item.to_dict(),)))
        for response, expected in (({'urls_restored': False, 'urls_pending': True}, 'waiting'),
                                   ({'urls_restored': True}, 'ready')):
            with patch('workspace_state.browser.request_browser', return_value={
                'exists': True, 'window_id': 7, **response,
            }) as request, patch.object(progress, '_query_request') as placement:
                outcome = progress.reconcile_pending()
            self.assertEqual(outcome['queried'], 1)
            self.assertEqual(self.stage()['state'], expected)
            self.assertEqual(request.call_args.args, ('restore_status', {'restore_token': 'startup:Default:saved'}))
            self.assertEqual(request.call_args.kwargs['profile'], 'Default')
            self.assertLessEqual(request.call_args.kwargs['timeout'], 1)
            placement.assert_not_called()
        self.assertEqual(self.document()['operation_state'], 'completed')
        self.assertTrue(read_stage_marker(cli._startup_marker('browsers')).verified)
        self.assertTrue(read_stage_marker(item_marker).verified)
        self.launch.assert_not_called()

    def test_observer_preserves_item_marker_with_another_request_identity(self):
        self.seed()
        marker = cli._startup_directory() / 'browser-items' / 'saved.done'
        previous = StageMarker('browsers', 'waiting', 'saved', operation_context=self.context.to_dict(),
                               provider_results=(replace(self.item(), placement=E(S.WAITING, request_id='other')).to_dict(),))
        write_stage_marker(marker, previous)
        with patch.object(progress, '_query_request', return_value={'token': 'request-1', 'status': 'verified'}):
            progress.reconcile_pending()
        self.assertEqual(read_stage_marker(marker), previous)

    def test_loading_chrome_redirect_or_replacement_fails_without_navigation(self):
        token = '["Default","startup:Default:saved",7]'
        item = ProviderItemResult('chrome', 'Default/saved', E(S.VERIFIED),
                                  E(S.WAITING, 'Still loading', True, token), E(S.VERIFIED))
        for response in ({'exists': True, 'window_id': 7, 'urls_restored': False,
                          'url_errors': ['Saved URL redirected']},
                         {'exists': True, 'window_id': 8, 'urls_restored': True}, {'exists': False}):
            with self.subTest(response=response):
                self.seed([item])
                with patch('workspace_state.browser.request_browser', return_value=response) as request:
                    outcome = progress.reconcile_pending()
                self.assertFalse(outcome['pending'])
                self.assertEqual(self.stage()['state'], 'failed')
                self.assertEqual(request.call_args.args[0], 'restore_status')
        self.launch.assert_not_called()

    def test_content_observation_deadline_expires_without_query(self):
        self.seed([ProviderItemResult('chrome', 'Default/saved', E(S.VERIFIED),
                  E(S.WAITING, 'Still loading', True, '["Default","saved",7]'), E(S.VERIFIED))])
        self.expire()
        with patch('workspace_state.browser.request_browser') as request:
            outcome = progress.reconcile_pending()
        request.assert_not_called()
        self.assertFalse(outcome['pending'])
        self.assertEqual(self.stage()['provider_results'][0]['content']['state'], 'failed')

    def test_stale_response_cannot_modify_new_operation_or_marker(self):
        self.seed()
        original_marker = cli._startup_marker('browsers').read_bytes()
        replacement = {}
        def reply(_token, _timeout):
            login_status.initialize_shutdown('abc123', 'new-operation')
            replacement.update(self.document())
            return {'token': 'request-1', 'status': 'verified'}
        with patch.object(progress, '_query_request', side_effect=reply):
            result = progress.reconcile_pending(self.context)
        self.assertFalse(result['updated'])
        self.assertEqual(self.document(), replacement)
        self.assertEqual(cli._startup_marker('browsers').read_bytes(), original_marker)

    def test_request_token_mismatch_is_not_verification(self):
        self.seed()
        with patch.object(progress, '_query_request', return_value={'token': 'another-request', 'status': 'verified'}):
            progress.reconcile_pending()
        self.assertEqual(self.stage()['state'], 'waiting')

    def test_failed_expired_cancelled_preserve_attempt_and_fail_truthfully(self):
        for terminal in ('failed', 'expired', 'cancelled'):
            with self.subTest(terminal=terminal):
                self.seed()
                with patch.object(progress, '_query_request', return_value={'token':'request-1', 'status':terminal}):
                    progress.reconcile_pending()
                self.assertEqual(self.stage()['state'], 'failed')
                marker = read_stage_marker(cli._startup_marker('browsers'))
                self.assertEqual(marker.state, 'failed')
                self.assertEqual(marker.snapshot, 'saved')
                self.assertFalse(marker.verified)
        self.launch.assert_not_called()

    def test_query_count_is_bounded_and_round_robin_does_not_starve(self):
        self.seed([self.item(f'request-{index}') for index in range(9)])
        queried = []
        def reply(token, timeout):
            self.assertGreater(timeout, 0)
            self.assertLessEqual(timeout, 1)
            queried.append(token)
            return {'token':token, 'status':'accepted'}
        with patch.object(progress, '_query_request', side_effect=reply):
            for _ in range(3):
                self.assertEqual(progress.reconcile_pending(max_requests=500)['queried'], 4)
        self.assertEqual(set(queried), {f'request-{index}' for index in range(9)})

    def test_operation_deadline_expires_all_items_not_just_query_cap(self):
        self.seed([self.item(f'request-{index}') for index in range(9)])
        self.expire()
        with patch.object(progress, '_query_request') as query:
            result = progress.reconcile_pending(self.context)
        query.assert_not_called()
        self.assertFalse(result['pending'])
        self.assertTrue(all(item['placement']['state'] == 'failed' for item in self.stage()['provider_results']))
        self.assertEqual(self.document()['operation_state'], 'failed')
        self.assertEqual(self.document()['overall_state'], 'failed')

    def test_deadline_watchdog_fails_untracked_startup_without_any_requests(self):
        login_status.update_stage('gdrive', 'waiting', 'Mount pending')
        login_status.update_stage('workspace', 'running', 'Worker pending')
        self.expire()
        with patch.object(progress, '_query_request') as query:
            result = progress.reconcile_pending(self.context)
        query.assert_not_called()
        self.assertTrue(result['updated'])
        self.assertEqual(self.stage('gdrive')['state'], 'failed')
        self.assertEqual(self.stage('workspace')['state'], 'failed')
        self.assertEqual(self.document()['operation_state'], 'failed')
        self.assertIn('deadline expired', self.stage('gdrive')['message'])

    def test_finish_keeps_waiting_message_and_does_not_complete_operation(self):
        self.seed()
        self.assertTrue(login_status.finish())
        self.assertEqual(self.document()['overall_state'], 'running')
        self.assertEqual(self.document()['operation_state'], 'running')
        self.assertIn('Waiting', self.document()['overall_message'])

    def test_waiting_error_publishes_evidence_without_failure_or_relaunch(self):
        error = ProviderRestoreError('Placement accepted', [self.item()])
        self.assertEqual(cli._publish_category_outcome('browsers', {'created_at':'saved'}, error=error), 'waiting')
        marker = read_stage_marker(cli._startup_marker('browsers'))
        self.assertEqual(marker.state, 'waiting')
        self.assertEqual(marker.operation_context, self.context.to_dict())
        self.assertEqual(cli.cmd_startup(Namespace(category='browsers', dry_run=False, force=False,
                                                   owns_tmux_restore=True)), 0)
        self.assertEqual(self.stage()['state'], 'waiting')
        self.launch.assert_not_called()

    def test_autosave_requires_verified_markers_not_existing_attempts(self):
        for category in cli.CATEGORIES:
            write_stage_marker(cli._startup_marker(category), StageMarker(category, 'ready', operation_context=self.context.to_dict()))
        self.seed()
        with patch.object(cli, '_arm_autosave') as arm:
            cli._arm_autosave_if_startup_complete()
            arm.assert_not_called()
        with patch.object(progress, '_query_request', return_value={'token':'request-1', 'status':'verified'}):
            progress.reconcile_pending()
        self.assertTrue(cli._autosave_marker().exists())

    def test_waiting_marker_preserves_saved_recipe(self):
        self.seed()
        previous = {'browsers': {'google_chrome': {'profiles': [{'profile':'Default', 'windows':[{'id':'saved', 'tabs':[]}]}]}}}
        captured = {}
        retained = cli._retain_unrestored_recipes(captured, previous)
        self.assertIn('browsers', retained)
        self.assertEqual(captured['browsers'], previous['browsers'])

    def test_json_and_legacy_markers_preserve_attempt_without_inventing_proof(self):
        path = self.root / 'marker'
        path.write_text('saved-snapshot\n')
        legacy = read_stage_marker(path, 'browsers')
        self.assertTrue(legacy.legacy)
        self.assertFalse(legacy.verified)
        path.write_text('failed: broken\n')
        self.assertEqual(read_stage_marker(path, 'browsers').state, 'failed')
        marker = StageMarker('browsers', 'waiting', 'saved', 'Awaiting', self.context.to_dict(), (self.item().to_dict(),))
        write_stage_marker(path, marker)
        self.assertEqual(read_stage_marker(path, 'browsers'), marker)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        path.write_text('{"schema_version":900}')
        self.assertEqual(read_stage_marker(path, 'browsers').state, 'attempted')

    def test_contended_status_lock_does_not_exceed_poll_budget(self):
        self.seed()
        lock = (login_status.runtime_root() / 'login-hud-status.lock').open('a+')
        self.addCleanup(lock.close)
        fcntl.flock(lock, fcntl.LOCK_EX)
        with patch.object(progress, '_query_request', return_value={'token':'request-1','status':'verified'}):
            started = time.monotonic()
            result = progress.reconcile_pending(budget_seconds=.04)
        self.assertFalse(result['updated'])
        self.assertLess(time.monotonic() - started, .3)

    def test_browser_collects_pending_evidence_and_never_cleans_up_waiting_windows(self):
        from workspace_state.browser import BROWSER_REQUIRED_CAPABILITIES, BrowserRestoreResult
        snapshot = {'browsers': {'google_chrome': {'profiles': [{'profile':'Default', 'windows':[
            {'id':'one', 'tabs':[]}, {'id':'two', 'tabs':[]}]}]}}}
        with ExitStack() as stack:
            for name, value in (('connected_profiles', ['Default']), ('ensure_browser_profiles', []),
                                ('wait_for_browser_settle', None), ('request_browser', {'exists':False}),
                                ('browser_companion_info', {'protocol_version':2, 'capabilities':list(BROWSER_REQUIRED_CAPABILITIES)})):
                stack.enter_context(patch.object(cli, name, return_value=value))
            restore = stack.enter_context(patch.object(cli, 'restore_browser', side_effect=[
                [BrowserRestoreResult('Accepted one', False, self.item('one'))],
                [BrowserRestoreResult('Deferred two', False, self.item('two'))]]))
            cleanup = stack.enter_context(patch.object(cli, '_close_startup_browser_duplicates'))
            with self.assertRaises(ProviderRestoreError) as caught:
                cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                                                          no_place=False, login_status=True), start_browser=True)
        self.assertEqual([item.placement.request_id for item in caught.exception.results], ['one', 'two'])
        self.assertEqual(restore.call_count, 2)
        cleanup.assert_not_called()
        markers = list((cli._startup_directory() / 'browser-items').glob('*.done'))
        self.assertEqual(len(markers), 2)
        self.assertTrue(all(read_stage_marker(path).state == 'waiting' for path in markers))

    def test_startup_waiting_job_is_successful_attempt_not_failed_or_ready(self):
        self.launch.side_effect = ProviderRestoreError('Accepted request', [self.item()])
        with patch.object(cli, '_wait_for_shell'):
            result = cli.cmd_startup(Namespace(category='browsers', dry_run=False, force=False,
                                               owns_tmux_restore=True, wait=0, no_place=False,
                                               workspace=None, session=None, select=False))
        self.assertEqual(result, 0)
        self.assertEqual(self.stage()['state'], 'waiting')
        self.assertEqual(self.stage('workspace')['state'], 'waiting')
        self.assertEqual(read_stage_marker(cli._startup_marker('browsers')).state, 'waiting')
        self.assertEqual(self.document()['overall_state'], 'running')

    def test_deferred_waiting_is_nonfailure_and_does_not_relaunch(self):
        for category, finish in (('file-manager', cli.finish_deferred_file_manager), ('vscode', cli.finish_deferred_vscode)):
            with self.subTest(category=category):
                self.launch.reset_mock()
                self.launch.side_effect = ProviderRestoreError('Accepted request', [self.item(category)])
                (cli._startup_directory() / f'{category}.deferred').touch()
                self.assertTrue(finish())
                self.assertTrue(finish())
                self.assertEqual(self.stage(category)['state'], 'waiting')
                self.assertEqual(read_stage_marker(cli._startup_marker(category)).state, 'waiting')
                self.launch.assert_called_once()
                self.assertFalse(cli._autosave_marker().exists())

    def test_nonzero_count_with_pending_evidence_does_not_authorize_autosave(self):
        from workspace_state.provider_results import ProviderCount
        cli._autosave_marker().write_text('ready\n')
        state = cli._publish_category_outcome('browsers', {}, count=ProviderCount(1, [self.item()]))
        self.assertEqual(state, 'waiting')
        self.assertFalse(cli._autosave_marker().exists())

    def test_stage_failure_outside_item_evidence_is_not_erased(self):
        verified = self.item(state=S.VERIFIED)
        error = ProviderRestoreError('Another item could not be identified', [verified])
        self.assertEqual(cli._publish_category_outcome('browsers', {}, error=error), 'failed')
        self.assertEqual(self.stage()['state'], 'failed')
        login_status.finish()
        self.assertEqual(self.stage()['state'], 'failed')

    def test_expired_lock_contention_requests_retry_and_cli_returns_nonzero(self):
        self.seed()
        self.expire()
        with patch.object(login_status, '_locked_update', return_value=False), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(progress.cmd_placement_progress(Namespace()), 1)
            outcome = progress.reconcile_pending(self.context)
        self.assertTrue(outcome['expired'])
        self.assertTrue(outcome['needs_retry'])
        self.assertEqual(self.stage()['state'], 'waiting')

    def test_unreadable_progress_state_is_not_reported_as_success(self):
        login_status.status_path().write_text('broken json')
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(progress.cmd_placement_progress(Namespace()), 1)

    def test_prior_attempt_markers_never_arm_current_autosave(self):
        other = replace(self.context, operation_id='prior-operation', attempt=99)
        for category in cli.CATEGORIES:
            write_stage_marker(cli._startup_marker(category), StageMarker(category, 'ready', operation_context=other.to_dict()))
        cli._arm_autosave_if_startup_complete()
        self.assertFalse(cli._autosave_marker().exists())
        self.seed()
        with patch.object(progress, '_query_request', return_value={'token':'request-1', 'status':'verified'}):
            progress.reconcile_pending()
        self.assertFalse(cli._autosave_marker().exists())

    def test_other_login_marker_does_not_authorize_dropping_saved_intent(self):
        old_login = replace(self.context, login_generation='another-login')
        write_stage_marker(cli._startup_marker('browsers'), StageMarker('browsers', 'ready', operation_context=old_login.to_dict()))
        previous = {'browsers': {'google_chrome': {'profiles': [{'profile':'Default', 'windows':[{'id':'saved','tabs':[]}]}]}}}
        self.assertIn('browsers', cli._retain_unrestored_recipes({}, previous))

    def test_late_response_after_terminal_operation_never_reopens_it(self):
        self.seed()
        document = self.document()
        document['operation_state'] = 'completed'
        atomic_json(login_status.status_path(), document)
        with patch.object(progress, '_query_request') as query:
            outcome = progress.reconcile_pending(self.context)
        query.assert_not_called()
        self.assertFalse(outcome['updated'])
        self.assertEqual(self.document(), document)

    def test_force_rejects_terminal_or_expired_operation_before_mutation(self):
        for state in ('completed', 'failed', 'running'):
            with self.subTest(state=state):
                document = self.document()
                document['operation_state'] = state
                atomic_json(login_status.status_path(), document)
                if state == 'running':
                    self.expire()
                before = login_status.status_path().read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'new login/operation'):
                    cli.cmd_startup(Namespace(category='browsers', force=True, dry_run=False))
                self.assertEqual(login_status.status_path().read_bytes(), before)
        self.launch.assert_not_called()

    def test_poll_timeout_kills_only_its_own_process_group(self):
        with patch.object(progress.subprocess, 'Popen') as start, patch.object(progress.os, 'killpg') as kill:
            process = start.return_value
            process.pid = 123456
            process.communicate.side_effect = [subprocess.TimeoutExpired('test', .01), ('', '')]
            self.assertEqual(progress._query_request('exact-token', .01)['status'], 'unknown')
        self.assertEqual(start.call_args.args[0], ['gnome-winctl', 'expectation', 'exact-token', '--json'])
        self.assertTrue(start.call_args.kwargs['start_new_session'])
        self.assertEqual(kill.call_args.args[0], 123456)


if __name__ == '__main__':
    unittest.main()
