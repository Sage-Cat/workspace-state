from argparse import Namespace
from contextlib import ExitStack
from copy import deepcopy
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, operations, restore


UUID = '11111111-1111-4111-8111-111111111111'


def saved_session(name='main'):
    return {'name': name, 'windows': [{
        'index': 0, 'name': 'current-work', 'layout': 'synthetic-layout', 'panes': [{
            'index': 0, 'cwd': '/synthetic/work', 'command': 'codex',
            'codex': {'session_id': UUID},
        }],
    }]}


def bootstrap_state():
    return {0: {'id': '@1', 'name': 'current-work', 'panes': {
        0: {'id': '%1', 'pid': 100, 'cwd': '/synthetic/work', 'command': 'zsh'},
    }}}


class CanonicalTmuxStartupTests(unittest.TestCase):
    def test_same_named_live_session_needs_attention_without_a_copy(self):
        self.addCleanup(operations.bind, None)  # startup binds its operation context
        snapshot = {'sessions': [saved_session()], 'terminals': []}
        bootstrap = bootstrap_state()
        original = deepcopy(bootstrap)
        commands = []
        restored = []

        def execute(command):
            commands.append(command)
            verb = command[1]
            if verb == 'list-sessions':
                return 'main\n'
            if verb == 'show-options':
                return 'off\n' if command[-1] == 'automatic-rename' else ''
            if verb == 'display-message':
                return {
                    '#{window_id}': '@10\n', '#{window_index}': '0\n', '#{pane_id}': '%10\n',
                    '#{window_id}\n#{window_name}': '@10\ncurrent-work\n',
                }[command[-1]]
            if verb not in set():
                self.fail(f'unexpected tmux mutation: {command!r}')
            return ''

        def canonical_restore(value, args, *, startup):
            self.assertTrue(startup)
            for retired in ('repair_processes', 'adopt_restored'):
                self.assertFalse(hasattr(args, retired))
            with self.assertRaises(restore.SessionNeedsAttention) as raised:
                restore.recreate_tmux(value['sessions'][0])
            restored.append(str(raised.exception))
            return {'terminals': 0, 'browsers': 0}

        args = Namespace(category='terminals', workspace=None, session=None, select=False,
                         dry_run=False, no_place=False, force=False, wait=0,
                         verify_codex=True)
        listed = subprocess.CompletedProcess([], 0, 'main\n', '')
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}))
            stack.enter_context(patch.object(operations, 'current', return_value=None))
            stack.enter_context(patch.object(cli, '_boot_id', return_value='synthetic-boot'))
            stack.enter_context(patch.object(cli.subprocess, 'run', return_value=listed))
            stack.enter_context(patch.object(cli, 'load', return_value=snapshot))
            stack.enter_context(patch.object(cli, '_wait_for_shell'))
            dispatch = stack.enter_context(patch.object(cli, '_restore', side_effect=canonical_restore))
            stack.enter_context(patch.object(restore, '_tmux_state', return_value=bootstrap))
            identity = stack.enter_context(patch.object(restore, 'codex_for_pane', return_value=None))
            stack.enter_context(patch.object(restore, '_tmux_exists', side_effect=lambda name: name == 'main'))
            stack.enter_context(patch.object(restore, 'run', side_effect=execute))

            self.assertEqual(cli.cmd_startup(args), 0)

        dispatch.assert_called_once()
        identity.assert_any_call(100, '/synthetic/work', pane_id='%1')
        self.assertEqual(restored, ['tmux session main is in use with a different layout; '
                                    'its saved items stay pending'])
        self.assertEqual([command for command in commands if command[1] in {'new-session', 'set-option'}], [])
        self.assertEqual(bootstrap, original)
        for command in commands:
            self.assertNotIn(command[1], {'send-keys', 'respawn-pane', 'respawn-window',
                                         'kill-pane', 'kill-window', 'kill-session'})


if __name__ == '__main__':
    unittest.main()
