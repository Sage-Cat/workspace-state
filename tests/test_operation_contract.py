import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import login_status, operations
from workspace_state.concurrency import completed_jobs


class OperationContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary.name,
                                     'XDG_STATE_HOME': temporary.name + '/state'})
        env.start(); self.addCleanup(env.stop)
        operations.bind(None); self.addCleanup(operations.bind, None)
        login_status.initialize('a' * 16)
        self.startup = operations.current()

    def test_captured_publisher_cannot_adopt_newer_operation(self):
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        before = login_status.status_path().read_bytes()
        with operations.publisher(self.startup):
            self.assertFalse(login_status.update_stage('warmup', 'ready', 'late'))
            self.assertFalse(login_status.fail_active('late failure'))
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_parallel_workers_retain_exact_publisher_context(self):
        results = list(completed_jobs({str(i): operations.current for i in range(4)}))
        self.assertEqual(len(results), 4)
        self.assertTrue(all(value == self.startup and error is None for _, value, error in results))

    def test_corrupted_shutdown_status_cannot_be_recreated_by_startup_writer(self):
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        login_status.status_path().write_text('broken')
        with operations.publisher(self.startup):
            self.assertFalse(login_status.update_stage('warmup', 'ready', 'late'))
        self.assertEqual(login_status.status_path().read_text(), 'broken')

    def test_legacy_or_previous_attempt_receipt_cannot_authorize(self):
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        status = json.loads(login_status.status_path().read_text())
        self.assertFalse(operations.receipt_matches(status, {'operation_id': 'b' * 32}))
        receipt = {'operation_context': dict(status['operation_context'])}
        self.assertTrue(operations.receipt_matches(status, receipt))
        receipt['operation_context']['attempt'] += 1
        self.assertFalse(operations.receipt_matches(status, receipt))

    def test_expiry_rejects_commit_but_preserves_cancellation_authority(self):
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        status = json.loads(login_status.status_path().read_text())
        receipt = {'operation_context': status['operation_context']}
        with patch('workspace_state.operations.time.monotonic', return_value=status['operation_context']['deadline'] + 1):
            self.assertFalse(operations.receipt_matches(status, receipt))
            self.assertTrue(operations.receipt_matches(status, receipt, allow_expired=True))
            self.assertTrue(login_status.cancel_shutdown('expired', recovery_pending=True))
        current = json.loads(login_status.status_path().read_text())
        self.assertEqual(current['operation_state'], 'recovering')
        self.assertFalse(current['commit_authorized'])
        self.assertTrue(current['recovery_pending'])

    def test_recovery_cannot_authorize_or_be_skipped(self):
        status = {'operation_state': 'preparing'}
        operations.transition(status, 'cancelling')
        operations.transition(status, 'recovering')
        operations.transition(status, 'recovery-failed')
        with self.assertRaises(ValueError):
            operations.transition(status, 'authorized')
        operations.transition(status, 'recovering')
        operations.transition(status, 'cancelled')
        self.assertFalse(status['recovery_pending'])
        self.assertFalse(status['commit_authorized'])

    def test_malformed_and_nonfinite_deadlines_rejected(self):
        for deadline in (True, -1, 0, float('inf'), float('nan'), 'later'):
            value = self.startup.to_dict() | {'deadline': deadline}
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                operations.OperationContext.from_dict(value)

    def test_coordinator_ownership_is_exclusive_and_reusable_after_exit(self):
        path = login_status.status_path().parent / 'coordinator.lock'
        with operations.coordinator_lock(path):
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                with operations.coordinator_lock(path):
                    self.fail('second owner was admitted')
        with operations.coordinator_lock(path):
            self.assertTrue(path.is_file())


if __name__ == '__main__':
    unittest.main()
