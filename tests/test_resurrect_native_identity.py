import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import resurrect


class ResurrectNativeIdentityTests(unittest.TestCase):
    def test_exact_current_pane_supplies_native_identity_to_restore_token(self):
        identity = '11111111-1111-4111-8111-111111111111'
        with patch.object(resurrect, 'run', return_value='%7\t123\t/work\n'), \
                patch.object(resurrect, 'codex_for_pane', return_value={'session_id': identity}) as capture:
            token = resurrect.codex_resume_token('work', '1', '0')
        capture.assert_called_once_with(123, '/work', pane_id='%7')
        self.assertEqual(token, 'wsctl-codex ' + identity)

    def test_unresolved_native_identity_cannot_launch_a_new_conversation(self):
        with patch.object(resurrect, 'run', return_value='%7\t123\t/work\n'), \
                patch.object(resurrect, 'codex_for_pane', return_value={'session_id': None}):
            self.assertIsNone(resurrect.codex_resume_token('work', '1', '0'))

    def test_unknown_live_pane_cannot_inherit_same_position_recipe_identity(self):
        identity = '11111111-1111-4111-8111-111111111111'
        recipe = {'sessions': [{'name': 'work', 'windows': [{
            'index': 1, 'name': 'review', 'panes': [{
                'index': 0, 'cwd': '/work', 'codex': {'session_id': identity},
            }],
        }]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.txt'
            original = (
                'pane\twork\t1\t1\t:*\t0\ttitle\t:/work\t1\tcodex\t:/usr/bin/codex\n'
                'window\twork\t1\t:review\t1\t:*\tlayout\toff\n'
            )
            path.write_text(original)
            with patch.object(resurrect, 'codex_resume_token', return_value=None):
                self.assertEqual(resurrect.annotate_state_file(path, recipe), {
                    'annotated': 0, 'unresolved': 1,
                })
            self.assertNotIn(identity, path.read_text())
            self.assertTrue(path.read_text().splitlines()[0].endswith('\t:'))

            path.write_text(original)
            with patch.object(resurrect, 'codex_resume_token') as live:
                self.assertEqual(resurrect.annotate_state_file(path, recipe, recipe_only=True), {
                    'annotated': 1, 'unresolved': 0,
                })
            live.assert_not_called()
            self.assertIn('wsctl-codex ' + identity, path.read_text())
