#!/usr/bin/python3
"""Synthetic-guest native protocol barrier; the installed host runs unchanged.

Hello and explicitly read-only requests pass during observation. All other
host requests wait for the durable original-browser catalog before reaching
Chrome. This is fixture sequencing, not a production native-host substitute.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import selectors
import signal
import socket
import struct
import subprocess
import sys
import time

READ_ONLY = frozenset({'ping', 'capture', 'list_windows', 'inspect_original_window'})
MAX_FRAME = 16 * 1024 * 1024
MAX_QUEUE = MAX_FRAME + 4
MAX_PROOF_SAMPLES = 100
IO_TIMEOUT = 8


def frames(buffer):
    while len(buffer) >= 4:
        length = struct.unpack('<I', buffer[:4])[0]
        if length > MAX_FRAME:
            raise RuntimeError('Synthetic native frame exceeds the bound')
        if len(buffer) < length + 4:
            return
        raw = bytes(buffer[:length + 4])
        del buffer[:length + 4]
        value = json.loads(raw[4:])
        if not isinstance(value, dict):
            raise RuntimeError('Synthetic native frame is not an object')
        yield raw, value


def initialization_safe(value):
    # A handshake cannot also carry an action. Unknown/type-confused requests
    # wait behind the gate, including future protocol additions.
    if value.get('type') == 'hello':
        return 'action' not in value
    action = value.get('action')
    return isinstance(action, str) and action in READ_ONLY and 'type' not in value


def serve(executable, gate, receipt):
    child = subprocess.Popen([str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    proof = {'started_at': time.time(), 'native_host_pid': child.pid,
             'gate': str(gate), 'initialization_passed': [], 'deferred_actions': [],
             'initialization_count': 0, 'deferred_count': 0}

    def record():
        temporary = receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps(proof, indent=2) + '\n')
        temporary.replace(receipt)

    stop_deadline = None

    def stopping(signum, _frame):
        nonlocal stop_deadline
        if child.poll() is None:
            child.send_signal(signum)
        proof.update(signal=signum, stop_requested_at=time.time())
        stop_deadline = time.monotonic() + IO_TIMEOUT
        record()

    original_signal = signal.signal(signal.SIGTERM, stopping)
    selector = selectors.DefaultSelector()
    inputs = {'chrome': sys.stdin.buffer.fileno(), 'host': child.stdout.fileno()}
    outputs = {'chrome': sys.stdout.buffer.fileno(), 'host': child.stdin.fileno()}
    blocking = {fd: os.get_blocking(fd) for fd in {*inputs.values(), *outputs.values()}}
    for fd in blocking:
        os.set_blocking(fd, False)
    for direction, fd in inputs.items():
        selector.register(fd, selectors.EVENT_READ, ('read', direction))
    buffers = {'chrome': bytearray(), 'host': bytearray()}
    outgoing = {'chrome': bytearray(), 'host': bytearray()}
    pending = deque()
    pending_bytes = 0
    eof = set()
    write_progress = {'chrome': None, 'host': None}

    def queue(direction, raw):
        if len(outgoing[direction]) + len(raw) > MAX_QUEUE:
            raise RuntimeError('Synthetic native forwarding queue exceeds the bound')
        if not outgoing[direction]:
            write_progress[direction] = time.monotonic()
            selector.register(outputs[direction], selectors.EVENT_WRITE, ('write', direction))
        outgoing[direction].extend(raw)

    def sample(name, count, value):
        proof[count] += 1
        proof[name] = [*proof[name], value][-MAX_PROOF_SAMPLES:]
        record()

    def label(value):
        return value[:256] if isinstance(value, str) else None

    record()
    try:
        # Child exit is not stdout EOF: its last frames may still be in the
        # pipe. Half-close Chrome input only after all replies reach the host.
        while True:
            if pending and gate.exists():
                released = 0
                while pending and len(outgoing['chrome']) + len(pending[0]) <= MAX_QUEUE:
                    raw = pending.popleft()
                    pending_bytes -= len(raw)
                    queue('chrome', raw)
                    released += 1
                proof.update(gate_opened_at=proof.get('gate_opened_at', time.time()),
                             deferred_released=proof.get('deferred_released', 0) + released)
                record()
            if 'chrome' in eof and not outgoing['host'] and not child.stdin.closed:
                child.stdin.close()
            if 'host' in eof and not outgoing['chrome']:
                if pending:
                    raise RuntimeError('Synthetic native host ended with unreleased gated requests')
                if outgoing['host']:
                    raise RuntimeError('Synthetic native host ended with unforwarded Chrome replies')
                return child.wait(timeout=IO_TIMEOUT)
            now = time.monotonic()
            if stop_deadline is not None and now >= stop_deadline:
                raise RuntimeError('Synthetic native EOF/stop settlement timed out')
            if any(value is not None and now - value >= IO_TIMEOUT for value in write_progress.values()):
                raise RuntimeError('Synthetic native forwarding stalled')
            for key, _ in selector.select(.1):
                event, direction = key.data
                if event == 'write':
                    try:
                        written = os.write(key.fd, outgoing[direction])
                    except BlockingIOError:
                        continue
                    if not written:
                        raise RuntimeError('Synthetic native forwarding made no progress')
                    del outgoing[direction][:written]
                    write_progress[direction] = time.monotonic() if outgoing[direction] else None
                    if not outgoing[direction]:
                        selector.unregister(key.fd)
                    continue
                try:
                    chunk = os.read(key.fd, 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fd)
                    eof.add(direction)
                    if buffers[direction]:
                        raise RuntimeError(f'Synthetic native {direction} EOF truncated a frame')
                    if stop_deadline is None:
                        stop_deadline = time.monotonic() + IO_TIMEOUT
                    continue
                buffer = buffers[direction]
                buffer.extend(chunk)
                for raw, value in frames(buffer):
                    if direction == 'chrome':
                        queue('host', raw)
                    elif gate.exists():
                        # Release older gated requests first; never reorder
                        # host mutations when the catalog gate opens mid-read.
                        while pending:
                            older = pending.popleft()
                            pending_bytes -= len(older)
                            queue('chrome', older)
                            proof['deferred_released'] = proof.get('deferred_released', 0) + 1
                        queue('chrome', raw)
                    elif initialization_safe(value):
                        queue('chrome', raw)
                        sample('initialization_passed', 'initialization_count',
                               label(value.get('action', value.get('type'))))
                    else:
                        if pending_bytes + len(raw) > MAX_QUEUE:
                            raise RuntimeError('Synthetic native mutation queue exceeds the bound')
                        pending.append(raw)
                        pending_bytes += len(raw)
                        sample('deferred_actions', 'deferred_count',
                               {'action': label(value.get('action')), 'id': label(value.get('id'))})
                if len(buffer) > MAX_QUEUE:
                    raise RuntimeError('Synthetic native input buffer exceeds the bound')
    except Exception as error:
        proof.update(error=str(error)[:512], pending_requests=len(pending),
                     pending_bytes=pending_bytes,
                     partial_bytes={key: len(value) for key, value in buffers.items()})
        raise
    finally:
        selector.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=IO_TIMEOUT)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=IO_TIMEOUT)
        child.stdin.close()
        child.stdout.close()
        for fd, value in blocking.items():
            try:
                os.set_blocking(fd, value)
            except OSError:
                pass
        signal.signal(signal.SIGTERM, original_signal)
        proof.update(finished_at=time.time(), native_host_exit=child.returncode)
        record()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--gate', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    root = Path.home() / '.local/state/wsctl-scale'
    if (socket.gethostname() != 'wsctl-validation' or os.getuid() != 1000
            or args.gate.parent != root or args.receipt.parent != root
            or not args.gate.name.startswith('chrome-observation-ready-')
            or not args.receipt.name.startswith('chrome-native-barrier-')):
        parser.error('Only the explicitly isolated synthetic guest is supported')
    return serve(Path.home() / '.local/bin/wsctl-native-host', args.gate, args.receipt)


if __name__ == '__main__':
    raise SystemExit(main())
