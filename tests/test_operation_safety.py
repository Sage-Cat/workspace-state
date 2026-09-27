"""Regression cases reproduced by the architecture review; no desktop mutations."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from workspace_state import login_status
from workspace_state.gnome_session import GnomeSessionClient


class OperationSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.temp.name,
                                     'XDG_STATE_HOME': self.temp.name + '/state'})
        env.start(); self.addCleanup(env.stop)
        login_status.initialize('a' * 16)
        login_status.initialize_shutdown('a' * 16, 'b' * 32)

    def client(self):
        client = GnomeSessionClient(Mock(), Mock(), Path('/unused'))
        client._checkpoint_active = True
        client._shutdown_operation_id = 'b' * 32
        client._shutdown_unit = 'wsctl-shutdown-finalize@' + 'b' * 32 + '.service'
        return client

    def test_late_startup_stage_cannot_pollute_shutdown(self):
        before = login_status.status_path().read_bytes()
        self.assertFalse(login_status.update_stage('warmup', 'failed', 'late event'))
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_cancel_retains_ownership_while_stop_callback_is_pending(self):
        client = self.client()
        with patch.object(client, '_acquire_shutdown_inhibitor'), \
             patch.object(client, '_clear_shutdown_coordination'), \
             patch.object(client, '_stop_shutdown_unit') as stop:
            client._cancel_verified_preflight('test cancel')
            client._forget_finished_preflight()
        self.assertTrue(client._shutdown_recovery_pending)
        self.assertTrue(client._checkpoint_active)
        self.assertEqual(client._shutdown_operation_id, 'b' * 32)
        self.assertEqual(stop.call_count, 1)

    def test_old_recovery_callback_cannot_reset_new_operation(self):
        client = self.client()
        with patch.object(client, '_acquire_shutdown_inhibitor'), \
             patch.object(client, '_clear_shutdown_coordination'), \
             patch.object(client, '_stop_shutdown_unit') as stop:
            client._cancel_verified_preflight('test cancel')
        callback = stop.call_args.args[1]
        client._shutdown_operation_id = 'c' * 32
        login_status.initialize_shutdown('a' * 16, 'c' * 32)
        before = login_status.status_path().read_bytes()
        with patch('workspace_state.gnome_session.transaction_exists', return_value=False):
            callback(0)
        self.assertEqual(client._shutdown_operation_id, 'c' * 32)
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_failed_rollback_keeps_exclusive_recovery_ownership(self):
        client = self.client()
        with patch.object(client, '_acquire_shutdown_inhibitor'), \
             patch.object(client, '_clear_shutdown_coordination'), \
             patch.object(client, '_stop_shutdown_unit') as stop:
            client._cancel_verified_preflight('test cancel')
        callback = stop.call_args.args[1]
        with patch('workspace_state.gnome_session.transaction_exists', return_value=True):
            callback(1)
        client._forget_finished_preflight()
        self.assertTrue(client._shutdown_recovery_pending)
        self.assertEqual(client._shutdown_operation_id, 'b' * 32)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(next(s['state'] for s in status['stages']
                              if s['id'] == 'profile-recovery'), 'failed')

    def test_reused_operation_id_does_not_make_old_callback_current(self):
        client = self.client()
        with patch.object(client, '_acquire_shutdown_inhibitor'), \
             patch.object(client, '_clear_shutdown_coordination'), \
             patch.object(client, '_stop_shutdown_unit') as stop:
            client._cancel_verified_preflight('test cancel')
        callback = stop.call_args.args[1]
        client._reset_shutdown_attempt()
        client._shutdown_operation_id = 'b' * 32
        client._shutdown_recovery_pending = True
        before = login_status.status_path().read_bytes()
        with patch('workspace_state.gnome_session.transaction_exists', return_value=False):
            callback(0)
        self.assertTrue(client._shutdown_recovery_pending)
        self.assertEqual(login_status.status_path().read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
