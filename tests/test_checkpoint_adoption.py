from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import checkpoint, cli, operations, storage
from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class CheckpointAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            'XDG_DATA_HOME': str(self.root / 'data'),
            'XDG_RUNTIME_DIR': str(self.root / 'runtime'),
            'XDG_CONFIG_HOME': str(self.root / 'config'),
            'WSCTL_OPERATION_CONTEXT': '',
        }))
        self.stack.enter_context(patch.object(operations, 'current', return_value=None))
        self.stack.enter_context(patch.object(cli, '_boot_id', return_value='test-boot'))
        self.generation = self.stack.enter_context(patch.object(cli, '_login_generation_file', return_value='a' * 16))
        self.context = operations.OperationContext('test-boot', 'a' * 16, 'b' * 32, 'shutdown', 1, 100)
        self.stack.enter_context(patch.object(cli, '_marker_context', return_value=self.context))
        self.stack.enter_context(patch.object(cli, '_startup_marker', side_effect=lambda name: self.root / f'{name}.done'))
        self.stack.enter_context(patch.object(cli, '_arm_autosave'))
        self.stack.enter_context(patch.object(cli, '_shutdown_allows_unresolved_codex', return_value=True))
        self.stack.enter_context(patch.object(cli, 'update_stage'))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        self.failed_marker = StageMarker('browsers', 'failed', message='restore failed')
        write_stage_marker(self.root / 'browsers.done', self.failed_marker)

    def recipe(self, label='original'):
        return {
            'created_at': label,
            'desktop': {'shell_companion': True}, 'sessions': [], 'terminals': [],
            'browsers': {'google_chrome': {'available': True, 'profiles': [{
                'profile': 'Default', 'windows': [{
                    'id': 'one', 'workspace_index': 0, 'monitor': 0,
                    'tabs': [{'url': f'https://example.test/{label}'}],
                }],
            }]}},
        }

    def save(self, snapshot, *, shutdown=False, partial=False):
        with patch.object(cli, '_capture_all', return_value=copy.deepcopy(snapshot)):
            result = cli.cmd_save(argparse.Namespace(allow_partial=partial, shutdown_safe=shutdown))
        return result, storage.load()

    def autosave(self, label='terminal-hook'):
        snapshot = self.recipe(label)
        del snapshot['browsers']
        with patch.object(cli, 'capture', return_value=snapshot):
            path, problems = cli._autosave_from_tmux()
        self.assertIsNotNone(path)
        self.assertEqual(problems, [])
        return storage.load()

    def test_manual_save_adopts_failed_restore_then_accepts_later_shutdown_capture(self):
        storage.save(self.recipe())
        _, adopted = self.save(self.recipe('manual'))
        self.assertEqual(read_stage_marker(self.root / 'browsers.done'), self.failed_marker)
        _, saved = self.save(self.recipe('later'), shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('later')['browsers'])
        self.assertFalse(saved.get('capture_errors', {}).get('preserved_categories'))
        self.assertIn('adoption', adopted['category_provenance']['browsers'])
        # A second shutdown capture in the same login remains authorized.
        _, saved = self.save(self.recipe('latest'), shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('latest')['browsers'])

    def test_terminal_only_autosave_preserves_adoption_without_adopting_other_data(self):
        _, adopted = self.save(self.recipe('manual'))
        hook = self.autosave()
        self.assertEqual(hook['category_provenance']['browsers'], adopted['category_provenance']['browsers'])
        _, saved = self.save(self.recipe('later'), shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('later')['browsers'])

    def test_terminal_only_save_cannot_adopt_failed_startup(self):
        storage.save(self.recipe('protected'))
        self.autosave()
        result, saved = self.save(self.recipe('live'), shutdown=True, partial=True)
        self.assertEqual(result, 3)
        self.assertEqual(saved['browsers']['google_chrome']['profiles'], self.recipe('protected')['browsers']['google_chrome']['profiles'])
        self.assertNotIn('adoption', saved['category_provenance']['browsers'])

    def test_partial_save_adopts_healthy_browser_but_not_failed_terminal_capture(self):
        snapshot = self.recipe('manual')
        snapshot['capture_errors'] = {'tmux': ['capture failed']}
        _, saved = self.save(snapshot, partial=True)
        self.assertIn('adoption', saved['category_provenance']['browsers'])
        self.assertNotIn('adoption', saved['category_provenance']['terminals'])
        _, saved = self.save(self.recipe('later'), shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('later')['browsers'])

    def test_partial_failed_browser_capture_preserves_unadopted_recipe(self):
        storage.save(self.recipe('protected'))
        broken = self.recipe('partial')
        broken['browsers']['google_chrome']['errors'] = ['missing profile']
        _, saved = self.save(broken, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('protected')['browsers'])
        self.assertNotIn('adoption', saved['category_provenance']['browsers'])
        self.assertTrue(saved['capture_errors']['preserved_categories'])

    def test_prior_login_or_changed_recipe_cannot_reuse_adoption(self):
        for invalidation in ('other-login', 'changed-content', 'other-boot'):
            with self.subTest(invalidation=invalidation):
                self.generation.return_value = 'a' * 16
                _, adopted = self.save(self.recipe('manual'))
                if invalidation == 'other-login':
                    self.generation.return_value = 'c' * 16
                elif invalidation == 'changed-content':
                    adopted['browsers'] = self.recipe('changed')['browsers']
                    storage.save(adopted)
                else:
                    adopted['category_provenance']['browsers']['adoption']['boot_id'] = 'other-boot'
                    storage.save(adopted)
                result, saved = self.save(self.recipe('live'), shutdown=True, partial=True)
                self.assertEqual(result, 3)
                self.assertEqual(saved['browsers']['google_chrome']['profiles'], adopted['browsers']['google_chrome']['profiles'])

    def test_no_login_identity_does_not_create_adoption(self):
        self.generation.return_value = None
        _, saved = self.save(self.recipe('manual'))
        self.assertNotIn('adoption', saved['category_provenance']['browsers'])

    def test_stale_inherited_worker_cannot_adopt_current_login_capture(self):
        stale = operations.OperationContext('test-boot', 'old-login', 'b' * 32, 'startup', 1, 100)
        with patch.object(operations, 'current', return_value=stale):
            _, saved = self.save(self.recipe('manual'))
        self.assertNotIn('adoption', saved['category_provenance']['browsers'])

    def test_login_change_during_capture_preserves_existing_checkpoint(self):
        storage.save(self.recipe('protected'))
        before = storage.path_for().read_bytes()
        self.generation.side_effect = ['a' * 16, 'c' * 16]
        with self.assertRaisesRegex(RuntimeError, 'login changed'):
            self.save(self.recipe('manual'))
        self.assertEqual(storage.path_for().read_bytes(), before)

    def test_failed_capture_after_adoption_retains_baseline_and_error_evidence(self):
        _, adopted = self.save(self.recipe('manual'))
        broken = self.recipe('broken')
        broken['browsers']['google_chrome']['available'] = False
        _, saved = self.save(broken, shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], adopted['browsers'])
        record = saved['category_provenance']['browsers']
        self.assertEqual(record['captured_at'], 'manual')
        self.assertEqual(record['adoption'], adopted['category_provenance']['browsers']['adoption'])
        self.assertIn('unavailable', record['retention_evidence']['capture_errors'][0])
        hook = self.autosave()
        self.assertEqual(hook['category_provenance']['browsers'], record)
        _, saved = self.save(self.recipe('healthy'), shutdown=True, partial=True)
        self.assertEqual(saved['browsers'], self.recipe('healthy')['browsers'])

    def test_legacy_hook_keeps_context_and_warning_without_claiming_capture_age(self):
        legacy = self.recipe('old-terminal-time')
        legacy['capture_context'] = {'schema_version': 1, 'captured_at': 'legacy-context',
                                     'topology_signature': 'old', 'provider_evidence': {}}
        legacy['capture_errors'] = {'preserved_categories': ['browser capture incomplete']}
        storage.save(legacy)
        hook = self.autosave('new-terminal-time')
        record = hook['category_provenance']['browsers']
        self.assertIsNone(record['captured_at'])
        self.assertEqual(record['capture_context'], legacy['capture_context'])
        self.assertEqual(hook['capture_errors']['preserved_categories'], legacy['capture_errors']['preserved_categories'])

    def test_show_distinguishes_checkpoint_update_from_browser_capture_age(self):
        self.save(self.recipe('browser-time'))
        self.autosave('terminal-time')
        output = io.StringIO()
        with redirect_stdout(output):
            cli.cmd_show(argparse.Namespace(json=False, details=False))
        self.assertIn('Checkpoint updated  terminal-time', output.getvalue())
        self.assertIn('browsers: captured browser-time', output.getvalue())

    def test_failed_manual_publication_cannot_change_adoption_or_checkpoint(self):
        storage.save(self.recipe('original'))
        before = storage.path_for().read_bytes()
        with patch.object(cli, 'save', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.save(self.recipe('manual'))
        self.assertEqual(storage.path_for().read_bytes(), before)
        self.assertEqual(read_stage_marker(self.root / 'browsers.done'), self.failed_marker)

    def test_hook_preserves_browser_capture_age_evidence_and_retention_warning(self):
        # Full capture evidence belongs to the original browser recipe, not the
        # later retained observation or terminal-only snapshot timestamp.
        original = self.recipe('original')
        original['capture_context'] = {
            'schema_version': 1, 'topology_signature': 'topology',
            'captured_at': 'original-time',
            'provider_evidence': {'browsers': {'state': 'captured', 'detail': 'complete'}},
        }
        with patch.object(checkpoint, 'verify_capture_context'):
            _, original = self.save(original)
        self.generation.return_value = 'c' * 16  # Adoption belongs to earlier login.
        _, retained = self.save(self.recipe('observation'), shutdown=True, partial=True)
        before = copy.deepcopy(retained)
        hook = self.autosave('new-terminal-time')
        self.assertEqual(hook['created_at'], 'new-terminal-time')
        self.assertEqual(hook['category_provenance']['browsers'], retained['category_provenance']['browsers'])
        provenance = hook['category_provenance']['browsers']
        self.assertEqual(provenance['captured_at'], 'original-time')
        self.assertEqual(provenance['capture_context'], original['category_provenance']['browsers']['capture_context'])
        self.assertIn('restoration did not complete', provenance['retained_reason'])
        self.assertEqual(hook['capture_errors']['preserved_categories'], retained['capture_errors']['preserved_categories'])
        self.assertEqual(hook['browsers'], retained['browsers'])
        self.assertEqual(retained, before)

    def test_rejected_partial_save_preserves_original_bytes_and_history(self):
        storage.save(self.recipe('original'))
        before = storage.path_for().read_bytes()
        broken = self.recipe('partial')
        broken['browsers']['google_chrome']['available'] = False
        with self.assertRaises(RuntimeError):
            self.save(broken)
        self.assertEqual(storage.path_for().read_bytes(), before)
        self.assertFalse((self.root / 'data/workspace-state/recovery/history').exists())


if __name__ == '__main__':
    unittest.main()
