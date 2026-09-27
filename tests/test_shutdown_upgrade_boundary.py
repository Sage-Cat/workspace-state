import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import operations, shutdown_finalize as worker


class ShutdownUpgradeBoundaryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary.name,
                                     'XDG_STATE_HOME': temporary.name + '/state',
                                     worker.OPERATION_ENV: 'b' * 32})
        env.start(); self.addCleanup(env.stop)
        operations.bind(None); self.addCleanup(operations.bind, None)
        root = Path(temporary.name) / 'workspace-state'
        root.mkdir(mode=0o700)
        (root / 'login-generation').write_text('a' * 16)
        self.status = root / 'login-hud-status.json'
        self.status.write_text(json.dumps({'schema_version': 1, 'mode': 'shutdown',
            'session_id': 'a' * 16, 'operation_id': 'b' * 32,
            'shutdown_action': 'poweroff', 'shutdown_origin': 'preflight'}))
        self.status.chmod(0o600)

    def test_legacy_coordinator_cannot_start_preparation_or_get_a_receipt(self):
        before = self.status.read_bytes()
        with patch.object(worker, '_save_checkpoints') as save, \
             patch.object(worker, 'load_profiles') as profiles, \
             patch.object(worker, 'write_worker_complete_marker') as receipt:
            self.assertEqual(worker.run_transaction('b' * 32), 1)
        save.assert_not_called(); profiles.assert_not_called(); receipt.assert_not_called()
        self.assertEqual(self.status.read_bytes(), before)
        with self.assertRaisesRegex(RuntimeError, 'log out and back in'):
            worker._shutdown_context('b' * 32)

    def test_cleanup_of_refused_preparation_does_not_adopt_newer_status(self):
        with patch.object(worker.sys, 'argv', ['worker', '--rollback']), \
             patch.object(worker, 'transaction_exists', return_value=False), \
             patch.object(worker.operations, 'context_from_status') as adopt, \
             patch.object(worker, 'recover_transaction') as recover:
            self.assertEqual(worker.main(), 0)
        adopt.assert_not_called(); recover.assert_not_called()

    def test_existing_journal_is_preserved_when_context_is_missing(self):
        with patch.object(worker.sys, 'argv', ['worker', '--rollback']), \
             patch.object(worker, 'transaction_exists', return_value=True), \
             patch.object(worker, 'recover_transaction') as recover:
            self.assertEqual(worker.main(), 1)
        recover.assert_not_called()


if __name__ == '__main__':
    unittest.main()
