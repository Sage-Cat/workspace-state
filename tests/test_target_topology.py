import unittest
from unittest.mock import patch

from workspace_state import desktop, file_manager, social_apps, vscode
from workspace_state.util import CommandError


class TargetTopologyTests(unittest.TestCase):
    def setUp(self):
        self.shell = {'available': True, 'workspaces': [{'index': 0, 'name': 'work'}],
            'monitors': [{'index': 2, 'x': 100, 'y': 0, 'width': 800, 'height': 600,
                          'identity': {'connector': 'TEST-1'}, 'primary': True}]}
        self.saved = {'workspace': 4, 'workspace_name': 'work', 'monitor': 7,
                      'monitor_identity': {'connector': 'TEST-1'}}

    def test_each_provider_resolves_workspace_and_monitor_from_one_observation(self):
        for provider in (file_manager, social_apps, vscode):
            with self.subTest(provider=provider.__name__), patch.object(
                provider, 'capture_shell', return_value=self.shell,
            ) as capture, patch.object(desktop, 'capture_shell', side_effect=AssertionError('extra topology read')):
                target = provider._target(self.saved)
            capture.assert_called_once_with()
            self.assertEqual((target['workspace'], target['monitor']), (0, 2))
        self.assertEqual((self.saved['workspace'], self.saved['monitor']), (4, 7))

    def test_missing_display_and_ambiguous_workspace_still_refuse(self):
        for shell in ({**self.shell, 'monitors': []},
                      {**self.shell, 'workspaces': [{'name': 'work'}, {'name': 'work'}]}):
            with self.subTest(shell=shell), self.assertRaises(CommandError):
                desktop.resolve_placement_target(self.saved, provider='test', shell=shell)

    def test_target_never_silently_switches_to_another_physical_display(self):
        with self.assertRaisesRegex(CommandError, 'not connected'):
            desktop.resolve_placement_target({**self.saved, 'monitor_identity': {'connector': 'MISSING'}},
                                             provider='test', shell=self.shell)


if __name__ == '__main__':
    unittest.main()
