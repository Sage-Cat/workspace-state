"""Real subprocess/lock regressions; all mutable state lives in a private directory."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'src'
WORKER = r'''
import json, sys
from pathlib import Path
from workspace_state import login_status as status, operations, startup
captured = operations.current()
print('ready', flush=True)
for line in sys.stdin:
    request = json.loads(line)
    action = request['action']
    if action == 'initialize':
        result = status.initialize(request['generation'])
    elif action == 'shutdown':
        result = status.initialize_shutdown(request['generation'], request['operation'])
    elif action == 'late':
        result = [status.update_stage('warmup', 'ready', 'stale publisher'),
                  status.fail_active('stale failure')]
        try:
            operations.context_from_status(status.status_path(), 'startup')
        except ValueError:
            result.append('refused')
        else:
            result.append('adopted')
    elif action == 'cancel':
        result = status.cancel_shutdown('isolated cancellation', recovery_pending=request['pending'])
    elif action == 'transition':
        result = status.set_operation_state(request['state'])
    elif action == 'write':
        result = all(status.update_stage(request['stage'], 'running', f'update {i}')
                     for i in range(request['count']))
        result = status.update_stage(request['stage'], 'ready', 'finished') and result
    elif action == 'directory':
        result = str(startup.startup_directory(Path(request['root']), request['boot'], request.get('generation')))
    else:
        raise ValueError(action)
    context = operations.current()
    print(json.dumps({'result': result, 'context': context.to_dict() if context else None}), flush=True)
'''


class LifecycleProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wsctl-lifecycle-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = dict(os.environ, PYTHONPATH=str(SOURCE),
                        XDG_RUNTIME_DIR=str(self.root / 'runtime'),
                        XDG_STATE_HOME=str(self.root / 'state'))
        self.env.pop('WSCTL_OPERATION_CONTEXT', None)
        self.runtime = self.root / 'runtime/workspace-state'
        self.status = self.runtime / 'login-hud-status.json'
        self.owner = self.runtime / 'current-operation.json'
        self.processes = []
        self.addCleanup(self.stop_workers)

    def stop_workers(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def read_line(self, process):
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(timeout=5), 'child response exceeded five seconds')
        line = process.stdout.readline()
        self.assertTrue(line, f'child exited unexpectedly: {process.poll()}')
        return line.strip()

    def spawn(self, context=None):
        env = self.env.copy()
        if context:
            env['WSCTL_OPERATION_CONTEXT'] = json.dumps(context)
        process = subprocess.Popen([sys.executable, '-u', '-c', WORKER], env=env,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        self.assertEqual(self.read_line(process), 'ready')
        return process

    def send(self, process, **request):
        process.stdin.write(json.dumps(request) + '\n')
        process.stdin.flush()

    def request(self, process, **request):
        self.send(process, **request)
        return json.loads(self.read_line(process))

    def initialize(self):
        coordinator = self.spawn()
        response = self.request(coordinator, action='initialize', generation='a' * 16)
        self.assertTrue(response['result'])
        return coordinator, response['context']

    def document(self):
        return json.loads(self.status.read_text())

    def assert_owner(self, context):
        self.assertEqual(json.loads(self.owner.read_text())['operation_context'], context)
        self.assertEqual(self.document()['operation_context'], context)

    def test_delayed_startup_process_cannot_publish_across_shutdown_recovery_or_relogin(self):
        coordinator, startup = self.initialize()
        delayed = self.spawn(startup)  # Captures authority before the transition.
        shutdown = self.request(coordinator, action='shutdown', generation='a' * 16,
                                operation='b' * 32)['context']
        for phase in ('shutdown', 'recovering', 'next-login'):
            with self.subTest(phase=phase):
                if phase == 'recovering':
                    self.assertTrue(self.request(coordinator, action='cancel', pending=True)['result'])
                elif phase == 'next-login':
                    self.assertTrue(self.request(coordinator, action='cancel', pending=False)['result'])
                    response = self.request(coordinator, action='initialize', generation='c' * 16)
                    self.assertTrue(response['result'])
                    self.assertNotEqual(response['context']['operation_id'], startup['operation_id'])
                before = (self.status.read_bytes(), self.owner.read_bytes())
                result = self.request(delayed, action='late')
                self.assertEqual(result['result'], [False, False, 'refused'])
                self.assertEqual(result['context'], startup)
                self.assertEqual((self.status.read_bytes(), self.owner.read_bytes()), before)
        self.assertNotEqual(self.document()['operation_context'], shutdown)

    def test_recovery_retains_owner_and_stale_shutdown_process_cannot_cancel_successor(self):
        coordinator, _ = self.initialize()
        shutdown = self.request(coordinator, action='shutdown', generation='a' * 16,
                                operation='b' * 32)['context']
        recovery = self.spawn(shutdown)
        self.assertTrue(self.request(recovery, action='cancel', pending=True)['result'])
        self.assert_owner(shutdown)
        self.assertTrue(self.document()['recovery_pending'])
        self.assertFalse(self.document()['commit_authorized'])
        self.assertTrue(self.request(recovery, action='transition', state='recovery-failed')['result'])
        self.assert_owner(shutdown)
        self.assertTrue(self.document()['recovery_pending'])
        self.assertFalse(self.request(recovery, action='transition', state='authorized')['result'])
        self.assertTrue(self.request(recovery, action='transition', state='recovering')['result'])
        self.assertTrue(self.request(recovery, action='cancel', pending=False)['result'])
        self.assert_owner(shutdown)
        self.assertEqual(self.document()['operation_state'], 'cancelled')
        successor = self.request(coordinator, action='shutdown', generation='a' * 16,
                                 operation='d' * 32)['context']
        before = self.status.read_bytes()
        self.assertFalse(self.request(recovery, action='cancel', pending=True)['result'])
        self.assertEqual(self.status.read_bytes(), before)
        self.assert_owner(successor)

    def test_concurrent_publishers_preserve_all_stages_and_exact_owner(self):
        _, context = self.initialize()
        workers = [self.spawn(context) for _ in range(6)]
        for index, worker in enumerate(workers):
            self.send(worker, action='write', stage=f'integration-{index}', count=12)
        for worker in workers:
            self.assertTrue(json.loads(self.read_line(worker))['result'])
        self.assert_owner(context)
        stages = {item['id']: item for item in self.document()['stages']}
        for index in range(len(workers)):
            self.assertEqual(stages[f'integration-{index}']['state'], 'ready')
            self.assertEqual(stages[f'integration-{index}']['message'], 'finished')
        log = (self.runtime / 'login-hud.log').read_text()
        for index in range(len(workers)):
            self.assertEqual(log.count(f'integration-{index}: ready - finished'), 1)
            self.assertEqual(log.count(f'integration-{index}: running - update '), 12)

    def test_same_boot_competing_login_processes_adopt_legacy_markers_only_once(self):
        marker_root = self.root / 'markers'
        setup = self.spawn()
        legacy = Path(self.request(setup, action='directory', root=str(marker_root), boot='same-boot')['result'])
        (legacy / 'terminals.done').write_text('legacy attempt')
        generations = ['a' * 16, 'b' * 16]
        workers = [self.spawn() for _ in generations]
        for worker, generation in zip(workers, generations):
            self.send(worker, action='directory', root=str(marker_root), boot='same-boot', generation=generation)
        directories = [Path(json.loads(self.read_line(worker))['result']) for worker in workers]
        self.assertEqual(sum(directory == legacy for directory in directories), 1)
        self.assertEqual(len(set(directories)), 2)
        owner = json.loads((legacy / 'generation-owner.json').read_text())
        self.assertEqual(owner['login_generation'], generations[directories.index(legacy)])
        for worker, generation, directory in zip(workers, generations, directories):
            self.assertEqual(self.request(worker, action='directory', root=str(marker_root),
                                          boot='same-boot', generation=generation)['result'], str(directory))
            self.assertEqual((directory / 'terminals.done').exists(), directory == legacy)

    def test_corrupt_presentation_does_not_let_delayed_process_replace_shutdown_owner(self):
        coordinator, startup = self.initialize()
        delayed = self.spawn(startup)
        shutdown = self.request(coordinator, action='shutdown', generation='a' * 16,
                                operation='b' * 32)['context']
        self.status.write_text('truncated presentation')
        self.assertEqual(self.request(delayed, action='late')['result'], [False, False, 'refused'])
        self.assertEqual(self.status.read_text(), 'truncated presentation')
        self.assertEqual(json.loads(self.owner.read_text())['operation_context'], shutdown)


@unittest.skipUnless(shutil.which('tmux') and Path('/bin/bash').exists(), 'tmux/bash unavailable')
class DisposableTmuxInputTests(unittest.TestCase):
    def test_existing_shell_pending_input_is_preserved_without_restore_commands(self):
        from unittest.mock import patch
        from workspace_state import restore
        from workspace_state.util import CommandError
        with tempfile.TemporaryDirectory(prefix='wsctl-tmux-test-') as temporary:
            root = Path(temporary)
            socket = root / 'socket'
            env = dict(os.environ, PS1='INTEGRATION> ', HISTFILE='/dev/null')
            env.pop('TMUX', None)
            env.pop('TMUX_PANE', None)
            for key in ('PROMPT_COMMAND', 'BASH_ENV', 'ENV'):
                env.pop(key, None)
            def tmux(*args, check=True):
                return subprocess.run(['tmux', '-S', str(socket), '-f', '/dev/null', *args],
                                      env=env, check=check, capture_output=True, text=True, timeout=5)
            try:
                pane = tmux('new-session', '-d', '-P', '-F', '#{pane_id}', '-s', 'isolated', '-x', '200', '-y', '30',
                            '-c', str(root), '/bin/bash --noprofile --norc').stdout.strip()
                def await_capture(fragment):
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        capture = tmux('capture-pane', '-p', '-t', pane).stdout
                        if fragment in capture:
                            return capture
                        time.sleep(.01)
                    self.fail(f'private shell did not display {fragment!r}')
                await_capture('INTEGRATION>')
                pending = f'printf accidental-execution > {root / "executed"}'
                tmux('send-keys', '-t', pane, '-l', pending)  # Private socket, deliberately no Enter.
                with patch.object(restore, 'run', side_effect=lambda command: tmux(*command[1:]).stdout):
                    state = restore._tmux_state('isolated')
                index, window = next(iter(state.items()))
                pane_index = next(iter(window['panes']))
                snapshot = {'windows': [{'index': index, 'name': window['name'], 'panes': [
                    {'index': pane_index, 'cwd': str(root), 'codex': {'session_id': '00000000-0000-0000-0000-000000000001'}},
                ]}]}
                before = await_capture(pending)
                self.assertIn(pending, before)
                with patch.object(restore, 'run', side_effect=AssertionError('restore attempted a tmux mutation')):
                    with self.assertRaisesRegex(CommandError, 'unverified shell input'):
                        restore._reconcile_tmux('isolated', snapshot, state, dry_run=False, repair_processes=True)
                self.assertEqual(tmux('capture-pane', '-p', '-t', pane).stdout, before)
                self.assertFalse((root / 'executed').exists())
            finally:
                tmux('kill-server', check=False)


if __name__ == '__main__':
    unittest.main()
