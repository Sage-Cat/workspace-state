from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch


spec = importlib.util.spec_from_file_location('vm_native_barrier',
    Path(__file__).parent / 'integration/vm_native_barrier.py')
barrier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(barrier)
scale_spec = importlib.util.spec_from_file_location('vm_scale_observer',
    Path(__file__).parent / 'integration/run_vm_scale.py')
scale = importlib.util.module_from_spec(scale_spec)
scale_spec.loader.exec_module(scale)


class NativeObservationBarrierTests(unittest.TestCase):
    def test_failed_observe_returns_diagnostic_without_stopping_native_browser(self):
        pipe = object.__new__(scale.ChromePipe)
        pipe.generation = 'synthetic'
        pipe.process = Mock()
        pipe.observe = Mock(side_effect=RuntimeError('worker temporarily absent'))
        with patch.object(scale, 'write') as receipt:
            self.assertEqual(pipe.observation_response(), {'error': 'worker temporarily absent'})
        pipe.process.assert_not_called()
        self.assertFalse(pipe.process.method_calls)
        self.assertEqual(receipt.call_args.args[0], 'chrome-observation-error-synthetic')

    def test_only_hello_and_explicit_read_only_actions_pass_before_catalog(self):
        for action in ('ping', 'capture', 'list_windows', 'inspect_original_window'):
            self.assertTrue(barrier.initialization_safe({'action': action}))
        self.assertTrue(barrier.initialization_safe({'type': 'hello'}))
        for action in ('restore_window', 'restore_status', 'identify_window', 'focus_window',
                       'release_window_identification', 'renew_window_identification',
                       'repair_restored_tabs', 'recover_original_window', 'close_restored_window',
                       'future_action', None):
            self.assertFalse(barrier.initialization_safe({'action': action}))
        for value in ({'type': 'hello', 'action': 'restore_window'},
                      {'type': 'hello', 'action': 'ping'},
                      {'type': 'request', 'action': 'capture'}, {'action': ['capture']},
                      {'action': {'name': 'capture'}}):
            self.assertFalse(barrier.initialization_safe(value))

    def test_fragmented_protocol_preserves_exact_frame_and_trailing_bytes(self):
        value = {'id': 'exact', 'action': 'restore_window', 'payload': {'window': 7}}
        raw = json.dumps(value).encode()
        frame = struct.pack('<I', len(raw)) + raw
        buffer = bytearray(frame[:-1])
        self.assertEqual(list(barrier.frames(buffer)), [])
        buffer.extend(frame[-1:] + b'\x01')
        self.assertEqual(list(barrier.frames(buffer)), [(frame, value)])
        self.assertEqual(buffer, b'\x01')

    def test_oversized_or_nonobject_native_frame_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'exceeds'):
            list(barrier.frames(bytearray(struct.pack('<I', barrier.MAX_FRAME + 1))))
        with self.assertRaisesRegex(RuntimeError, 'not an object'):
            list(barrier.frames(bytearray(struct.pack('<I', 2) + b'[]')))


class NativeBarrierPipeTests(unittest.TestCase):
    """Real isolated pipes exercise EOF, buffering and the unchanged child."""
    def frame(self, value):
        raw = json.dumps(value).encode()
        return struct.pack('<I', len(raw)) + raw

    def start(self, root, script, *, open_gate=False, timeout=8, queue=barrier.MAX_QUEUE):
        executable = root / 'synthetic-host'
        executable.write_text('#!' + sys.executable + '\n' + script)
        executable.chmod(0o700)
        gate = root / 'gate'
        if open_gate:
            gate.write_text('exact original catalog ready\n')
        receipt = root / 'receipt.json'
        runner = '''import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('barrier', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.IO_TIMEOUT = float(sys.argv[5])
module.MAX_QUEUE = int(sys.argv[6])
raise SystemExit(module.serve(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])))
'''
        process = subprocess.Popen([sys.executable, '-c', runner, barrier.__file__,
                                    str(executable), str(gate), str(receipt), str(timeout), str(queue)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
        return process, gate, receipt

    def test_exit_drains_all_final_frames_and_forwards_exact_chrome_replies(self):
        frames = b''.join(self.frame({'type': 'hello', 'number': index}) for index in range(4))
        reply = self.frame({'id': 'reply', 'result': {'unchanged': True}})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = ('import os, sys\n'
                      f'os.write(1, {frames!r})\n'
                      f'assert sys.stdin.buffer.read() == {reply!r}\n')
            process, _, receipt = self.start(root, script)
            stdout, stderr = process.communicate(reply, timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, frames)
            proof = json.loads(receipt.read_text())
            self.assertEqual(proof['native_host_exit'], 0)
            self.assertEqual(proof['initialization_count'], 4)
            self.assertNotIn('error', proof)

    def test_partial_eof_in_either_direction_is_not_a_successful_exit(self):
        frame = self.frame({'type': 'hello'})
        for direction in ('chrome', 'host'):
            with self.subTest(direction=direction), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                script = ('import os, sys\n' + (f'os.write(1, {frame[:-1]!r})\n'
                          if direction == 'host' else 'sys.stdin.buffer.read()\n'))
                process, _, receipt = self.start(root, script)
                stdout, stderr = process.communicate(frame[:-1] if direction == 'chrome' else b'', timeout=5)
                self.assertNotEqual(process.returncode, 0, stderr.decode())
                proof = json.loads(receipt.read_text())
                self.assertIn(direction + ' EOF truncated a frame', proof['error'])
                self.assertEqual(proof['partial_bytes'][direction], len(frame) - 1)
                self.assertEqual(stdout, b'')

    def test_unreleased_mutation_eof_is_recorded_and_refused(self):
        frame = self.frame({'id': 'exact', 'action': 'restore_window'})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, _, receipt = self.start(root, 'import os\n' + f'os.write(1, {frame!r})\n')
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, b'')
            proof = json.loads(receipt.read_text())
            self.assertIn('unreleased gated requests', proof['error'])
            self.assertEqual(proof['pending_requests'], 1)
            self.assertEqual(proof['pending_bytes'], len(frame))

    def test_gate_release_preserves_original_mutation_order_and_bytes(self):
        frames = b''.join(self.frame({'id': str(index), 'action': 'restore_window'})
                          for index in range(3))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, gate, receipt = self.start(root, 'import os,time\n' +
                f'os.write(1, {frames!r})\ntime.sleep(.5)\n')
            try:
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if receipt.exists() and json.loads(receipt.read_text()).get('deferred_count') == 3:
                        break
                    time.sleep(.01)
                else:
                    self.fail('The exact mutation frames did not reach the closed gate')
                gate.write_text('exact catalog durably verified\n')
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr.decode())
                self.assertEqual(stdout, frames)
                self.assertEqual(json.loads(receipt.read_text())['deferred_released'], 3)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)

    def test_proof_samples_are_bounded_while_exact_total_is_preserved(self):
        frames = b''.join(self.frame({'type': 'hello'}) for _ in range(barrier.MAX_PROOF_SAMPLES + 30))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, _, receipt = self.start(root, 'import os\n' + f'os.write(1, {frames!r})\n')
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, frames)
            proof = json.loads(receipt.read_text())
            self.assertEqual(proof['initialization_count'], barrier.MAX_PROOF_SAMPLES + 30)
            self.assertEqual(len(proof['initialization_passed']), barrier.MAX_PROOF_SAMPLES)

    def test_deferred_queue_and_oversized_diagnostic_values_are_bounded(self):
        frames = b''.join(self.frame({'id': str(index), 'action': 'restore_window', 'payload': 'x' * 400})
                          for index in range(4))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, _, receipt = self.start(root, 'import os\n' + f'os.write(1, {frames!r})\n', queue=1024)
            stdout, stderr = process.communicate(timeout=5)
            self.assertNotEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, b'')
            proof = json.loads(receipt.read_text())
            self.assertIn('mutation queue exceeds the bound', proof['error'])
            self.assertLessEqual(proof['pending_bytes'], 1024)
        frame = self.frame({'id': 'private synthetic identifier' * 1000, 'action': 'x' * 8192})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, _, receipt = self.start(root, 'import os\n' + f'os.write(1, {frame!r})\n')
            process.communicate(timeout=5)
            proof = json.loads(receipt.read_text())
            self.assertEqual(len(proof['deferred_actions'][0]['id']), 256)
            self.assertEqual(len(proof['deferred_actions'][0]['action']), 256)
            self.assertLess(receipt.stat().st_size, 2048)

    def test_output_backpressure_has_a_bounded_failure_not_a_deadlock(self):
        frame = self.frame({'action': 'capture', 'padding': 'x' * (128 * 1024)})
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            process, _, receipt = self.start(root, 'import os,time\n' +
                f'os.write(1, {frame!r})\ntime.sleep(5)\n', timeout=.2)
            try:
                # Keep the output pipe unread so the forwarding queue stalls.
                process.wait(timeout=3)
                proof = json.loads(receipt.read_text())
                self.assertNotEqual(process.returncode, 0)
                self.assertIn('forwarding stalled', proof['error'])
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
