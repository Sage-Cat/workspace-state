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

    def test_parallel_workers_retain_exact_publisher_context(self):
        results = list(completed_jobs({str(i): operations.current for i in range(4)}))
        self.assertEqual(len(results), 4)
        self.assertTrue(all(value == self.startup and error is None for _, value, error in results))

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
