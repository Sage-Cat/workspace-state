from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import fcntl
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import login_finalize, login_status, operations
from workspace_state import cli
from workspace_state import startup
from workspace_state.provider_progress import CATEGORY_STAGES
from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker
from workspace_state.util import atomic_json


class LoginFinalizeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.previous = operations.current()
        self.addCleanup(lambda: operations.bind(self.previous))
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory + '/runtime', 'XDG_STATE_HOME': directory + '/state',
                                                       'INVOCATION_ID': 'c' * 32, 'SERVICE_RESULT': 'timeout'}))
        operations.bind(None)
        login_status.initialize('a' * 16)
        self.context = operations.current()

    def test_drive_transport_exception_becomes_terminal_failure(self):
        with patch.object(login_finalize, '_start_drives', side_effect=subprocess.TimeoutExpired('findmnt', 20)), \
             patch.object(login_finalize, '_warm_cloud_metadata') as warmup:
            self.assertEqual(login_finalize.main(), 1)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(status['overall_state'], 'failed')
        self.assertIn('TimeoutExpired', status['overall_message'])
        warmup.assert_not_called()

    def test_transport_uses_remaining_deadline_and_inherits_operation_authority(self):
        context = operations.OperationContext.create('a' * 16, 'startup', budget=1)
        with operations.publisher(context), patch.object(login_finalize, '_check_operation'), \
             patch.object(login_finalize.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0)) as run:
            login_finalize._run('test-command', timeout=30)
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 1)
        inherited = json.loads(run.call_args.kwargs['env'][operations.CONTEXT_ENV])
        self.assertEqual(inherited, context.to_dict())

    def test_late_finalizer_cannot_adopt_or_fail_a_shutdown_operation(self):
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        before = login_status.status_path().read_text()
        operations.bind(self.context)
        with patch.object(login_finalize, '_finalize') as finalize:
            self.assertEqual(login_finalize.main(), 1)
        finalize.assert_not_called()
        self.assertEqual(login_status.status_path().read_text(), before)

    def test_systemd_timeout_uses_its_recorded_invocation_context(self):
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        operations.bind(None)
        self.assertEqual(login_finalize.service_result(), 0)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(status['overall_state'], 'failed')
        self.assertIn('timeout', status['overall_message'])

    def test_old_systemd_receipt_cannot_fail_a_new_operation(self):
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        before = login_status.status_path().read_text()
        operations.bind(None)
        self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_text(), before)

    def test_reported_provider_failure_settles_operation_and_survives_service_exit(self):
        for name, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.update_stage('browsers', 'failed', 'Exact tab URLs differ')
        with patch.object(login_finalize, '_start_drives', return_value={'gdrive': True}), \
             patch.object(login_finalize, '_warm_cloud_metadata', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_file_manager', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_vscode', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_codex', return_value=True):
            self.assertEqual(login_finalize.main(), 1)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(status['operation_state'], 'failed')
        self.assertIn('browsers', status['overall_message'])
        self.assertNotIn('stopped before completion', status['overall_message'])
        before = login_status.status_path().read_bytes()
        with patch.dict(os.environ, {'SERVICE_RESULT': 'exit-code'}):
            self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_bytes(), before)
        self.assertFalse(login_finalize._invocation_receipt().exists())

    def test_service_callback_preserves_terminal_stage_even_with_other_pending_work(self):
        login_status.update_stage('login-finalization', 'failed', 'Drive transport failed')
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        before = login_status.status_path().read_bytes()
        self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_completed_operation_cannot_be_failed_by_late_service_callback(self):
        for name, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.finish()
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        before = login_status.status_path().read_bytes()
        self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def ready_with_failed_finalizer(self):
        for name, _label in login_status.DEFAULT_STAGES:
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.update_stage('login-finalization', 'failed', 'Previous finalizer failed')
        for category in CATEGORY_STAGES:
            write_stage_marker(cli._startup_marker(category), StageMarker(
                category, 'ready', 'saved', operation_context=self.context.to_dict()))

    def test_scoped_retry_carries_verified_proof_and_rejects_old_publishers(self):
        self.ready_with_failed_finalizer()
        login_status.finish()
        self.assertEqual(json.loads(login_status.status_path().read_text())['operation_state'], 'failed')
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        context = login_finalize.retry_operation(self.context.operation_id)
        self.assertNotEqual(context.operation_id, self.context.operation_id)
        self.assertEqual(context.attempt, self.context.attempt + 1)
        self.assertEqual(context.deadline, self.context.deadline)
        for category in CATEGORY_STAGES:
            self.assertTrue(read_stage_marker(cli._startup_marker(category)).verified_for(context))
        with operations.publisher(self.context):
            self.assertFalse(login_status.update_stage('browsers', 'failed', 'Late old worker'))
        before = login_status.status_path().read_bytes()
        login_finalize.service_result()
        self.assertEqual(login_status.status_path().read_bytes(), before)
        with patch.object(login_finalize, '_start_drives', return_value={'gdrive': True}), \
             patch.object(login_finalize, '_warm_cloud_metadata', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_file_manager', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_vscode', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_codex', return_value=True):
            self.assertEqual(login_finalize.main(), 0)
        result = json.loads(login_status.status_path().read_text())
        self.assertEqual(result['overall_state'], 'ready')
        self.assertEqual(result['operation_state'], 'completed')

    def test_retry_refuses_provider_failures_and_wrong_identity_without_mutation(self):
        self.ready_with_failed_finalizer()
        for operation_id, stage_state in (('another-operation', 'ready'),
                                           (self.context.operation_id, 'failed'),
                                           (self.context.operation_id, 'waiting')):
            login_status.update_stage('social-apps', stage_state, 'Current provider evidence')
            before = login_status.status_path().read_bytes()
            with patch.object(login_finalize, '_finalize') as finalize:
                self.assertEqual(login_finalize.main(operation_id), 1)
            finalize.assert_not_called()
            self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_retry_requires_current_verified_marker_proof(self):
        self.ready_with_failed_finalizer()
        write_stage_marker(cli._startup_marker('browsers'), StageMarker('browsers', 'ready'))
        before = login_status.status_path().read_bytes()
        self.assertEqual(login_finalize.main(self.context.operation_id), 1)
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_retry_cannot_extend_expired_operation_deadline(self):
        self.ready_with_failed_finalizer()
        context = replace(self.context, deadline=time.monotonic() - 1)
        document = json.loads(login_status.status_path().read_text())
        document['operation_context'] = context.to_dict()
        atomic_json(login_status.status_path(), document)
        atomic_json(login_status.operation_path(), {'schema_version': 1, 'operation_context': context.to_dict()})
        operations.bind(context)
        before = login_status.status_path().read_bytes()
        with patch.object(login_finalize, '_finalize') as finalize:
            self.assertEqual(login_finalize.main(context.operation_id), 1)
        finalize.assert_not_called()
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_retry_migrates_markers_under_ownership_lock_before_publishing_new_uuid(self):
        self.ready_with_failed_finalizer()
        observed = []
        original_write = startup.write_stage_marker

        def write_marker(path, marker):
            with (login_status.runtime_root() / 'login-hud-status.lock').open('a+') as lock:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(json.loads(login_status.status_path().read_text())['operation_id'], self.context.operation_id)
            self.assertEqual(json.loads(login_status.operation_path().read_text())['operation_context'], self.context.to_dict())
            observed.append(path)
            original_write(path, marker)

        with patch.object(startup, 'write_stage_marker', side_effect=write_marker):
            context = login_finalize.retry_operation(self.context.operation_id)
        self.assertEqual(len(observed), len(CATEGORY_STAGES))
        self.assertEqual(json.loads(login_status.status_path().read_text())['operation_id'], context.operation_id)

    def test_failed_marker_migration_restores_previous_proof_and_authority(self):
        self.ready_with_failed_finalizer()
        before = login_status.status_path().read_bytes()
        owner = login_status.operation_path().read_bytes()
        markers = {cli._startup_marker(name): cli._startup_marker(name).read_bytes() for name in CATEGORY_STAGES}
        original_write = startup.write_stage_marker
        writes = 0

        def write_marker(path, marker):
            nonlocal writes
            writes += 1
            original_write(path, marker)
            if writes == 2:
                raise OSError('Interrupted after atomic marker replacement')

        with patch.object(startup, 'write_stage_marker', side_effect=write_marker):
            with self.assertRaisesRegex(RuntimeError, 'Could not acquire'):
                login_finalize.retry_operation(self.context.operation_id)
        self.assertEqual(login_status.status_path().read_bytes(), before)
        self.assertEqual(login_status.operation_path().read_bytes(), owner)
        self.assertEqual({path: path.read_bytes() for path in markers}, markers)
        self.assertEqual(operations.current(), self.context)

    def test_retry_rechecks_shutdown_suspension_after_acquiring_status_lock(self):
        self.ready_with_failed_finalizer()
        before = login_status.status_path().read_bytes()
        original_update = login_status._locked_update

        def suspended_update(*args, **kwargs):
            atomic_json(login_status.runtime_root() / 'startup-suspended.json', {
                'schema_version': 1, 'boot_id': self.context.boot_id,
                'login_generation': self.context.login_generation,
            })
            return original_update(*args, **kwargs)

        with patch.object(login_status, '_locked_update', side_effect=suspended_update):
            with self.assertRaisesRegex(RuntimeError, 'suspended'):
                login_finalize.retry_operation(self.context.operation_id)
        self.assertEqual(login_status.status_path().read_bytes(), before)
        for category in CATEGORY_STAGES:
            self.assertTrue(read_stage_marker(cli._startup_marker(category)).verified_for(self.context))

    def test_failed_status_publication_rolls_back_before_releasing_ownership_lock(self):
        self.ready_with_failed_finalizer()
        for replaced in (False, True):
            with self.subTest(replaced=replaced):
                before = login_status.status_path().read_bytes()
                owner = login_status.operation_path().read_bytes()
                markers = {cli._startup_marker(name): cli._startup_marker(name).read_bytes() for name in CATEGORY_STAGES}
                original_write = login_status.atomic_json
                rollback_writes = []

                def fail_status(path, value):
                    if path != login_status.status_path():
                        return original_write(path, value)
                    if replaced:
                        original_write(path, value)
                    raise OSError('Injected status publication failure')

                def restore_status(path, value):
                    with (login_status.runtime_root() / 'login-hud-status.lock').open('a+') as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    rollback_writes.append(path)
                    original_write(path, value)

                with patch.object(login_status, 'atomic_json', side_effect=fail_status), \
                     patch.object(login_finalize, 'atomic_json', side_effect=restore_status):
                    with self.assertRaisesRegex(RuntimeError, 'Could not acquire'):
                        login_finalize.retry_operation(self.context.operation_id)
                self.assertEqual(rollback_writes, [login_status.status_path()])
                self.assertEqual(login_status.status_path().read_bytes(), before)
                self.assertEqual(login_status.operation_path().read_bytes(), owner)
                self.assertEqual({path: path.read_bytes() for path in markers}, markers)
                self.assertEqual(operations.current(), self.context)
        context = login_finalize.retry_operation(self.context.operation_id)
        self.assertNotEqual(context.operation_id, self.context.operation_id)
        self.assertEqual(context.deadline, self.context.deadline)

    def test_plain_finalizer_cannot_reopen_terminal_operation(self):
        self.ready_with_failed_finalizer()
        login_status.finish()
        before = login_status.status_path().read_bytes()
        with patch.object(login_finalize, '_finalize') as finalize:
            self.assertEqual(login_finalize.main(), 1)
        finalize.assert_not_called()
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_running_retry_clears_only_finalizer_failure(self):
        self.ready_with_failed_finalizer()
        login_status.update_stage('social-apps', 'failed', 'Unresolved provider')
        with patch.object(login_finalize, '_start_drives', return_value={'gdrive': True}), \
             patch.object(login_finalize, '_warm_cloud_metadata', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_file_manager', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_vscode', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_codex', return_value=True):
            self.assertEqual(login_finalize.main(), 1)
        result = json.loads(login_status.status_path().read_text())
        finalizer = next(stage for stage in result['stages'] if stage['id'] == 'login-finalization')
        self.assertIn('social-apps', finalizer['message'])
        self.assertNotIn('login-finalization', finalizer['message'])


if __name__ == '__main__':
    unittest.main()
