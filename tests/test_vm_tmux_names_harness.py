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


class NamingObserverTests(unittest.TestCase):
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
                (names.p, 'graphical_launch_evidence', {}), (names.p, 'boot', 'synthetic-boot'),
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
