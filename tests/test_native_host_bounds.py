"""Isolated pipes/socket tests: never connects to a real Chrome companion."""
import json
import os
import select
import socket
import struct
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager, nullcontext
from unittest.mock import patch

from workspace_state import native_host as host
from workspace_state.browser import profile_socket_path


class NativeHostBoundsTests(unittest.TestCase):
    def read_exact(self, stream, count):
        result = bytearray()
        deadline = time.monotonic() + 3
        while len(result) < count:
            self.assertTrue(select.select([stream], [], [], max(0, deadline - time.monotonic()))[0], 'host stalled')
            data = os.read(stream.fileno(), count - len(result))
            self.assertTrue(data, 'host closed unexpectedly')
            result.extend(data)
        return bytes(result)

    def read_native(self, stream):
        size = struct.unpack('=I', self.read_exact(stream, 4))[0]
        return json.loads(self.read_exact(stream, size))

    @contextmanager
    def running_host(self, **limits):
        with tempfile.TemporaryDirectory() as root, patch.dict(os.environ, {'XDG_RUNTIME_DIR': root}), (patch.multiple(host, **limits) if limits else nullcontext()):
            incoming, send_fd = os.pipe()
            receive_fd, outgoing = os.pipe()
            streams = [os.fdopen(fd, mode, buffering=0) for fd, mode in
                       [(incoming, 'rb'), (outgoing, 'wb'), (receive_fd, 'rb'), (send_fd, 'wb')]]
            native_in, native_out, receive, send = streams
            results, errors, clients = [], [], []
            def serve():
                try:
                    results.append(host.serve(native_in, native_out))
                except Exception as error:
                    errors.append(error)
            thread = threading.Thread(target=serve, daemon=True)
            thread.start()
            try:
                send.write(host.encode_native_message({'type': 'hello', 'profile': 'Default'}))
                self.assertTrue(self.read_native(receive)['ok'])
                def connect():
                    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    client.settimeout(2)
                    client.connect(str(profile_socket_path('Default')))
                    clients.append(client)
                    return client
                yield connect, receive, send, thread, results
            finally:
                for client in clients:
                    client.close()
                send.close()
                thread.join(3)
                for stream in streams:
                    stream.close()
                self.assertFalse(thread.is_alive(), 'host did not stop after native EOF')
                self.assertFalse(errors, errors)

    def test_nonreading_client_does_not_block_other_requests(self):
        with self.running_host() as (connect, receive, send, _, _):
            slow = connect()
            slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024)
            slow.sendall(b'{"action":"slow"}\n')
            first = self.read_native(receive)
            payload = json.dumps({'id': first['id'], 'ok': True, 'result': 'x' * (2 * 1024 * 1024)}).encode()
            send.write(struct.pack('=I', len(payload)) + payload)
            fast = connect()
            fast.sendall(b'{"action":"ping"}\n')
            second = self.read_native(receive)
            self.assertEqual(second['action'], 'ping')
            send.write(host.encode_native_message({'id': second['id'], 'ok': True}))
            self.assertTrue(json.loads(fast.recv(4096))['ok'])

    def test_native_backpressure_has_absolute_deadline(self):
        with self.running_host(NATIVE_WRITE_SECONDS=.15) as (connect, receive, _, thread, results):
            client = connect()
            client.sendall(json.dumps({'action': 'capture', 'payload': 'x' * 100000}).encode() + b'\n')
            # Leave the native output pipe unread; bounded loop must terminate.
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [1])
            self.assertFalse(profile_socket_path('Default').exists())

    def test_idle_clients_expire_and_malformed_request_is_contained(self):
        with self.running_host(CLIENT_READ_SECONDS=.05) as (connect, receive, send, _, _):
            idle = connect()
            self.assertEqual(idle.recv(1), b'')
            malformed = connect()
            malformed.sendall(b'[]\n')
            self.assertFalse(json.loads(malformed.recv(4096))['ok'])
            good = connect()
            good.sendall(b'{"action":"ping"}\n')
            request = self.read_native(receive)
            send.write(host.encode_native_message({'id': request['id'], 'ok': True}))
            self.assertTrue(json.loads(good.recv(4096))['ok'])

    def test_second_request_never_replays_first_frame(self):
        with self.running_host() as (connect, receive, _, _, _):
            client = connect()
            client.sendall(b'{"action":"one"}\n')
            self.assertEqual(self.read_native(receive)['action'], 'one')
            client.sendall(b'{"action":"two"}\n')
            self.assertEqual(client.recv(1), b'')
            self.assertFalse(select.select([receive], [], [], .1)[0])
