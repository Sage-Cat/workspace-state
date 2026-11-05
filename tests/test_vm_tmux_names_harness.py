import importlib.util
import json
from contextlib import ExitStack
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace


HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_tmux_names_harness', HERE / 'run_vm_tmux_names.py')
names = importlib.util.module_from_spec(spec)
spec.loader.exec_module(names)


NEW_AGENT_FACTS = {'logind': {'inhibit_delay_max_usec': 20_000_000, 'inhibitors': [
    {'line': 'workspace-state 1000 u 5 py shutdown final save delay', 'mode': 'delay'}]}, 'shutdown_files': []}
OLD_AGENT_FACTS = {'logind': {'inhibit_delay_max_usec': 20_000_000, 'inhibitors': [
    {'line': 'workspace-state 1000 u 5 py shutdown transaction block', 'mode': 'block'}]}, 'shutdown_files': []}


class NamingObserverTests(unittest.TestCase):
    def test_candidate_preparation_refuses_an_agent_with_a_block_lock_before_capture(self):
        release = 'r-' + 'a' * 24
        args = Namespace(expected_release=release, new_run=False, session='main', donor_session='scale-02')
        status = {'operation_state': 'completed'}
        for phase in (names.prepare, names.prepare_continuity):
            with self.subTest(phase=phase.__name__), tempfile.TemporaryDirectory() as directory, \
                 patch.object(names, 'guard'), patch.object(names.p, 'ROOT', Path(directory)), \
                 patch.object(names, 'settled_status', return_value=status), \
                 patch.object(names, 'hud_bytes', return_value={}), \
                 patch('workspace_state.storage.load', return_value={}), \
                 patch.object(names.p, 'cycle_facts', return_value=OLD_AGENT_FACTS), \
                 patch.object(names.p, 'capture') as capture, patch.object(names.p.f, 'verify_running_companions'), \
                 patch.object(names, 'release_id', return_value=release), \
                 self.assertRaisesRegex(RuntimeError, 'blocking inhibitor'):
                phase(args, release)
            capture.assert_not_called()

    def test_default_timeout_matches_the_poweroff_watcher(self):
        with patch.object(sys, 'argv', ['run_vm_tmux_names.py', '--disposable-guest', 'watch']), \
             patch.object(names.p, 'guest_environment', return_value='/release'), \
             patch.object(names, 'guard'), patch.object(names.p, 'run_directory', return_value=Path('/x')), \
             patch.object(names.p, 'read', return_value={'scenario': names.SCENARIO}), \
             patch.object(names, 'supersede_watch_failure'), patch.object(names.p, 'watch') as watch:
            names.main()
        self.assertEqual(watch.call_args.args[0].timeout, names.p.DEFAULT_WATCH_TIMEOUT)
        self.assertEqual(names.p.DEFAULT_WATCH_TIMEOUT, 900)

    def test_expired_watcher_failure_is_visible_to_verify_under_either_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(names.watcher_failures(directory), [])
            for filename in names.WATCH_FAILURE_FILES:
                (directory / filename).write_text(json.dumps({'error': filename}))
            self.assertEqual([item['error'] for item in names.watcher_failures(directory)],
                             ['failure-watch.json', 'names-failure-watch.json'])

    def test_watcher_expiry_written_by_main_is_found_and_a_new_watch_supersedes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'before.json').write_text(json.dumps({'scenario': names.SCENARIO}))
            with patch.object(sys, 'argv', ['run_vm_tmux_names.py', '--disposable-guest', 'watch']), \
                 patch.object(names.p, 'guest_environment', return_value='/release'), patch.object(names, 'guard'), \
                 patch.object(names.p, 'ROOT', directory), patch.object(names.p, 'run_directory', return_value=directory), \
                 patch.object(names.p, 'watch', side_effect=RuntimeError('Watch expired without VM exit')):
                (directory / 'active.json').write_text('{}')
                with self.assertRaisesRegex(RuntimeError, 'Watch expired'):
                    names.main()
            failures = names.watcher_failures(directory)
            self.assertEqual(len(failures), 1)
            self.assertIn('Watch expired', failures[0]['error'])
            names.supersede_watch_failure(directory)
            self.assertEqual(names.watcher_failures(directory), [])
            self.assertEqual(len(list(directory.glob('superseded-names-failure-watch-*.json'))), 1)

    def test_continuity_refuses_failed_startup_before_observing_or_writing_a_baseline(self):
        release = 'r-' + 'a' * 24
        args = Namespace(expected_release=release, new_run=False)
        with tempfile.TemporaryDirectory() as directory, patch.object(names, 'guard'), patch.object(
            names.p, 'ROOT', Path(directory)
        ), patch.object(names, 'settled_status', return_value={'operation_state': 'failed'}), patch.object(
            names.p, 'capture'
        ) as capture, patch.object(names.p, 'write') as write, self.assertRaisesRegex(RuntimeError, 'successful'):
            names.prepare_continuity(args, release)
        capture.assert_not_called()
        write.assert_not_called()

    def test_continuity_is_read_only_and_binds_independent_native_names(self):
        release = 'r-' + 'a' * 24
        args = Namespace(expected_release=release, new_run=False, session='main', donor_session='scale-02')
        panes = [{'name': name, 'windows': [{'panes': [{}] * count}]} for name, count in [('main', 5), ('scale-02', 1)]]
        status = {'operation_state': 'completed', 'operation_context': {'login_generation': 'synthetic-generation'}}
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.object(names.p, 'ROOT', Path(directory)))
            for owner, key, value in (
                (names, 'guard', None), (names, 'settled_status', status),
                (names, 'hud_bytes', {'synthetic-marker': 'unchanged'}),
                (names.p, 'capture', {'synthetic': 'snapshot'}), (names.p.f, 'shell', {'windows': []}),
                (names, 'native_tmux', panes), (names.p, 'read', []),
                (names.p, 'require_fixture_inventory', None),
                (names, 'snapshot_intent', 'independent'), (names, 'intent', 'independent'),
                (names.p.f, 'verify_running_companions', {}), (names.p.f, 'observe_chrome', {}),
                (names.p.f, 'wayland_login', 'synthetic-login'), (names.p, 'native_inventory', {}),
                (names.p, 'boot', 'synthetic-boot'), (names.p, 'cycle_facts', NEW_AGENT_FACTS),
            ):
                stack.enter_context(patch.object(owner, key, return_value=value))
            mutate = stack.enter_context(patch.object(names, 'tmux'))
            command = stack.enter_context(patch.object(names.p.f, 'run'))
            stack.enter_context(patch('workspace_state.storage.load', return_value={'synthetic': 'canonical'}))
            result = names.prepare_continuity(args, release)
            evidence = json.loads((Path(result['run_directory']) / 'before.json').read_text())
        self.assertFalse(result['manual_save_called'])
        self.assertFalse(evidence['fixture_mutation'])
        self.assertEqual(evidence['preparation'], 'observer-only-continuity')
        mutate.assert_not_called()
        command.assert_not_called()

    def test_unproven_continuity_cannot_be_relabelled_as_a_valid_cycle(self):
        for flags in (
            {'manual_save_called': True, 'fixture_mutation': False, 'prior_operation_state': 'completed'},
            {'manual_save_called': False, 'fixture_mutation': True, 'prior_operation_state': 'completed'},
            {'manual_save_called': False, 'fixture_mutation': False, 'prior_operation_state': 'failed'},
        ):
            with self.subTest(flags=flags), patch.object(names, 'guard'), patch.object(
                names.p, 'run_directory', return_value=Path('/synthetic-observer')
            ), patch.object(names.p, 'read', return_value={
                'scenario': names.SCENARIO, 'preparation': 'observer-only-continuity',
                'hud_failures_reset': False, **flags
            }), self.assertRaisesRegex(RuntimeError, 'prepared production naming cycle'):
                names.verify('r-' + 'a' * 24)


if __name__ == '__main__':
    unittest.main()
