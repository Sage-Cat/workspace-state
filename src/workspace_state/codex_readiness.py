"""Read loaded Codex identities from an existing local app-server daemon.

The daemon uses WebSocket framing over its Unix control socket. This probe never
starts a daemon, loads a thread, or subscribes to conversation events. Loaded
identity is supporting evidence only: a daemon may retain an unsubscribed thread.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any

_MAX_MESSAGE = 1024 * 1024
_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _Connection:
    def __init__(self, connection: socket.socket, deadline: float):
        self.connection = connection
        self.deadline = deadline

    def _timeout(self) -> None:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Codex readiness deadline expired")
        self.connection.settimeout(remaining)

    def _read(self, count: int) -> bytes:
        result = bytearray()
        while len(result) < count:
            self._timeout()
            part = self.connection.recv(count - len(result))
            if not part:
                raise EOFError("Codex control socket closed")
            result.extend(part)
        return bytes(result)

    def _write(self, data: bytes) -> None:
        self._timeout()
        self.connection.sendall(data)

    def handshake(self) -> None:
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        self._write((
            "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
            "Connection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n\r\n"
        ).encode("ascii"))
        header = bytearray()
        while not header.endswith(b"\r\n\r\n"):
            if len(header) >= 8192:
                raise ValueError("Oversized WebSocket response header")
            header.extend(self._read(1))
        lines = header.decode("ascii").split("\r\n")
        if lines[0].split(" ", 2)[:2] != ["HTTP/1.1", "101"]:
            raise ValueError("Codex control socket did not upgrade")
        fields = dict(line.lower().split(":", 1) for line in lines[1:] if ":" in line)
        # The digest is case-sensitive, unlike header names.
        accept = next((line.split(":", 1)[1].strip() for line in lines[1:]
                       if line.lower().startswith("sec-websocket-accept:")), "")
        expected = base64.b64encode(hashlib.sha1((key + _WEBSOCKET_GUID).encode()).digest()).decode()
        if (accept != expected or fields.get("upgrade", "").strip() != "websocket"
                or "upgrade" not in fields.get("connection", "")):
            raise ValueError("Invalid WebSocket upgrade response")

    def _frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        length = len(payload)
        if length < 126:
            header = bytes((0x80 | opcode, 0x80 | length))
        elif length <= 65535:
            header = bytes((0x80 | opcode, 0xFE)) + struct.pack("!H", length)
        else:
            header = bytes((0x80 | opcode, 0xFF)) + struct.pack("!Q", length)
        self._write(header + mask + bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload)))

    def send(self, message: dict[str, Any]) -> None:
        self._frame(1, json.dumps(message).encode())

    def receive(self) -> dict[str, Any]:
        message = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 0x70 or second & 128:
                raise ValueError("Unsupported WebSocket frame")
            length = second & 127
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if length + len(message) > _MAX_MESSAGE:
                raise ValueError("Oversized Codex response")
            if opcode >= 8 and (not final or length > 125):
                raise ValueError("Invalid WebSocket control frame")
            payload = self._read(length)
            if opcode == 8:
                raise EOFError("Codex closed the WebSocket")
            if opcode == 9:
                self._frame(10, payload)
                continue
            if opcode == 10:
                continue
            if opcode not in (0, 1) or (opcode == 0) != started:
                raise ValueError("Unexpected WebSocket message")
            started = True
            message.extend(payload)
            if final:
                result = json.loads(message)
                if not isinstance(result, dict):
                    raise ValueError("Invalid Codex response")
                return result

    def call(self, identifier: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.send({"id": identifier, "method": method, "params": params})
        for _ in range(100):
            message = self.receive()
            if message.get("id") == identifier:
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex readiness request failed")
                return result
        raise ValueError("Too many unrelated Codex notifications")


def loaded_thread_ids(codex_home: Path | None = None, *, timeout: float = 1.0) -> set[str] | None:
    """Return loaded identities, or None if unavailable within the total deadline."""
    root = codex_home or Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    path = root / "app-server-control" / "app-server-control.sock"
    if timeout <= 0:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            client = _Connection(connection, time.monotonic() + timeout)
            client._timeout()
            connection.connect(str(path))
            client.handshake()
            client.call(1, "initialize", {"clientInfo": {"name": "wsctl_readiness", "version": "1"}})
            client.send({"method": "initialized"})
            identities: set[str] = set()
            cursor = None
            seen_cursors: set[str] = set()
            for identifier in range(2, 18):
                result = client.call(identifier, "thread/loaded/list", {"cursor": cursor})
                data = result.get("data")
                if not isinstance(data, list) or any(not isinstance(item, str) for item in data):
                    return None
                identities.update(data)
                cursor = result.get("nextCursor")
                if cursor is None:
                    return identities
                if not isinstance(cursor, str) or cursor in seen_cursors:
                    return None
                seen_cursors.add(cursor)
    except (OSError, EOFError, ValueError, UnicodeError):
        pass
    return None
