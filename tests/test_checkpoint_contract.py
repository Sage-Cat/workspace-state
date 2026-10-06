import json
from datetime import datetime
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import checkpoint, storage
from workspace_state.util import CommandError


class CheckpointContractTests(unittest.TestCase):
    def test_known_legacy_versions_migrate_without_editing_input(self):
        for version in (None, 1, 2, 3, 4, 5):
            source = {'sessions': []}
            if version is not None:
                source['version'] = version
            migrated = checkpoint.migrate(source)
            self.assertEqual(migrated['version'], 5)
            self.assertEqual(source.get('version'), version)

    def test_unknown_schema_cannot_replace_current_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_DATA_HOME': directory}):
            storage.save({'sessions': [], 'created_at': 'known'})
            before = storage.path_for().read_bytes()
            for version in (True, 0, 6, '2'):
                with self.subTest(version=version), self.assertRaises(ValueError):
                    storage.save({'version': version, 'sessions': []})
                self.assertEqual(storage.path_for().read_bytes(), before)

    def test_hotplug_during_capture_rejects_publication(self):
        shell = {'monitors': [{'index': 0, 'connector': 'DP-1', 'width': 100}], 'workspaces': []}
        with patch('workspace_state.checkpoint.capture_shell', return_value=shell), \
             patch('workspace_state.checkpoint.workspace_names', return_value=['One']):
            context = checkpoint.CaptureContext.begin()
        context.verify(shell)
        with self.assertRaises(CommandError):
            context.verify({'monitors': [], 'workspaces': []})
        self.assertEqual(context.shell, shell)

    def test_capture_transaction_covers_initial_shell_read_through_completion(self):
        shell = {'monitors': [], 'workspaces': []}
        start = datetime.fromisoformat('2026-06-01T09:00:00.125+00:00')
        finish = datetime.fromisoformat('2026-06-01T09:00:04.500+00:00')
        with patch.object(checkpoint, 'datetime') as clock, \
                patch.object(checkpoint, 'capture_shell') as read_shell, \
                patch.object(checkpoint, 'workspace_names', return_value=['Synthetic']):
            clock.now.side_effect = [start, finish]
            def initial_shell():
                self.assertEqual(clock.now.call_count, 1)
                return shell
            read_shell.side_effect = initial_shell
            context = checkpoint.CaptureContext.begin()
            providers = {'browsers': {'state': 'captured', 'content_digest': 'a' * 64}}
            evidence = context.evidence(providers, created_at='2026-06-01T09:00:02+00:00')
        self.assertEqual(evidence['schema_version'], 2)
        self.assertEqual(evidence['capture_id'], context.capture_id)
        self.assertEqual(evidence['captured_at'], start.isoformat())
        self.assertEqual(evidence['completed_at'], finish.isoformat())
        self.assertEqual(evidence['snapshot_created_at'], '2026-06-01T09:00:02+00:00')
        providers['browsers']['state'] = 'failed'
        self.assertEqual(evidence['provider_evidence']['browsers']['state'], 'captured')

    def test_bounded_private_history_preserves_unmanaged_files(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_DATA_HOME': directory}):
            history = Path(directory) / 'workspace-state/recovery/history'
            history.mkdir(parents=True)
            (history / 'notes.json').write_text('keep')
            for index in range(checkpoint.HISTORY_LIMIT + 4):
                storage.save({'sessions': [], 'created_at': str(index)})
            generations = list(history.glob('*-*.json'))
            self.assertEqual(len(generations), checkpoint.HISTORY_LIMIT)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in generations))
            self.assertEqual((history / 'notes.json').read_text(), 'keep')
            self.assertEqual(storage.load()['created_at'], str(checkpoint.HISTORY_LIMIT + 3))


if __name__ == '__main__':
    unittest.main()
