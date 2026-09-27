from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import barrier, operations


class StartupBarrierTests(unittest.TestCase):
    def run_main(self):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return barrier.main([])

    def test_stop_precedes_companion_join_and_never_names_gui_units(self):
        process = Mock(pid=876543, returncode=0)
        process.communicate.return_value = (None, '')
        order = []
        def stopped(*args, **kwargs):
            order.append('stopped')
            return None, ''
        process.communicate.side_effect = stopped
        with patch.object(barrier.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True,
                          side_effect=lambda **kwargs: order.append('quiescent')) as quiet:
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(order, ['stopped', 'quiescent'])
        self.assertEqual(spawn.call_args.args[0], (
            'systemctl', '--user', 'stop', 'wsctl-startup-restore-*.service',
            'wsctl-restore-worker-*.service', 'wsctl-login-finalize.service'))
        self.assertTrue(spawn.call_args.kwargs['start_new_session'])
        self.assertLessEqual(process.communicate.call_args.kwargs['timeout'], 25)
        self.assertLessEqual(quiet.call_args.kwargs['timeout'], 10)

    def test_failed_stop_never_checks_companions(self):
        process = Mock(returncode=5)
        process.communicate.return_value = (None, 'stop failed\n')
        with patch.object(barrier.subprocess, 'Popen', return_value=process), \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True) as quiet:
            self.assertEqual(self.run_main(), 1)
        quiet.assert_not_called()

    def test_stop_timeout_kills_only_owned_group_and_reaps_within_budget(self):
        process = Mock(pid=876543, returncode=-9)
        process.communicate.side_effect = [subprocess.TimeoutExpired('systemctl', 24.75), (None, '')]
        with patch.object(barrier.subprocess, 'Popen', return_value=process), \
             patch.object(barrier.os, 'killpg') as kill, \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True) as quiet:
            self.assertEqual(self.run_main(), 1)
        kill.assert_called_once_with(876543, signal.SIGKILL)
        quiet.assert_not_called()
        self.assertEqual(process.communicate.call_count, 2)
        self.assertLessEqual(process.communicate.call_args.kwargs['timeout'], barrier.REAP_SECONDS)

    def test_expired_inherited_context_is_preserved_without_adopting_or_checking(self):
        context = operations.OperationContext.create('a' * 16, 'startup', budget=-1)
        process = Mock(returncode=0)
        process.communicate.return_value = (None, '')
        with operations.publisher(context), \
             patch.object(operations, 'context_from_status', side_effect=AssertionError('adopted operation')), \
             patch.object(operations.OperationContext, 'check', side_effect=AssertionError('rejected cleanup authority')), \
             patch.object(barrier.subprocess, 'Popen', return_value=process) as spawn, \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True, return_value=None):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(json.loads(spawn.call_args.kwargs['env'][operations.CONTEXT_ENV]), context.to_dict())

    def test_companion_failure_blocks_barrier(self):
        with patch.object(barrier, 'stop_startup_workers'), \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True,
                          side_effect=barrier.browser.BrowserUnavailable('mutations remain active')):
            self.assertEqual(self.run_main(), 1)

    def test_companion_receives_remaining_overall_deadline(self):
        clock = [100.0]
        def slow_stop(**kwargs):
            clock[0] += 31
        with patch.object(barrier.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(barrier, 'stop_startup_workers', side_effect=slow_stop), \
             patch.object(barrier.browser, 'wait_for_quiescence', create=True, return_value=None) as quiet:
            self.assertEqual(self.run_main(), 0)
        quiet.assert_called_once_with(timeout=4.0)

    def test_timeout_reaps_an_actual_disposable_process_without_systemctl(self):
        original = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            def cleanup():
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=3)
            self.addCleanup(cleanup)
            return child
        command = (sys.executable, '-c', 'import time; time.sleep(60)')
        started = time.monotonic()
        with patch.object(barrier, 'STOP_COMMAND', command), \
             patch.object(barrier, 'REAP_SECONDS', .1), \
             patch.object(barrier.subprocess, 'Popen', side_effect=spawn):
            with self.assertRaisesRegex(RuntimeError, 'did not stop'):
                barrier.stop_startup_workers(timeout=.2)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())
        with self.assertRaises(ProcessLookupError):
            os.kill(children[0].pid, 0)


if __name__ == '__main__':
    unittest.main()
