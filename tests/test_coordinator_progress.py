import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import login_status, operations
from workspace_state.gnome_session import GnomeSessionClient


class CoordinatorProgressTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'XDG_RUNTIME_DIR': temporary.name,
                                              'XDG_STATE_HOME': temporary.name + '/state'})
        environment.start(); self.addCleanup(environment.stop)
        operations.bind(None); self.addCleanup(operations.bind, None)
        login_status.initialize('a' * 16)
        self.context = operations.current()
        self.client = GnomeSessionClient(Mock(), Mock(), Path('/tools'))
        self.client._operation_context = self.context

    def pending(self):
        document = json.loads(login_status.status_path().read_text())
        document['stages'][0]['provider_results'] = [{'placement': {'state': 'waiting', 'request_id': 'one'}}]
        login_status.status_path().write_text(json.dumps(document))

    def test_observation_is_throttled_and_never_overlaps(self):
        self.pending()
        callbacks = []
        with patch.object(self.client, '_spawn', side_effect=lambda command, cb: callbacks.append((command, cb)) or 123):
            self.client._poll_placement_progress()
            self.client._poll_placement_progress()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(callbacks[0][0], ['/tools/wsctl', 'placement-progress'])
            callbacks[0][1](0)
            self.client._poll_placement_progress()
            self.assertEqual(len(callbacks), 1)

    def test_shutdown_suspension_stops_observation(self):
        self.pending()
        self.client._startup_blocked_by_shutdown = True
        with patch.object(self.client, '_spawn') as spawn:
            self.client._poll_placement_progress()
        spawn.assert_not_called()

    def test_superseded_context_is_never_adopted(self):
        self.pending()
        login_status.initialize_shutdown('a' * 16, 'b' * 32)
        with patch.object(self.client, '_spawn') as spawn:
            self.client._poll_placement_progress()
        spawn.assert_not_called()
        self.assertTrue(self.client._placement_progress_finished)

    def test_terminal_operation_stops_even_with_pending_evidence(self):
        for state in ('completed', 'cancelled', 'failed'):
            with self.subTest(state=state):
                self.pending()
                document = json.loads(login_status.status_path().read_text())
                document['operation_state'] = state
                login_status.status_path().write_text(json.dumps(document))
                self.client._placement_progress_finished = False
                self.client._placement_progress_next = 0
                with patch.object(self.client, '_spawn') as spawn:
                    self.client._poll_placement_progress()
                spawn.assert_not_called()
                self.assertTrue(self.client._placement_progress_finished)

    def test_expiry_observes_once_even_without_a_request(self):
        callbacks = []
        with patch('workspace_state.operations.time.monotonic', return_value=self.context.deadline + 1), \
             patch.object(self.client, '_spawn', side_effect=lambda command, cb: callbacks.append(cb) or 123):
            self.client._poll_placement_progress()
            self.assertEqual(len(callbacks), 1)
            callbacks[0](0)
            self.client._poll_placement_progress()
            self.assertEqual(len(callbacks), 1)
        self.assertTrue(self.client._placement_progress_finished)


if __name__ == '__main__':
    unittest.main()
