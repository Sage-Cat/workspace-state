"""Synthetic capture ownership regressions; no live desktop inputs are used."""
from copy import deepcopy
from datetime import datetime
from argparse import Namespace
from pathlib import Path
import unittest
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, checkpoint, cli
from test_browser_reconciliation import fixture, observation


def legacy_publication(*, terminal_warning=False):
    previous, native = fixture()
    previous['browsers']['google_chrome'].pop('latest_observation')
    current = {
        'version': 5, 'created_at': '2026-06-01T09:00:04+00:00',
        'sessions': [], 'terminals': [], 'desktop': {'shell_companion': True},
        'browsers': {'google_chrome': deepcopy(fixture()[0]['browsers']['google_chrome']['latest_observation']['browser_state'])},
        'capture_context': {
            'schema_version': 1, 'captured_at': '2026-06-01T09:00:00.125+00:00',
            'topology_signature': 'synthetic-topology',
            'provider_evidence': {name: {'state': 'captured', 'detail': 'provider capture completed'}
                                  for name in ('terminals', 'browsers', 'social_apps', 'file_manager', 'vscode')},
        },
    }
    with patch.object(cli, '_startup_marker', return_value=Path('/nonexistent/synthetic-marker')), \
            patch.object(cli, 'read_stage_marker', return_value=None), \
            patch.object(cli, '_checkpoint_login', return_value=None):
        retained = cli._retain_unrestored_recipes(current, previous)
    problems = {'terminals': ['1 Codex session ID(s) are unresolved']} if terminal_warning else {}
    checkpoint.record_provenance(current, previous, source='shutdown-save', retained=retained, problems=problems)
    return current, native


class BrowserCaptureBindingTests(unittest.TestCase):
    def assertRejected(self, snapshot, native):
        before = deepcopy((snapshot, native))
        with self.assertRaises(reconciliation.ReconciliationRequired):
            reconciliation.reconcile(snapshot, native)
        self.assertEqual((snapshot, native), before)

    def test_legacy_completed_capture_is_not_limited_to_one_second(self):
        snapshot, native = legacy_publication()
        original = deepcopy((snapshot, native))
        _, receipt = reconciliation.reconcile(snapshot, native)
        self.assertEqual(receipt['native_windows']['Default'], {'window-1': 100, 'window-2': 101})
        self.assertEqual((snapshot, native), original)

    def test_legacy_terminal_uuid_warning_does_not_invalidate_browser_capture(self):
        snapshot, native = legacy_publication(terminal_warning=True)
        self.assertEqual(snapshot['category_provenance']['terminals']['state'], 'failed')
        reconciliation.reconcile(snapshot, native)

    def test_nearby_timestamps_without_producer_ownership_are_unbound(self):
        snapshot, native = fixture()
        observation(snapshot).update(schema_version=1, captured_at=snapshot['created_at'])
        snapshot['capture_context'] = {
            'schema_version': 1, 'captured_at': '2026-10-03T20:00:00.125+00:00',
            'provider_evidence': {'browsers': {'state': 'captured'}},
        }
        self.assertRejected(snapshot, native)

    def test_legacy_stale_copied_partial_and_inconsistent_publications_reject(self):
        cases = {
            'missing category ownership': lambda s: s.pop('category_provenance'),
            'copied observation': lambda s: observation(s).update(captured_at='2026-06-01T08:00:04+00:00'),
            'copied context': lambda s: s['capture_context'].update(captured_at='2026-06-01T08:00:00.125+00:00'),
            'future context': lambda s: s['capture_context'].update(captured_at='2026-06-02T09:00:00.125+00:00'),
            'stale outer': lambda s: s.update(created_at='2026-06-02T09:00:04+00:00'),
            'wrong attempt': lambda s: s['category_provenance']['browsers']['retention_evidence'].update(attempted_at='other'),
            'partial browser': lambda s: s['category_provenance']['browsers']['retention_evidence'].update(capture_errors=['missing profile']),
            'wrong reason': lambda s: s['category_provenance']['browsers'].update(retained_reason='other'),
            'recipe edit': lambda s: s['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0].update(url='https://example.test/edit'),
            'terminal edit': lambda s: s['desktop'].update(workspace_names=['changed']),
            'wrong source': lambda s: s['category_provenance']['terminals'].update(source='terminal-autosave'),
            'wrong terminal digest': lambda s: s['category_provenance']['terminals'].update(content_digest='0' * 64),
            'partial terminal': lambda s: s['category_provenance']['terminals'].update(state='failed', capture_errors=['tmux unavailable']),
            'missing topology': lambda s: s['capture_context'].pop('topology_signature'),
            'terminal provider incomplete': lambda s: s['capture_context']['provider_evidence']['terminals'].update(state='failed'),
            'browser provider incomplete': lambda s: s['capture_context']['provider_evidence']['browsers'].update(state='failed'),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                snapshot, native = legacy_publication()
                mutate(snapshot)
                self.assertRejected(snapshot, native)

    def test_new_capture_binds_exact_content_identity_and_completion(self):
        snapshot, native = fixture()
        observed = observation(snapshot)
        context = snapshot['capture_context']
        self.assertNotEqual(observed['captured_at'], snapshot['created_at'])
        self.assertEqual(observed['capture_id'], context['capture_id'])
        self.assertEqual(observed['captured_at'], context['completed_at'])
        self.assertEqual(observed['browser_digest'], context['provider_evidence']['browsers']['content_digest'])
        self.assertEqual(observed['retained_recipe_digest'], reconciliation.recipe_digest(reconciliation.browser_state(snapshot)))
        reconciliation.reconcile(snapshot, native)

    def test_new_transaction_stale_copied_tampered_and_partial_observations_reject(self):
        cases = {
            'missing identity': lambda s: observation(s).pop('capture_id'),
            'copied observation identity': lambda s: observation(s).update(capture_id='00000000-0000-4000-8000-000000000002'),
            'copied context identity': lambda s: s['capture_context'].update(capture_id='00000000-0000-4000-8000-000000000002'),
            'invalid identity': lambda s: s['capture_context'].update(capture_id='invalid'),
            'missing completion': lambda s: s['capture_context'].pop('completed_at'),
            'copied completion': lambda s: observation(s).update(captured_at='2026-10-03T20:00:05+00:00'),
            'capture after completion': lambda s: s['capture_context'].update(captured_at='2026-10-03T20:00:06+00:00'),
            'outer outside capture': lambda s: (s.update(created_at='2026-10-02T20:00:00+00:00'), s['capture_context'].update(snapshot_created_at='2026-10-02T20:00:00+00:00')),
            'copied outer binding': lambda s: s['capture_context'].update(snapshot_created_at='2026-10-04T20:00:00+00:00'),
            'missing provider digest': lambda s: s['capture_context']['provider_evidence']['browsers'].pop('content_digest'),
            'copied provider digest': lambda s: s['capture_context']['provider_evidence']['browsers'].update(content_digest='0' * 64),
            'tampered URL': lambda s: observation(s)['browser_state']['profiles'][0]['windows'][0]['tabs'][0].update(url='https://example.test/tampered'),
            'tampered placement': lambda s: observation(s)['browser_state']['profiles'][0]['windows'][0].update(workspace_index=3),
            'tampered group': lambda s: observation(s)['browser_state']['profiles'][0]['windows'][0]['groups'][0].update(color='red'),
            'recipe edit': lambda s: s['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0].update(url='https://example.test/edit'),
            'partial browser': lambda s: observation(s)['browser_state'].update(errors=['missing profile']),
        }
        for label, mutate in cases.items():
            with self.subTest(label=label):
                snapshot, native = fixture()
                mutate(snapshot)
                self.assertRejected(snapshot, native)
                with patch.object(cli, 'ensure_browser_profiles') as launch, \
                        patch.object(cli, 'restore_browser') as restore, \
                        patch.object(cli, 'request_browser') as request, \
                        patch.object(cli, 'save') as save:
                    with self.assertRaises(reconciliation.ReconciliationRequired):
                        cli._restore_browsers(snapshot, Namespace(workspace=None, dry_run=False,
                            no_place=False, login_status=False), start_browser=True)
                    launch.assert_not_called()
                    restore.assert_not_called()
                    request.assert_not_called()
                    save.assert_not_called()

    def test_real_capture_coordinator_emits_one_new_transaction_per_capture(self):
        previous, native = fixture()
        previous['browsers']['google_chrome'].pop('latest_observation')
        observed = deepcopy(fixture()[0]['browsers']['google_chrome']['latest_observation']['browser_state'])
        terminals = {'version': 5, 'created_at': '2026-06-01T09:00:02+00:00',
                     'sessions': [], 'terminals': [], 'desktop': {'shell_companion': True}}
        shell = {'available': True, 'monitors': [], 'workspaces': []}
        times = [datetime.fromisoformat(value) for value in (
            '2026-06-01T09:00:00.125+00:00', '2026-06-01T09:00:04.500+00:00',
            '2026-06-01T09:00:00.125+00:00', '2026-06-01T09:00:04.500+00:00')]
        with patch.object(checkpoint, 'capture_shell', return_value=shell), \
                patch.object(checkpoint, 'workspace_names', return_value=['Synthetic']), \
                patch.object(checkpoint, 'datetime') as clock, \
                patch.object(cli, 'capture', side_effect=lambda **kw: deepcopy(terminals)), \
                patch.object(cli, 'capture_browser', side_effect=lambda **kw: deepcopy(observed)), \
                patch.object(cli, 'capture_social_apps', return_value={}), \
                patch.object(cli, 'capture_file_manager', return_value={'windows': []}), \
                patch.object(cli, 'capture_vscode', return_value={'windows': []}), \
                patch.object(cli, '_startup_marker', return_value=Path('/nonexistent/synthetic-marker')), \
                patch.object(cli, 'read_stage_marker', return_value=None), \
                patch.object(cli, '_checkpoint_login', return_value=None):
            clock.now.side_effect = times
            first, second = cli._capture_all(), cli._capture_all()
            for snapshot in (first, second):
                self.assertEqual(snapshot['capture_context']['schema_version'], 2)
                self.assertEqual(snapshot['capture_context']['snapshot_created_at'], snapshot['created_at'])
                cli._retain_unrestored_recipes(snapshot, previous)
                reconciliation.reconcile(snapshot, native)
        self.assertNotEqual(first['capture_context']['capture_id'], second['capture_context']['capture_id'])
        observation(second).update(deepcopy(observation(first)))
        self.assertRejected(second, native)


if __name__ == '__main__':
    unittest.main()
