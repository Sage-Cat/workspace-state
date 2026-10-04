from __future__ import annotations

import importlib.util
import copy
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location(
    'vm_scale_protocol_test_module', Path(__file__).parent / 'integration/run_vm_scale.py')
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)


class SyntheticWarmupTests(unittest.TestCase):
    def fixture(self):
        pipe = object.__new__(fixture.ChromePipe)
        pipe.extension_id = 'fixture'
        pipe.observe = Mock(return_value=[{'tabs': [
            {'id': w * 6 + t, 'url': 'http://127.0.0.1:18765/scale/test',
             'pendingUrl': '', 'status': 'unloaded' if t == 0 else 'complete'}
            for t in range(6)]} for w in range(7)])
        pipe.call = Mock(side_effect=[{'targetInfos': [{'type': 'service_worker',
            'url': 'chrome-extension://fixture/worker.js', 'targetId': 'target'}]},
            {'sessionId': 'session'}, {'result': {}}])
        pipe.detach = Mock()
        return pipe

    def test_only_synthetic_unloaded_tabs_are_reloaded(self):
        pipe = self.fixture()
        result = pipe.warm_synthetic_tabs()
        self.assertEqual(result['reloaded'], list(range(0, 42, 6)))
        expression = pipe.call.call_args_list[2].args[1]['expression']
        self.assertIn('chrome.tabs.reload', expression)
        self.assertNotIn('create', expression)
        self.assertNotIn('remove', expression)
        pipe.detach.assert_called_once_with('session')

    def test_real_or_pending_content_refused_without_protocol_mutation(self):
        for key, value in [('url', 'https://example.test/private'), ('pendingUrl', 'https://example.test/new')]:
            pipe = self.fixture()
            pipe.observe.return_value[0]['tabs'][0][key] = value
            with self.assertRaisesRegex(RuntimeError, 'Refusing'):
                pipe.warm_synthetic_tabs()
            pipe.call.assert_not_called()


class TerminalFixtureTests(unittest.TestCase):
    def seed(self, root, *, automatic_rename='0', observed_name=None):
        names = {}

        def run(*args, **kwargs):
            if args[1] == 'rename-window':
                names[args[3]] = args[4]
            if args[1] == 'display-message':
                return (observed_name or names[args[4]]) + '\t' + automatic_rename
            return ''

        with patch.object(fixture, 'ROOT', root), patch.object(fixture, 'run', side_effect=run) as commands, \
                patch.object(fixture, 'launch') as launch, patch.object(fixture, 'wait_for'), \
                patch.object(fixture, 'write') as write:
            fixture.terminals()
        return names, commands, launch, write

    def test_every_window_has_an_explicit_verified_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            names, commands, launch, write = self.seed(Path(temporary))
        sessions = ['main'] + [f'scale-{index:02}' for index in range(2, 11)]
        self.assertEqual(names, {
            f'={session}:': f'scale-window-{index:02}'
            for index, session in enumerate(sessions, 1)
        })
        checks = [call.args for call in commands.call_args_list if call.args[1] == 'display-message']
        self.assertEqual(len(checks), 10)
        self.assertTrue(all(args[-1] == '#{window_name}\t#{automatic-rename}' for args in checks))
        self.assertEqual(launch.call_count, 6)
        write.assert_called_once()
        self.assertEqual(len(set(write.call_args.args[1])), 23)

    def test_automatic_name_is_rejected_even_when_current_text_matches(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'Synthetic window name is not fixed: main'):
                self.seed(Path(temporary), automatic_rename='1')

    def test_unexpected_window_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(RuntimeError, 'Synthetic window name is not fixed: main'):
                self.seed(Path(temporary), observed_name='[tmux]')


class ChromePipeProtocolTests(unittest.TestCase):
    def test_natural_controller_exit_records_actual_native_status_without_inventing_term(self):
        for code in (None, 0, 1, -5):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                pipe = self.pipe(root)
                pipe.process.poll.return_value = code
                with patch.object(fixture, 'ROOT', root), patch.object(fixture.time, 'time', return_value=123):
                    pipe.record_native_exit()
                path = root / 'chrome-session-stop-test-generation.json'
                if code is None:
                    self.assertFalse(path.exists())
                else:
                    self.assertEqual(json.loads(path.read_text()), {
                        'generation': 'test-generation', 'browser_pid': 123,
                        'exit_code': code, 'exit_observed_at': 123})

    def test_natural_exit_observation_preserves_prior_stop_timeout_and_completion(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipe = self.pipe(root)
            pipe.process.poll.return_value = 0
            prior = {'signal': 15, 'requested_at': 100, 'finished_at': 110, 'timed_out': True}
            with patch.object(fixture, 'ROOT', root):
                fixture.write('chrome-session-stop-test-generation', prior)
                pipe.record_native_exit()
            result = json.loads((root / 'chrome-session-stop-test-generation.json').read_text())
            self.assertTrue(all(result[key] == value for key, value in prior.items()))
            self.assertEqual(result['exit_code'], 0)

    def test_slow_pending_replacement_reports_exact_intent_without_promoting_pending_url(self):
        windows = [{'id': i, 'tabs': [{'id': i * 6 + j, 'url': f'http://127.0.0.1:18765/scale/{i}-{j}',
                    'status': 'complete', 'groupId': 10 if i == 0 and j < 4 else -1}
                    for j in range(6)]} for i in range(7)]
        after = copy.deepcopy(windows)
        after[0]['tabs'][3].update(id=999, url='', status='loading',
                                  pendingUrl='http://127.0.0.1:18765/scale/replacement')
        readings = iter([windows, after])
        def call(method, params=None, session=None):
            if method == 'Target.getTargets':
                return {'targetInfos': [{'type': 'service_worker', 'targetId': 'worker',
                                        'url': 'chrome-extension://fixture/service-worker.js'}]}
            if method == 'Target.attachToTarget':
                return {'sessionId': 'session'}
            if method == 'Target.detachFromTarget':
                return {}
            expression = params['expression']
            value = None
            if expression == 'chrome.windows.getAll({populate:true})':
                value = next(readings)
            elif 'chrome.tabs.update' in expression:
                value = json.loads(re.search(r'const url = (.+) \+ "-evolved";', expression).group(1)) + '-evolved'
            elif expression.startswith('chrome.tabs.create('):
                value = after[0]['tabs'][3]
            return {'result': {'value': value}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipe = self.pipe(root)
            pipe.extension_id = 'fixture'
            pipe.call = Mock(side_effect=call)
            with patch.object(fixture, 'ROOT', root):
                result = pipe.evolve_synthetic_tabs()
        intended = {item['id']: item['url'] for item in result['expected_urls']}
        self.assertEqual(len(intended), 42)
        self.assertNotIn(3, intended)
        self.assertEqual(intended[999], 'http://127.0.0.1:18765/scale/replacement')
        for window in windows:
            tab = window['tabs'][0]
            self.assertEqual(intended[tab['id']], tab['url'] + '-evolved')
        self.assertEqual(result['before'], result['after'])

    def test_session_stop_forwards_real_signal_and_waits_without_devtools_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            pipe = self.pipe(Path(temporary))
            pipe.process.poll.return_value = None
            pipe.process.returncode = 0
            pipe.call = Mock()
            with patch.object(fixture, 'ROOT', Path(temporary)):
                with self.assertRaises(SystemExit) as ended:
                    pipe.session_stop(fixture.signal.SIGTERM, None)
            self.assertEqual(ended.exception.code, 0)
            pipe.process.send_signal.assert_called_once_with(fixture.signal.SIGTERM)
            pipe.process.wait.assert_called_once_with(timeout=8)
            pipe.call.assert_not_called()

    def test_session_stop_timeout_is_not_a_successful_native_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipe = self.pipe(root)
            pipe.process.poll.return_value = None
            pipe.process.wait.side_effect = fixture.subprocess.TimeoutExpired('chrome', 8)
            with patch.object(fixture, 'ROOT', root):
                with self.assertRaises(SystemExit) as ended:
                    pipe.session_stop(fixture.signal.SIGTERM, None)
            self.assertEqual(ended.exception.code, 1)
            self.assertTrue(json.loads((root / 'chrome-session-stop-test-generation.json').read_text())['timed_out'])

    def pipe(self, root):
        pipe = fixture.ChromePipe.__new__(fixture.ChromePipe)
        pipe.generation = 'test-generation'
        pipe.gate = root / 'native-host-gate'
        pipe.companion_revision = 'test-revision'
        pipe.process = Mock(pid=123)
        pipe.process.poll.return_value = -5
        pipe.number = 1
        pipe.incoming = 0
        pipe.buffer = bytearray()
        return pipe

    def test_detach_cannot_mask_original_command_or_exit_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipe = self.pipe(root)
            with patch.object(fixture, 'ROOT', root), patch.object(
                    fixture.os, 'write', side_effect=BrokenPipeError('pipe closed')):
                with self.assertRaisesRegex(RuntimeError, 'Runtime.evaluate failed .*exit=-5'):
                    try:
                        pipe.call('Runtime.evaluate')
                    finally:
                        pipe.detach('session')
            record = json.loads((root / 'chrome-pipe-failure-test-generation.json').read_text())
            self.assertEqual(record['method'], 'Runtime.evaluate')
            self.assertEqual(record['exit_code'], -5)
            self.assertEqual([failure['method'] for failure in record['failures']],
                             ['Runtime.evaluate', 'Target.detachFromTarget'])

    def test_detach_failure_without_prior_error_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            pipe = self.pipe(Path(temporary))
            pipe.call = Mock(side_effect=RuntimeError('detach failed'))
            with self.assertRaisesRegex(RuntimeError, 'detach failed'):
                pipe.detach('session')

    def test_invalid_native_counts_are_recorded_without_opening_barrier(self):
        self.assert_invalid_startup(
            [{'id': 1, 'tabs': [{'id': 2, 'url': 'chrome://newtab/'}], 'groups': []}])

    def test_missing_original_groups_cannot_open_barrier(self):
        self.assert_invalid_startup([
            {'id': index, 'tabs': [{'url': 'about:blank#scale'}] * 6, 'groups': []}
            for index in range(7)])

    def assert_invalid_startup(self, state):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            pipe = self.pipe(root)
            pipe.observe = Mock(return_value=state)
            def expired(function, *, timeout):
                self.assertIsNone(function())
                raise RuntimeError('Bounded readiness expired')
            original_write = Path.write_text
            with patch.object(fixture, 'ROOT', root), patch.object(fixture, 'wait_for', expired), \
                    patch.object(fixture, 'process_identity', return_value={'pid': 123}), \
                    patch.object(Path, 'write_text', autospec=True, side_effect=original_write) as write_text:
                # Use a real private socket; the exited mock browser skips its
                # accept loop, while the startup observation path runs fully.
                pipe.serve()
            self.assertFalse(pipe.gate.exists())
            self.assertFalse((root / 'chrome-login-original.json').exists())
            self.assertFalse((root / 'chrome-last-companion-build.json').exists())
            observed = json.loads((root / 'chrome-login-observed.json').read_text())
            self.assertEqual(observed['counts'], {
                'windows': len(state), 'tabs': sum(len(w['tabs']) for w in state), 'groups': 0})
            self.assertEqual(observed['state'], state)
            self.assertFalse(json.loads((root / 'chrome-observation-launch.json').read_text())['baseline_valid'])
            self.assertEqual(json.loads((root / 'chrome-login-invalid.json').read_text())['observed'], state)
            self.assertNotIn(pipe.gate, [call.args[0] for call in write_text.call_args_list])


if __name__ == '__main__':
    unittest.main()
