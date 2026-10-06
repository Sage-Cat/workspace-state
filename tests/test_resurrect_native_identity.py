import unittest
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
