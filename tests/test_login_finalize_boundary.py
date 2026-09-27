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


if __name__ == '__main__':
    unittest.main()
