from __future__ import annotations

import argparse
import copy
from contextlib import ExitStack, redirect_stdout
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from workspace_state import checkpoint, cli, codex_status, shutdown_checkpoint_guard


class ManualCodexStatusTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(cli, 'state_lock'))
        self.guard = self.stack.enter_context(patch.object(shutdown_checkpoint_guard, 'check_manual_save_allowed'))
        self.stack.enter_context(patch.object(shutdown_checkpoint_guard, 'clear_after_manual_save'))
        self.stack.enter_context(patch.object(cli, '_checkpoint_login', return_value={'boot_id': 'test', 'login_generation': 'test'}))
        self.stack.enter_context(patch.object(cli, 'load', return_value=None))
        self.stack.enter_context(patch.object(cli, '_arm_autosave'))
        self.stack.enter_context(patch.object(cli, 'configured_apps', return_value=[]))
        self.stack.enter_context(patch.object(checkpoint, 'verify_capture_context'))
        self.snapshot = {
            'created_at': '2026-01-01T00:00:00Z', 'desktop': {'shell_companion': True},
            'terminals': [], 'sessions': [{'name': 'work', 'windows': [{'index': 1, 'name': 'notes',
                'panes': [{'id': '%1', 'index': 1, 'codex': {
                    'pid': 101, 'start_ticks': '123', 'tty': '/dev/pts/2',
                    'session_id': None, 'confidence': 'unknown'}}]}]}],
            'browsers': {'google_chrome': {'available': True, 'profiles': []}},
        }
        self.capture = self.stack.enter_context(patch.object(cli, '_capture_all', side_effect=lambda: copy.deepcopy(self.snapshot)))
        self.publish = self.stack.enter_context(patch.object(cli, 'save', return_value=Path('/synthetic/current.json')))
        self.record = self.snapshot['sessions'][0]['windows'][0]['panes'][0]['codex'] | {
            'session_id': '11111111-1111-4111-8111-111111111111', 'confidence': 'native-status'}
        self.proof = Mock()
        self.proof.capture_record.side_effect = lambda: dict(self.record)
        self.probe = self.stack.enter_context(patch.object(codex_status, 'probe', return_value=self.proof))
        self.revalidate = self.stack.enter_context(patch.object(codex_status, 'revalidate', return_value=True))

    def save(self, verify=True, shutdown=False):
        return cli.cmd_save(argparse.Namespace(allow_partial=False, shutdown_safe=shutdown, verify_idle_codex=verify))

    def test_explicit_probe_supplies_identity_and_is_revalidated_before_publication(self):
        self.publish.side_effect = lambda value: self.assertEqual(self.revalidate.call_count, 1) or Path('/synthetic/current.json')
        self.assertEqual(self.save(), 0)
        self.probe.assert_called_once_with(101, '%1')
        self.revalidate.assert_called_once_with(self.proof)
        saved = self.publish.call_args.args[0]
        self.assertEqual(saved['sessions'][0]['windows'][0]['panes'][0]['codex'], self.record)
        self.assertEqual(self.snapshot['sessions'][0]['windows'][0]['panes'][0]['codex']['session_id'], None)

    def test_default_save_remains_read_only_and_refuses_unresolved_identity(self):
        with self.assertRaisesRegex(RuntimeError, 'session ID.*unresolved'):
            self.save(verify=False)
        self.probe.assert_not_called()
        self.publish.assert_not_called()

    def test_busy_or_draft_refusal_never_publishes(self):
        self.probe.side_effect = codex_status.StatusRefused('client has pending draft input')
        with self.assertRaisesRegex(RuntimeError, 'pending draft'):
            self.save()
        self.publish.assert_not_called()

    def test_changed_process_identity_after_capture_never_publishes(self):
        for key, value in [('pid', 202), ('start_ticks', '456'), ('tty', '/dev/pts/3')]:
            with self.subTest(key=key):
                original = self.record[key]
                self.record[key] = value
                with self.assertRaisesRegex(RuntimeError, 'client changed after capture'):
                    self.save()
                self.publish.assert_not_called()
                self.record[key] = original

    def test_changed_status_before_publication_never_publishes(self):
        self.revalidate.return_value = False
        with self.assertRaisesRegex(RuntimeError, 'status changed before publication'):
            self.save()
        self.publish.assert_not_called()

    def test_current_shutdown_guard_runs_before_any_capture_or_probe(self):
        self.guard.side_effect = ValueError('shutdown is still active')
        with self.assertRaisesRegex(ValueError, 'shutdown is still active'):
            self.save()
        self.capture.assert_not_called()
        self.probe.assert_not_called()
        self.publish.assert_not_called()

    def test_probe_cannot_be_used_in_shutdown_worker(self):
        with self.assertRaisesRegex(RuntimeError, 'explicit manual save'):
            self.save(shutdown=True)
        self.capture.assert_not_called()
        self.probe.assert_not_called()
        self.publish.assert_not_called()

    def test_conflicting_evidence_cannot_be_overridden_by_status(self):
        self.snapshot['sessions'][0]['windows'][0]['panes'][0]['codex']['confidence'] = 'conflicting-identities'
        with self.assertRaisesRegex(RuntimeError, 'conflicting conversation evidence'):
            self.save()
        self.probe.assert_not_called()
        self.publish.assert_not_called()

    def test_known_identity_never_receives_status_input(self):
        self.snapshot['sessions'][0]['windows'][0]['panes'][0]['codex'] = dict(self.record)
        self.assertEqual(self.save(), 0)
        self.probe.assert_not_called()
        self.revalidate.assert_not_called()
