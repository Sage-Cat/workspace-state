from __future__ import annotations

from contextlib import ExitStack
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import login_finalize, login_status, operations
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
             patch.object(login_finalize.subprocess, 'Popen') as launch:
            process = launch.return_value
            process.communicate.return_value = ('out', 'err')
            process.returncode = 0
            result = login_finalize._run('test-command', timeout=30)
        self.assertGreater(process.communicate.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(process.communicate.call_args.kwargs['timeout'], .25)
        inherited = json.loads(launch.call_args.kwargs['env'][operations.CONTEXT_ENV])
        self.assertEqual(inherited, context.to_dict())
        self.assertEqual(result.stdout, 'out')

    def test_late_finalizer_cannot_adopt_or_fail_another_login(self):
        login_status.initialize('c' * 16)
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
        login_status.initialize('c' * 16)
        before = login_status.status_path().read_text()
        operations.bind(None)
        self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_text(), before)

    def test_reported_provider_failure_settles_operation_and_survives_service_exit(self):
        for name, _label in login_status.default_stages():
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.update_stage('browsers', 'failed', 'Exact tab URLs differ')
        with patch.object(login_finalize, '_start_drives', return_value={'drive-a': True}), \
             patch.object(login_finalize, '_warm_cloud_metadata', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_file_manager', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_vscode', return_value=True), \
             patch.object(login_finalize, 'finish_deferred_codex', return_value=True):
            self.assertEqual(login_finalize.main(), 0)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(status['operation_state'], 'failed')
        self.assertEqual(status['overall_message'], 'Login completed with items needing attention')
        finalizer = next(stage for stage in status['stages'] if stage['id'] == 'login-finalization')
        self.assertEqual(finalizer['state'], 'ready')
        self.assertIn('browsers', finalizer['message'])
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
        for name, _label in login_status.default_stages():
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.finish()
        atomic_json(login_finalize._invocation_receipt(), self.context.to_dict())
        before = login_status.status_path().read_bytes()
        self.assertEqual(login_finalize.service_result(), 0)
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def ready_with_failed_finalizer(self):
        for name, _label in login_status.default_stages():
            login_status.update_stage(name, 'ready', 'Verified')
        login_status.update_stage('login-finalization', 'failed', 'Previous finalizer failed')

    def test_plain_finalizer_cannot_reopen_terminal_operation(self):
        self.ready_with_failed_finalizer()
        login_status.finish()
        before = login_status.status_path().read_bytes()
        with patch.object(login_finalize, '_finalize') as finalize:
            self.assertEqual(login_finalize.main(), 1)
        finalize.assert_not_called()
        self.assertEqual(login_status.status_path().read_bytes(), before)
