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
    def test_same_named_bootstrap_reaches_independent_canonical_restore(self):
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
            if verb not in {'new-session', 'set-option'}:
                self.fail(f'unexpected tmux mutation: {command!r}')
            return ''

        def canonical_restore(value, args, *, startup):
            self.assertTrue(startup)
            self.assertFalse(args.repair_processes)
            self.assertFalse(args.adopt_restored)
            restored.append(restore.recreate_tmux(
                value['sessions'][0], repair_processes=args.repair_processes,
                adopt_restored=args.adopt_restored,
            ))
            return {'terminals': 1, 'browsers': 0}

        # Start with both unsafe flags true to prove fallback replaces them.
        args = Namespace(category='terminals', workspace=None, session=None, select=False,
                         dry_run=False, no_place=False, force=False, wait=0,
                         repair_processes=True, adopt_restored=True, await_tmux=True,
                         verify_codex=True)
        listed = subprocess.CompletedProcess([], 0, 'main\n', '')
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}))
            stack.enter_context(patch.object(operations, 'current', return_value=None))
            stack.enter_context(patch.object(cli, '_boot_id', return_value='synthetic-boot'))
            stack.enter_context(patch.object(cli.subprocess, 'run', return_value=listed))
            stack.enter_context(patch.object(cli, 'load', return_value=snapshot))
            wait = stack.enter_context(patch.object(cli, '_wait_for_tmux_restore', return_value=False))
            stack.enter_context(patch.object(cli, '_wait_for_shell'))
            dispatch = stack.enter_context(patch.object(cli, '_restore', side_effect=canonical_restore))
            stack.enter_context(patch.object(restore, '_tmux_state', return_value=bootstrap))
            identity = stack.enter_context(patch.object(restore, 'codex_for_pane', return_value=None))
            stack.enter_context(patch.object(restore, '_tmux_exists', side_effect=lambda name: name == 'main'))
            stack.enter_context(patch.object(restore, 'run', side_effect=execute))

            self.assertFalse(cli._saved_tmux_sessions_are_live(snapshot))
            self.assertEqual(cli.cmd_startup(args), 0)

        wait.assert_called_once_with(0, await_start=True)
        dispatch.assert_called_once()
        identity.assert_any_call(100, '/synthetic/work', pane_id='%1')
        self.assertEqual(restored[0][0], 'main-wsctl')
        creation = next(command for command in commands if command[1] == 'new-session')
        self.assertEqual(creation[creation.index('-s') + 1], 'main-wsctl')
        self.assertIn('wsctl-codex-resume ' + UUID, creation[-1])
        self.assertEqual(bootstrap, original)
        for command in commands:
            self.assertNotIn(command[1], {'send-keys', 'respawn-pane', 'respawn-window',
                                         'kill-pane', 'kill-window', 'kill-session'})
            if command[1] == 'set-option':
                self.assertIn(command[command.index('-t') + 1], {'=main-wsctl:', '@10', '%10'})

    def test_multiple_existing_saved_names_require_conversation_evidence(self):
        listed = subprocess.CompletedProcess([], 0, 'main\nwork\nextra\n', '')
        for saved_ids in (False, True):
            with self.subTest(saved_ids=saved_ids):
                snapshot = {'sessions': [saved_session(name) for name in ('main', 'work')]}
                if not saved_ids:
                    for session in snapshot['sessions']:
                        session['windows'][0]['panes'][0].pop('codex')
                with patch.object(cli.subprocess, 'run', return_value=listed), \
                        patch.object(restore, '_tmux_state', return_value=bootstrap_state()), \
                        patch.object(restore, 'codex_for_pane', return_value=None):
                    self.assertFalse(cli._saved_tmux_sessions_are_live(snapshot))


if __name__ == '__main__':
    unittest.main()
