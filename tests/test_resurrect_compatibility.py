import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import resurrect


UUIDS = [f'{index:08d}-1111-4111-8111-111111111111' for index in range(1, 6)]


def recipe(names=('current-review', 'current-task')):
    return {'version': 5, 'sessions': [{'name': 'main', 'windows': [{
        'index': index, 'name': name, 'layout': 'synthetic-layout', 'panes': [{
            'index': 0, 'cwd': '/work/' + name, 'command': 'codex',
            'codex': {'session_id': UUIDS[index]},
        }],
    } for index, name in enumerate(names)]}], 'terminals': []}


def artifact(value):
    lines = []
    for session in value['sessions']:
        for window in session['windows']:
            for pane in window['panes']:
                identity = (pane.get('codex') or {}).get('session_id')
                command = 'wsctl-codex ' + identity if identity else ''
                lines.append('\t'.join([
                    'pane', session['name'], str(window['index']), '1', ':*',
                    str(pane['index']), 'volatile title', ':' + pane['cwd'].replace(' ', r'\ '),
                    '1', pane['command'], ':' + command,
                ]))
            lines.append('\t'.join([
                'window', session['name'], str(window['index']), ':' + window['name'],
                '1', ':*', window['layout'], 'off',
            ]))
    return '\n'.join([*lines, 'state\tmain\t']) + '\n'


class ResurrectCompatibilityTests(unittest.TestCase):
    def matches(self, text, canonical):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.txt'
            path.write_text(text)
            before = path.read_bytes()
            try:
                return resurrect.state_matches_recipe(path, canonical)
            finally:
                self.assertEqual(path.read_bytes(), before)

    def test_matching_artifact_and_escaped_cwd_are_compatible_without_live_calls(self):
        canonical = recipe()
        canonical['sessions'][0]['windows'][0]['panes'][0]['cwd'] = '/work/a directory'
        with patch.object(resurrect, 'run', side_effect=AssertionError('must not query live panes')):
            self.assertTrue(self.matches(artifact(canonical), canonical))

    def test_old_four_windows_cannot_precede_current_two_windows(self):
        self.assertFalse(self.matches(artifact(recipe(('old-one', 'old-two', 'old-three', 'old-four'))), recipe()))

    def test_same_position_name_and_cwd_with_different_uuid_is_incompatible(self):
        canonical = recipe()
        stale = copy.deepcopy(canonical)
        stale['sessions'][0]['windows'][0]['panes'][0]['codex']['session_id'] = UUIDS[4]
        self.assertFalse(self.matches(artifact(stale), canonical))

    def test_shell_pane_matches_but_cannot_replay_a_saved_codex(self):
        canonical = recipe()
        stale = copy.deepcopy(canonical)
        pane = canonical['sessions'][0]['windows'][0]['panes'][0]
        pane.pop('codex')
        pane['command'] = 'zsh'
        self.assertTrue(self.matches(artifact(canonical), canonical))
        self.assertFalse(self.matches(artifact(stale), canonical))

    def test_extra_session_or_pane_and_changed_name_or_cwd_are_incompatible(self):
        canonical = recipe()
        variants = []
        value = copy.deepcopy(canonical)
        value['sessions'].append({'name': 'obsolete', 'windows': copy.deepcopy(value['sessions'][0]['windows'])})
        variants.append(value)
        for field, replacement in [('name', 'obsolete-name'), ('cwd', '/obsolete')]:
            value = copy.deepcopy(canonical)
            window = value['sessions'][0]['windows'][0]
            (window if field == 'name' else window['panes'][0])[field] = replacement
            variants.append(value)
        value = copy.deepcopy(canonical)
        pane = copy.deepcopy(value['sessions'][0]['windows'][0]['panes'][0])
        pane['index'] = 1
        value['sessions'][0]['windows'][0]['panes'].append(pane)
        variants.append(value)
        for stale in variants:
            with self.subTest(stale=stale):
                self.assertFalse(self.matches(artifact(stale), canonical))

    def test_unknown_codex_and_malformed_or_unsupported_records_fail_closed(self):
        canonical = recipe()
        exact = artifact(canonical)
        for text in [
            exact.replace(':wsctl-codex ' + UUIDS[0], ':'),
            exact.replace(':wsctl-codex ' + UUIDS[0], ':codex resume ' + UUIDS[0]),
            exact + 'grouped_session\tother\tmain\n',
            exact + exact.splitlines()[0] + '\n',
            'pane\tmain\n',
        ]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.matches(text, canonical)
        self.assertFalse(self.matches('', canonical))


class RestoreWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'package/bin'
        self.bin.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        (self.bin.parent / 'src').symlink_to(project / 'src', target_is_directory=True)
        for name in ('wsctl-continuum-restore', 'wsctl-python'):
            shutil.copy2(project / 'bin' / name, self.bin / name)
        self.log = self.root / 'calls.log'
        (self.bin / 'wsctl').write_text('#!/bin/sh\nprintf "wsctl %s\\n" "$*" >> "$TEST_CALLS"\n')
        (self.bin / 'wsctl').chmod(0o755)
        plugin = self.root / 'plugins/tmux-resurrect/scripts'
        plugin.mkdir(parents=True)
        (plugin / 'restore.sh').write_text('#!/bin/sh\nprintf "plugin\\n" >> "$TEST_CALLS"\n')
        (plugin / 'restore.sh').chmod(0o755)
        self.tmux = self.root / 'tmux'
        self.tmux.mkdir()
        (self.bin / 'tmux').write_text(
            '#!/bin/sh\nprintf "tmux %s\\n" "$*" >> "$TEST_CALLS"\nprintf "%s\\n" "$TEST_RESURRECT_DIR"\n'
        )
        (self.bin / 'tmux').chmod(0o755)
        self.canonical = self.root / 'data/workspace-state/snapshots/current.json'
        self.canonical.parent.mkdir(parents=True)
        self.canonical.write_text(json.dumps(recipe()))
        self.state = self.tmux / 'saved.txt'
        (self.tmux / 'last').symlink_to(self.state.name)
        self.environment = {
            **os.environ, 'HOME': str(self.root / 'home'), 'XDG_DATA_HOME': str(self.root / 'data'),
            'TEST_CALLS': str(self.log), 'TEST_RESURRECT_DIR': str(self.tmux),
            'TMUX_PLUGIN_MANAGER_PATH': str(self.root / 'plugins'),
        }

    def restore(self):
        before = {path: path.read_bytes() for path in (self.canonical, self.state) if path.exists()}
        result = subprocess.run([self.bin / 'wsctl-continuum-restore'], env=self.environment,
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        calls = self.log.read_text().splitlines() if self.log.exists() else []
        # All tmux access made by the guard is a read-only option lookup.
        self.assertTrue(all(call == 'tmux show-option -gqv @resurrect-dir'
                            for call in calls if call.startswith('tmux ')))
        return result, calls

    def test_exact_matching_native_state_reaches_plugin_after_guard(self):
        self.state.write_text(artifact(recipe()))
        result, calls = self.restore()
        self.assertEqual(calls[0], 'tmux show-option -gqv @resurrect-dir')
        self.assertTrue(calls[1].startswith('wsctl tmux begin '))
        self.assertEqual(calls[2:], ['plugin', 'wsctl tmux end'])
        self.assertEqual(result.stderr, '')

    def test_old_four_and_same_position_different_uuid_skip_plugin_and_markers(self):
        stale = recipe()
        stale['sessions'][0]['windows'][0]['panes'][0]['codex']['session_id'] = UUIDS[4]
        for value in (recipe(('old-one', 'old-two', 'old-three', 'old-four')), stale):
            with self.subTest(value=value):
                self.log.unlink(missing_ok=True)
                self.state.write_text(artifact(value))
                result, calls = self.restore()
                self.assertEqual(calls, ['tmux show-option -gqv @resurrect-dir'])
                self.assertIn('canonical startup fallback', result.stderr)

    def test_missing_first_run_and_corrupt_evidence_skip_without_mutation(self):
        for scenario in ('missing-native', 'corrupt-native', 'missing-canonical', 'corrupt-canonical'):
            with self.subTest(scenario=scenario):
                self.log.unlink(missing_ok=True)
                self.state.write_text(artifact(recipe()))
                self.canonical.write_text(json.dumps(recipe()))
                if scenario == 'missing-native':
                    self.state.unlink()
                elif scenario == 'corrupt-native':
                    self.state.write_text('pane\tbroken\n')
                elif scenario == 'missing-canonical':
                    self.canonical.unlink()
                else:
                    self.canonical.write_text('{')
                result, calls = self.restore()
                self.assertNotIn('plugin', calls)
                self.assertFalse(any(call.startswith('wsctl ') for call in calls))
                self.assertIn('canonical startup fallback', result.stderr)

    def test_configured_directory_expands_home_and_hostname(self):
        expanded = self.root / 'home' / 'saved-host'
        expanded.mkdir(parents=True)
        (expanded / 'last').write_text(artifact(recipe()))
        with patch.object(resurrect, 'run', return_value='$HOME/saved-$HOSTNAME\n') as command, \
                patch.object(resurrect.socket, 'gethostname', return_value='host'), \
                patch.dict(os.environ, self.environment):
            self.assertTrue(resurrect.native_restore_matches_recipe())
        command.assert_called_once_with(['tmux', 'show-option', '-gqv', '@resurrect-dir'], timeout=2)

    def test_default_directory_follows_legacy_then_xdg_plugin_convention(self):
        home = self.root / 'home'
        for directory in (self.root / 'data/tmux/resurrect', home / '.tmux/resurrect'):
            with self.subTest(directory=directory):
                directory.mkdir(parents=True)
                (directory / 'last').write_text(artifact(recipe()))
                with patch.object(resurrect, 'run', return_value='\n'), \
                        patch.dict(os.environ, self.environment):
                    self.assertTrue(resurrect.native_restore_matches_recipe())


if __name__ == '__main__':
    unittest.main()
