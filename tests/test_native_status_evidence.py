from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_codex_status as status_fixture
from workspace_state import capture, codex_status, native_status_evidence as evidence


class NativeStatusEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = status_fixture.CodexStatusTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.runtime = Path(self.temporary.name)
        (self.runtime / 'login-generation').write_text('abcdef1234')
        self.scope = {'boot_id': '33333333-3333-4333-8333-333333333333', 'login_generation': 'abcdef1234'}
        self.fixture.stack.enter_context(patch.object(evidence, 'runtime_root', return_value=self.runtime))
        self.boot = self.fixture.stack.enter_context(patch.object(evidence.operations, 'boot_id', return_value=self.scope['boot_id']))
        self.proof = self.fixture.proof()

    def remember(self):
        evidence.remember([self.proof], self.scope)

    def passive(self):
        return evidence.passive_identity(456, '%987')

    @property
    def path(self):
        return self.runtime / 'native-status-evidence/456.json'

    def test_exact_view_reuses_real_proof_without_input_or_account_storage(self):
        self.remember()
        self.fixture.sent.clear()
        self.assertEqual(self.passive(), {
            'pid': 456, 'session_id': status_fixture.SESSION, 'confidence': 'native-status-view',
            'start_ticks': '10', 'tty': '/dev/pts/7'})
        self.assertEqual(self.fixture.sent, [])
        raw = self.path.read_text()
        self.assertNotIn('Server:', raw)
        self.assertNotIn('Model:', raw)
        self.assertNotIn('/work', raw)
        self.assertNotIn('Token usage:', raw)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_capture_fallback_uses_the_exact_pane_and_keeps_process_identity(self):
        self.remember()
        with patch('workspace_state.codex_title.native_title_proof', return_value=None), \
                patch('workspace_state.codex_title._pane_title', return_value=None):
            value = capture.codex_for_pane(123, '/work', proc_root=self.fixture.proc, pane_id='%987')
        self.assertEqual(value, self.passive())
        self.assertEqual(capture.codex_for_pane(123, '/work', proc_root=self.fixture.proc), {
            'pid': 456, 'session_id': None, 'confidence': 'unknown', 'start_ticks': '10', 'tty': '/dev/pts/7'})

    def test_any_view_input_geometry_process_or_login_change_withdraws_evidence(self):
        changes = ('output', 'input', 'title', 'width', 'height', 'cursor', 'copy',
                   'pid', 'parent', 'argv', 'tty', 'foreground', 'boot', 'login', 'catalog')
        for change in changes:
            with self.subTest(change=change):
                # Reset each isolated fixture rather than relying on old proofs.
                case = NativeStatusEvidenceTests()
                case.setUp()
                try:
                    case.remember()
                    f = case.fixture
                    if change == 'output': f.log.insert(0, 'new output'); f._screen()
                    elif change == 'input': f._screen('› draft')
                    elif change == 'title': f.title = 'changed'
                    elif change == 'width': f.width -= 1
                    elif change == 'height': f.height -= 1
                    elif change == 'cursor': f.column += 1
                    elif change == 'copy': f.mode = '1'
                    elif change == 'pid': f._process(456, ticks=11)
                    elif change == 'parent': f._process(123, comm='zsh', argv=b'zsh\0', group=123, ticks=11)
                    elif change == 'argv': f._process(456, argv=b'codex\0--no-alt-screen\0--foo\0')
                    elif change == 'tty': f._process(456, tty='/dev/pts/8')
                    elif change == 'foreground': f._process(456, foreground=123)
                    elif change == 'boot': case.boot.return_value = status_fixture.OTHER
                    elif change == 'login': (case.runtime / 'login-generation').write_text('1234')
                    elif change == 'catalog': f.loaded.return_value = {status_fixture.OTHER}
                    self.assertIsNone(case.passive())
                finally:
                    case.doCleanups()

    def test_new_thread_same_pid_and_old_report_in_scrollback_is_not_reused(self):
        self.remember()
        self.fixture.log += ['', 'New conversation', '', *status_fixture.block(status_fixture.OTHER)]
        self.fixture.loaded.return_value = {status_fixture.SESSION, status_fixture.OTHER}
        self.fixture._screen()
        self.assertIsNone(self.passive())

    def test_wrong_pane_record_or_unsafe_file_refuses(self):
        self.remember()
        self.assertIsNone(evidence.passive_identity(456, '%988'))
        original = self.path.read_text()
        for change in ('schema', 'uuid', 'fingerprint', 'digest', 'pid', 'pane', 'json', 'large', 'public'):
            with self.subTest(change=change):
                self.path.chmod(0o600)
                data = json.loads(original)
                if change == 'schema': data['schema_version'] = 2
                elif change == 'uuid': data['session_id'] = status_fixture.OTHER
                elif change == 'fingerprint': data['fingerprint'] = '0' * 64
                elif change == 'digest': data['status_block_digest'] = '0' * 64
                elif change == 'pid': data['pid'] = 457
                elif change == 'pane': data['pane_id'] = '%988'
                self.path.write_text('{broken' if change == 'json' else 'x' * 4097 if change == 'large' else json.dumps(data))
                if change == 'public': self.path.chmod(0o644)
                self.assertIsNone(self.passive())
        self.path.unlink()
        target = self.runtime / 'outside'; target.write_text(original); target.chmod(0o600)
        self.path.symlink_to(target)
        self.assertIsNone(self.passive())

    def test_missing_private_evidence_never_sends_input_or_creates_files(self):
        self.fixture.sent.clear()
        self.assertIsNone(self.passive())
        self.assertFalse(self.path.parent.exists())
        self.assertEqual(self.fixture.sent, [])

    def test_stale_proof_or_wrong_login_cannot_be_remembered(self):
        with self.assertRaisesRegex(RuntimeError, 'login changed'):
            evidence.remember([self.proof], None)
        self.fixture._screen('› draft')
        with self.assertRaisesRegex(RuntimeError, 'evidence changed'):
            self.remember()
        self.assertFalse(self.path.exists())

    def test_view_changed_during_passive_catalog_validation_refuses(self):
        self.remember()
        def changed(*args, **kwargs):
            self.fixture._screen('› draft')
            return {status_fixture.SESSION}
        self.fixture.loaded.side_effect = changed
        self.assertIsNone(self.passive())

    def test_scope_changed_during_late_publication_refuses(self):
        original = evidence._scope
        count = 0
        def scope():
            nonlocal count
            count += 1
            return original() if count == 1 else None
        with patch.object(evidence, '_scope', side_effect=scope), self.assertRaisesRegex(RuntimeError, 'login changed'):
            self.remember()
        self.assertFalse(self.path.exists())

    def test_explicit_height_restore_retains_complete_unchanged_status_only(self):
        self.fixture.height = 37
        self.assertFalse(codex_status.revalidate(self.proof))
        renewed = evidence.after_owned_height_restore(self.proof, 37)
        self.assertTrue(codex_status.revalidate(renewed))
        evidence.remember([renewed], self.scope)
        self.assertEqual(self.passive()['session_id'], self.proof.session_id)

    def test_height_restore_cannot_accept_other_changes_or_hidden_report(self):
        for change in ('width', 'title', 'output', 'input', 'owner', 'hidden', 'expected'):
            with self.subTest(change=change):
                case = NativeStatusEvidenceTests()
                case.setUp()
                try:
                    f = case.fixture
                    f.height = 37
                    expected = 37
                    if change == 'width': f.width -= 1
                    elif change == 'title': f.title = 'changed'
                    elif change == 'output': f.log += ['new output']; f._screen()
                    elif change == 'input': f._screen('› draft')
                    elif change == 'owner': f._process(456, ticks=11)
                    elif change == 'hidden': f.log = f.log[-4:]; f._screen()
                    elif change == 'expected': expected = 38
                    with self.assertRaises(codex_status.StatusRefused):
                        evidence.after_owned_height_restore(case.proof, expected)
                finally:
                    case.doCleanups()
