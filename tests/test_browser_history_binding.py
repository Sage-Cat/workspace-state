"""Exact recovery of legacy browser evidence from private bounded generations."""
from argparse import Namespace
from copy import deepcopy
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, checkpoint, cli, storage
from test_browser_capture_binding import legacy_publication
from test_browser_reconciliation import observation


def old_terminal_autosave(original):
    current = {'version': 5, 'created_at': '2026-06-02T10:00:00+00:00',
               'sessions': [], 'terminals': [], 'desktop': {'shell_companion': True},
               'browsers': deepcopy(original['browsers'])}
    # The old consumer refused the legitimate slow observation and omitted its
    # witness, while its terminal-only producer copied browser provenance.
    with patch.object(reconciliation, 'observation_witness', return_value=None):
        checkpoint.record_provenance(current, original, source='terminal-autosave')
    return current


class BrowserHistoryBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        original, self.native = legacy_publication(terminal_warning=True)
        storage.save(original)
        self.original = storage.load()
        storage.save(old_terminal_autosave(self.original))
        self.current = storage.load()
        self.history = self.root / 'workspace-state/recovery/history'
        self.generation = next(self.history.glob('*.json'))

    def assertRejected(self, current=None):
        current = current or self.current
        before = deepcopy(current)
        canonical = storage.path_for().read_bytes()
        with self.assertRaises(reconciliation.ReconciliationRequired):
            reconciliation.reconcile(current, self.native)
        self.assertEqual(current, before)
        self.assertEqual(storage.path_for().read_bytes(), canonical)
        with patch.object(cli, 'ensure_browser_profiles') as launch, \
                patch.object(cli, 'restore_browser') as restore, \
                patch.object(cli, 'request_browser') as request, patch.object(cli, 'save') as save:
            with self.assertRaises(reconciliation.ReconciliationRequired):
                cli._restore_browsers(current, Namespace(workspace=None, dry_run=False,
                    no_place=False, login_status=False), start_browser=True)
            launch.assert_not_called()
            restore.assert_not_called()
            request.assert_not_called()
            save.assert_not_called()

    def test_slow_shutdown_then_old_hook_recovers_only_original_exact_publication(self):
        before = deepcopy(self.current)
        canonical = storage.path_for().read_bytes()
        history = {path.name: path.read_bytes() for path in self.history.iterdir()}
        evidence = reconciliation.recover_legacy_evidence(self.current)
        self.assertEqual(evidence['history_generation'], self.generation.name)
        self.assertEqual(evidence['capture_context'], self.original['capture_context'])
        self.assertEqual(evidence['created_at'], self.original['created_at'])
        _, receipt = reconciliation.reconcile(self.current, self.native)
        self.assertEqual(receipt['native_windows']['Default'], {'window-1': 100, 'window-2': 101})
        self.assertFalse(receipt['canonical_checkpoint_changed'])
        self.assertEqual(self.current, before)
        self.assertEqual(storage.path_for().read_bytes(), canonical)
        self.assertEqual({path.name: path.read_bytes() for path in self.history.iterdir()}, history)

    def test_recovered_witness_survives_later_autosaves_without_history(self):
        def autosave(timestamp):
            captured = {'version': 5, 'created_at': timestamp, 'sessions': [], 'terminals': [],
                        'desktop': {'shell_companion': True}, 'capture_errors': {'tmux': []}}
            with patch.object(cli, 'capture', return_value=captured), \
                    patch.object(cli, '_terminal_problems', return_value=[]), \
                    patch.object(cli, '_fallback_monitor_problem', return_value=None), \
                    patch.object(cli, '_checkpoint_login', return_value=None):
                path, problems = cli._autosave_from_tmux()
            self.assertIsNotNone(path)
            self.assertEqual(problems, [])
            return storage.load()
        first = autosave('2026-06-03T10:00:00+00:00')
        self.assertEqual(first['browsers'], self.current['browsers'])
        self.assertEqual(first['category_provenance']['browsers'], self.current['category_provenance']['browsers'])
        witness = first[reconciliation.WITNESS_KEY]
        self.assertEqual(witness['capture_evidence']['capture_context'], self.original['capture_context'])
        self.assertEqual(witness['capture_evidence']['history_generation'], self.generation.name)
        shutil.rmtree(self.history)
        second = autosave('2026-06-04T10:00:00+00:00')
        self.assertEqual(second[reconciliation.WITNESS_KEY], witness)
        before = deepcopy(second)
        reconciliation.reconcile(second, self.native)
        self.assertEqual(second, before)

    def test_missing_history_or_generation_cannot_supply_authority(self):
        self.generation.unlink()
        self.assertRejected()
        self.history.rmdir()
        self.assertRejected()

    def test_corrupt_or_hash_mismatched_managed_history_fails_closed(self):
        raw = self.generation.read_bytes()
        for content in (b'{', raw.replace(b'synthetic-topology', b'tampered-topology'),
                        b'{"version":5,"version":5}', b'{"nonfinite":NaN}',
                        b'{"nested":' + b'[' * 2000 + b'0' + b']' * 2000 + b'}'):
            with self.subTest(content=content[:35]):
                self.generation.write_bytes(content)
                self.assertRejected()
        self.generation.write_bytes(raw)
        renamed = self.generation.with_name(self.generation.name[:-25] + '0' * 20 + '.json')
        self.generation.rename(renamed)
        self.assertRejected()

    def test_unsafe_managed_file_or_directory_cannot_supply_authority(self):
        self.generation.chmod(0o644)
        self.assertRejected()
        self.generation.chmod(0o600)
        self.history.chmod(0o755)
        self.assertRejected()
        self.history.chmod(0o700)
        with patch.object(checkpoint.os, 'getuid', return_value=os.getuid() + 1):
            self.assertRejected()
        with patch.object(checkpoint, 'HISTORY_MAX_BYTES', 8):
            self.assertRejected()

    def test_managed_file_foreign_uid_is_rejected_independently_of_directory_owner(self):
        original_fstat = os.fstat
        def foreign_file(descriptor):
            metadata = original_fstat(descriptor)
            if stat.S_ISREG(metadata.st_mode):
                fields = list(metadata)
                fields[4] += 1
                return os.stat_result(fields)
            return metadata
        with patch.object(checkpoint.os, 'fstat', side_effect=foreign_file):
            self.assertRejected()

    def test_symlink_fifo_directory_and_hardlink_are_never_read_as_generation(self):
        raw = self.generation.read_bytes()
        name = self.generation.name
        outside = self.root / 'outside.json'
        outside.write_bytes(raw)
        outside.chmod(0o600)
        for kind in ('symlink', 'fifo', 'directory', 'hardlink'):
            with self.subTest(kind=kind):
                path = self.history / name
                path.unlink()
                if kind == 'symlink': path.symlink_to(outside)
                elif kind == 'fifo': os.mkfifo(path, 0o600)
                elif kind == 'directory': path.mkdir(mode=0o700)
                else: os.link(outside, path)
                self.assertRejected()
                if kind == 'directory': path.rmdir()
                else: path.unlink()
                path.write_bytes(raw)
                path.chmod(0o600)

    def test_symlink_history_directory_is_not_followed(self):
        actual = self.root / 'actual-history'
        self.history.rename(actual)
        self.history.symlink_to(actual, target_is_directory=True)
        self.assertRejected()

    def test_matching_conflicting_or_duplicate_publications_are_ambiguous(self):
        candidate = deepcopy(self.original)
        stamp = '2026-06-01T09:00:01.125+00:00'
        candidate['capture_context']['captured_at'] = stamp
        terminal = candidate['category_provenance']['terminals']
        terminal['captured_at'] = stamp
        terminal['capture_context']['captured_at'] = stamp
        checkpoint.keep_generation(self.history, candidate)
        self.assertRejected()
        next(path for path in self.history.glob('*.json') if path != self.generation).unlink()
        duplicate = self.history / ('20260605T100000000000Z-' + self.generation.name.split('-')[1])
        duplicate.write_bytes(self.generation.read_bytes())
        duplicate.chmod(0o600)
        self.assertRejected()

    def test_exact_browser_payload_and_retention_metadata_are_required(self):
        cases = {
            'observation URL': lambda s: observation(s)['browser_state']['profiles'][0]['windows'][0]['tabs'][0].update(url='https://example.test/edit'),
            'observation placement': lambda s: observation(s)['browser_state']['profiles'][0]['windows'][0].update(workspace_index=4),
            'recipe placement': lambda s: s['browsers']['google_chrome']['profiles'][0]['windows'][0].update(workspace_index=4),
            'observation capture time': lambda s: observation(s).update(captured_at='2026-06-01T09:00:05+00:00'),
            'retention attempt': lambda s: s['category_provenance']['browsers']['retention_evidence'].update(attempted_at='other'),
            'retention reason': lambda s: s['category_provenance']['browsers'].update(retained_reason='other'),
            'terminal source': lambda s: s['category_provenance']['terminals'].update(source='manual-save'),
            'terminal content': lambda s: s['desktop'].update(workspace_names=['changed']),
            'terminal timestamp': lambda s: s['category_provenance']['terminals'].update(captured_at='other'),
            'terminal partial': lambda s: s['category_provenance']['terminals'].update(state='failed', capture_errors=['missing terminal']),
            'claimed context': lambda s: s.update(capture_context={}),
            'invalid witness': lambda s: s.update(retained_browser_capture={}),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                current = deepcopy(self.current)
                mutate(current)
                self.assertRejected(current)

    def test_invalid_original_capture_is_not_upgraded_by_its_filename_hash(self):
        self.generation.unlink()
        candidate = deepcopy(self.original)
        candidate['capture_context']['provider_evidence']['browsers']['state'] = 'failed'
        checkpoint.keep_generation(self.history, candidate)
        self.assertRejected()

    def test_history_reader_is_bounded_and_ignores_unmanaged_notes(self):
        (self.history / 'notes.json').write_text('unmanaged')
        reconciliation.reconcile(self.current, self.native)
        for index in range(checkpoint.HISTORY_LIMIT):
            duplicate = self.history / (f'20260605T10000{index}000000Z-' + self.generation.name.split('-')[1])
            duplicate.write_bytes(self.generation.read_bytes())
            duplicate.chmod(0o600)
        self.assertRejected()

    def test_generation_hash_uses_the_writer_canonical_encoding_for_unicode(self):
        self.generation.unlink()
        original = deepcopy(self.original)
        original['synthetic_note'] = 'Café'
        checkpoint.keep_generation(self.history, original)
        reconciliation.reconcile(self.current, self.native)


if __name__ == '__main__':
    unittest.main()
