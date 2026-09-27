from __future__ import annotations

import base64
import hashlib
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workspace_state.codex_readiness import loaded_thread_ids


def _read(connection, count):
    data = bytearray()
    while len(data) < count:
        part = connection.recv(count - len(data))
        if not part:
            raise EOFError()
        data.extend(part)
    return bytes(data)


def _receive(connection):
    first, second = _read(connection, 2)
    if not second & 128:
        raise AssertionError("Client must mask WebSocket frames")
    length = second & 127
    if length == 126:
        length = struct.unpack("!H", _read(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _read(connection, 8))[0]
    mask = _read(connection, 4)
    data = _read(connection, length)
    return first & 15, bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))


def _send(connection, data, opcode=1, final=True):
    first = opcode | (128 if final else 0)
    length = len(data)
    if length < 126:
        header = bytes((first, length))
    else:
        header = bytes((first, 126)) + struct.pack("!H", length)
    connection.sendall(header + data)


def _handshake(connection, *, valid=True):
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        header.extend(_read(connection, 1))
    key = next(line.split(":", 1)[1].strip() for line in header.decode().splitlines()
               if line.startswith("Sec-WebSocket-Key:"))
    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
    if not valid:
        accept = "invalid"
    connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                       f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())


class CodexReadinessTests(unittest.TestCase):
    def _probe(self, handler, *, timeout=1.0):
        errors = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "app-server-control"
            control.mkdir()
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(control / "app-server-control.sock"))
                server.listen(1)
                server.settimeout(2)

                def serve():
                    try:
                        with server.accept()[0] as connection:
                            connection.settimeout(2)
                            handler(connection)
                    except Exception as error:
                        errors.append(error)

                worker = threading.Thread(target=serve)
                worker.start()
                result = loaded_thread_ids(root, timeout=timeout)
                worker.join(3)
                self.assertFalse(worker.is_alive())
                if errors:
                    raise errors[0]
                return result

    def test_read_only_paginated_probe_handles_ping_and_fragmented_response(self):
        requests = []

        def serve(connection):
            _handshake(connection)
            requests.append(json.loads(_receive(connection)[1]))
            _send(connection, b'ping', opcode=9)
            self.assertEqual(_receive(connection), (10, b'ping'))
            _send(connection, b'{"id":1,"result":', final=False)
            _send(connection, b'{}}', opcode=0)
            requests.append(json.loads(_receive(connection)[1]))
            requests.append(json.loads(_receive(connection)[1]))
            _send(connection, json.dumps({"id": 2, "result": {
                "data": ["thread-a"], "nextCursor": "page-2",
            }}).encode())
            requests.append(json.loads(_receive(connection)[1]))
            _send(connection, json.dumps({"id": 3, "result": {
                "data": ["thread-b"], "nextCursor": None,
            }}).encode())

        self.assertEqual(self._probe(serve), {"thread-a", "thread-b"})
        self.assertEqual([request["method"] for request in requests], [
            "initialize", "initialized", "thread/loaded/list", "thread/loaded/list",
        ])
        self.assertEqual(requests[-1]["params"], {"cursor": "page-2"})

    def test_invalid_handshake_is_unavailable(self):
        self.assertIsNone(self._probe(lambda connection: _handshake(connection, valid=False)))

    def test_daemon_rpc_error_is_unavailable(self):
        def serve(connection):
            _handshake(connection)
            _receive(connection)
            _send(connection, b'{"id":1,"error":{"code":-32601}}')

        self.assertIsNone(self._probe(serve))

    def test_oversized_frame_is_unavailable_without_reading_body(self):
        def serve(connection):
            _handshake(connection)
            _receive(connection)
            connection.sendall(bytes((129, 127)) + struct.pack("!Q", 2 ** 32))

        self.assertIsNone(self._probe(serve))

    def test_stalled_daemon_is_bounded_and_unavailable(self):
        def serve(connection):
            # EOF proves the probe abandoned the unresponsive endpoint.
            while connection.recv(4096):
                pass

        start = time.monotonic()
        self.assertIsNone(self._probe(serve, timeout=0.05))
        self.assertLess(time.monotonic() - start, 1.0)

    def test_missing_daemon_is_unavailable_and_not_started(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(loaded_thread_ids(root))
            self.assertEqual(list(root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
