"""Terminal autosaves preserve validated observation evidence, not a new baseline."""
from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, checkpoint, cli, storage
from test_browser_reconciliation import fixture, observation
from test_browser_capture_binding import legacy_publication


class BrowserObservationWitnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = patch.dict(os.environ, {'XDG_DATA_HOME': str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.snapshot, self.native = fixture()
        self.snapshot.update(version=5, sessions=[], terminals=[], desktop={'shell_companion': True})
        storage.save(self.snapshot)

    def autosave(self, timestamp='2026-10-04T12:00:00+00:00'):
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

    def test_actual_repeated_autosaves_keep_browser_and_original_evidence_unchanged(self):
        _, initial = reconciliation.reconcile(self.snapshot, self.native)
        first = self.autosave()
        second = self.autosave('2026-10-05T12:00:00+00:00')
        self.assertIsNone(second.get('capture_context'))
        self.assertEqual(first['browsers'], self.snapshot['browsers'])
        self.assertEqual(second['browsers'], self.snapshot['browsers'])
        self.assertEqual(first[reconciliation.WITNESS_KEY], second[reconciliation.WITNESS_KEY])
        proof = second[reconciliation.WITNESS_KEY]
        self.assertEqual(proof['browser_digest'], reconciliation.digest(reconciliation.browser_state(self.snapshot)))
        self.assertEqual(proof['capture_evidence']['created_at'], self.snapshot['created_at'])
        self.assertEqual(proof['capture_evidence']['capture_context'], self.snapshot['capture_context'])
        self.assertEqual(first['category_provenance']['browsers'], second['category_provenance']['browsers'])
        self.assertNotIn('adoption', second['category_provenance']['browsers'])
        before = deepcopy(second)
        _, result = reconciliation.reconcile(second, self.native)
        self.assertEqual(result['native_windows'], initial['native_windows'])
        self.assertFalse(result['canonical_checkpoint_changed'])
        self.assertEqual(second, before)
        self.assertEqual(storage.load(), before)

    def test_legacy_original_publication_evidence_survives_repeated_terminal_autosaves(self):
        self.snapshot, self.native = legacy_publication(terminal_warning=True)
        storage.save(self.snapshot)
        first, second = self.autosave(), self.autosave('2026-06-03T12:00:00+00:00')
        self.assertEqual(first[reconciliation.WITNESS_KEY], second[reconciliation.WITNESS_KEY])
        evidence = second[reconciliation.WITNESS_KEY]['capture_evidence']
        self.assertEqual(evidence['category_provenance'], self.snapshot['category_provenance'])
        self.assertEqual(evidence['terminal_content_digest'], checkpoint.category_digest(self.snapshot, 'terminals'))
        self.assertIsNone(second.get('capture_context'))
        before = deepcopy(second)
        reconciliation.reconcile(second, self.native)
        self.assertEqual(second, before)
        self.assertEqual(storage.load(), before)

    def test_browser_recipe_or_observation_edits_invalidate_witness(self):
        hook = self.autosave()
        for category in ('recipe', 'observation'):
            with self.subTest(category=category):
                changed = deepcopy(hook)
                browser = (reconciliation.browser_state(changed) if category == 'recipe'
                           else observation(changed)['browser_state'])
                browser['profiles'][0]['windows'][0]['tabs'][0]['url'] = 'https://example.test/user-edit'
                with self.assertRaisesRegex(reconciliation.ReconciliationRequired, 'invalid or obsolete'):
                    reconciliation.reconcile(changed, self.native)
                storage.save(changed)
                next_hook = self.autosave()
                self.assertNotIn(reconciliation.WITNESS_KEY, next_hook)
                self.assertEqual(next_hook['browsers'], changed['browsers'])
                with self.assertRaises(reconciliation.ReconciliationRequired):
                    reconciliation.reconcile(next_hook, self.native)

    def test_invalid_witness_evidence_never_gains_new_capture_authority(self):
        valid = self.autosave()
        cases = {
            'schema': lambda w: w.update(schema_version=2),
            'source': lambda w: w.update(source='manual-save'),
            'digest': lambda w: w.update(browser_digest='0' * 64),
            'missing evidence': lambda w: w.pop('capture_evidence'),
            'timestamp': lambda w: w['capture_evidence'].update(created_at='2026-10-01T20:00:00+00:00'),
            'capture schema': lambda w: w['capture_evidence']['capture_context'].update(schema_version=3),
            'provider failure': lambda w: w['capture_evidence']['capture_context']['provider_evidence']['browsers'].update(state='failed'),
            'missing reason': lambda w: w['capture_evidence'].update(preserved_categories=[]),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                changed = deepcopy(valid)
                mutate(changed[reconciliation.WITNESS_KEY])
                with self.assertRaises(reconciliation.ReconciliationRequired):
                    reconciliation.reconcile(changed, self.native)
                storage.save(changed)
                hook = self.autosave()
                self.assertNotIn(reconciliation.WITNESS_KEY, hook)
                with self.assertRaises(reconciliation.ReconciliationRequired):
                    reconciliation.reconcile(hook, self.native)

    def test_initial_unverified_observation_is_not_upgraded_from_category_provenance(self):
        invalid = deepcopy(self.snapshot)
        invalid.pop('capture_context')
        invalid['category_provenance'] = {'browsers': {
            'schema_version': 1, 'content_digest': checkpoint.category_digest(invalid, 'browsers'),
            'source': 'shutdown-save', 'state': 'captured',
            'capture_context': deepcopy(self.snapshot['capture_context']),
        }}
        storage.save(invalid)
        hook = self.autosave()
        self.assertNotIn(reconciliation.WITNESS_KEY, hook)
        self.assertEqual(hook['browsers'], invalid['browsers'])
        with self.assertRaises(reconciliation.ReconciliationRequired):
            reconciliation.reconcile(hook, self.native)

    def test_full_capture_discards_obsolete_witness(self):
        hook = self.autosave()
        for source in ('manual-save', 'shutdown-save'):
            with self.subTest(source=source):
                fresh = deepcopy(self.snapshot)
                fresh[reconciliation.WITNESS_KEY] = deepcopy(hook[reconciliation.WITNESS_KEY])
                fresh['browsers']['google_chrome'].pop('latest_observation')
                checkpoint.record_provenance(fresh, hook, source=source)
                self.assertNotIn(reconciliation.WITNESS_KEY, fresh)
                self.assertIsNone(reconciliation.retained_observation(fresh))

    def test_changed_candidate_cannot_inherit_valid_previous_witness(self):
        changed = deepcopy(self.snapshot)
        changed['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0]['url'] = 'https://example.test/other'
        checkpoint.record_provenance(changed, self.snapshot, source='terminal-autosave')
        self.assertNotIn(reconciliation.WITNESS_KEY, changed)

    def test_native_changes_still_reject_even_with_valid_original_capture_witness(self):
        hook = self.autosave()
        self.native['Default']['windows'][0]['tabs'][0]['url'] = 'https://example.test/current-change'
        with self.assertRaisesRegex(reconciliation.ReconciliationRequired, 'native URLs or group structure'):
            reconciliation.reconcile(hook, self.native)
