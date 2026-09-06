from __future__ import annotations

import fcntl
import json
import os
import selectors
import socket
import stat
import struct
import sys
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .browser import profile_socket_path, runtime_dir

MAX_HOST_TO_CHROME = 1024 * 1024
MAX_CHROME_TO_HOST = 64 * 1024 * 1024


def encode_native_message(value: object) -> bytes:
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_HOST_TO_CHROME:
        raise ValueError("native messaging payload is too large")
    return struct.pack("=I", len(payload)) + payload


def decode_native_messages(buffer: bytearray) -> list[Any]:
    messages = []
    while len(buffer) >= 4:
        length = struct.unpack("=I", buffer[:4])[0]
        if length > MAX_CHROME_TO_HOST:
            raise ValueError("native messaging payload is too large")
        if len(buffer) < 4 + length:
            break
        raw = bytes(buffer[4:4 + length])
        del buffer[:4 + length]
        messages.append(json.loads(raw))
    return messages


def _write_native(stream: BinaryIO, value: object) -> None:
    stream.write(encode_native_message(value))
    stream.flush()


def _resolved_profile(message: dict[str, Any]) -> tuple[str, str]:
    profile = str(message.get("profile") or "Default")
    directory = str(message.get("profileDirectory") or "Default")
    if message.get("profileConfigured"):
        return profile, directory
    email = str(message.get("profileEmail") or "").casefold()
    if not email:
        return profile, directory
    local_state = Path.home() / ".config/google-chrome/Local State"
    try:
        state = json.loads(local_state.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return profile, directory
    matches = [
        str(profile_directory)
        for profile_directory, info in state.get("profile", {}).get("info_cache", {}).items()
        if str(info.get("user_name") or "").casefold() == email
    ]
    if len(matches) == 1:
        return matches[0], matches[0]
    return profile, directory


def _existing_host_responds(path: Path, *, timeout: float = 2.0) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
        probe.sendall(b'{"action":"ping","payload":{}}\n')
        response = bytearray()
        while b"\n" not in response:
            chunk = probe.recv(65536)
            if not chunk:
                return False
            response.extend(chunk)
            if len(response) > MAX_HOST_TO_CHROME:
                return False
        value = json.loads(bytes(response).split(b"\n", 1)[0])
        return isinstance(value, dict) and "ok" in value
    except (OSError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    finally:
        probe.close()


def _socket_identity(value: socket.socket) -> tuple[int, int]:
    details = os.fstat(value.fileno())
    return details.st_dev, details.st_ino


def _unlink_owned_socket(path: Path, identity: tuple[int, int]) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISSOCK(details.st_mode) and (details.st_dev, details.st_ino) == identity:
        path.unlink()


def _acquire_profile_lock(profile: str) -> int:
    directory = runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = profile_socket_path(profile).with_suffix(".lock")
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    lock_fd = os.open(path, flags, 0o600)
    os.fchmod(lock_fd, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_fd)
        raise RuntimeError(
            f"native host already connected for Chrome profile {profile!r}",
        ) from None
    return lock_fd


def _prepare_listener(profile: str) -> tuple[socket.socket, Path, tuple[int, int]]:
    directory = runtime_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = profile_socket_path(profile)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None:
        if not stat.S_ISSOCK(mode):
            raise RuntimeError(f"refusing to replace non-socket native host path: {path}")
        if _existing_host_responds(path):
            raise RuntimeError(f"native host already connected for Chrome profile {profile!r}")
        path.unlink()
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen()
    listener.setblocking(False)
    return listener, path, _socket_identity(listener)


def serve(stdin: BinaryIO = sys.stdin.buffer, stdout: BinaryIO = sys.stdout.buffer) -> int:
    selector = selectors.DefaultSelector()
    selector.register(stdin, selectors.EVENT_READ, ("native", None))
    native_buffer = bytearray()
    listener: socket.socket | None = None
    socket_path: Path | None = None
    socket_identity: tuple[int, int] | None = None
    profile_lock_fd: int | None = None
    clients: dict[socket.socket, bytearray] = {}
    pending: dict[str, socket.socket] = {}

    try:
        while True:
            for key, _events in selector.select():
                kind, _value = key.data
                if kind == "native":
                    chunk = os.read(stdin.fileno(), 65536)
                    if not chunk:
                        return 0
                    native_buffer.extend(chunk)
                    for message in decode_native_messages(native_buffer):
                        if message.get("type") == "hello":
                            if listener is None:
                                profile, profile_directory = _resolved_profile(message)
                                profile_lock_fd = _acquire_profile_lock(profile)
                                listener, socket_path, socket_identity = _prepare_listener(profile)
                                selector.register(listener, selectors.EVENT_READ, ("listener", None))
                            _write_native(stdout, {
                                "type": "hello",
                                "ok": True,
                                "profile": profile,
                                "profileDirectory": profile_directory,
                            })
                            continue
                        request_id = str(message.get("id") or "")
                        client = pending.pop(request_id, None)
                        if client is None or client not in clients:
                            continue
                        try:
                            client.sendall(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")
                        except OSError:
                            pass
                        selector.unregister(client)
                        clients.pop(client, None)
                        client.close()
                elif kind == "listener" and listener is not None:
                    client, _address = listener.accept()
                    client.setblocking(True)
                    clients[client] = bytearray()
                    selector.register(client, selectors.EVENT_READ, ("client", None))
                elif kind == "client":
                    client = key.fileobj
                    try:
                        chunk = client.recv(65536)
                    except OSError:
                        chunk = b""
                    if not chunk:
                        selector.unregister(client)
                        clients.pop(client, None)
                        pending = {
                            request_id: waiting_client
                            for request_id, waiting_client in pending.items()
                            if waiting_client is not client
                        }
                        client.close()
                        continue
                    buffer = clients[client]
                    buffer.extend(chunk)
                    if len(buffer) > MAX_CHROME_TO_HOST:
                        client.sendall(b'{"ok":false,"error":"request is too large"}\n')
                        selector.unregister(client)
                        clients.pop(client, None)
                        client.close()
                        continue
                    if b"\n" not in buffer:
                        continue
                    raw = bytes(buffer).split(b"\n", 1)[0]
                    request_id = None
                    try:
                        message = json.loads(raw)
                        request_id = uuid.uuid4().hex
                        pending[request_id] = client
                        _write_native(stdout, {**message, "id": request_id})
                    except (ValueError, TypeError, json.JSONDecodeError) as error:
                        if request_id is not None:
                            pending.pop(request_id, None)
                        client.sendall(json.dumps({"ok": False, "error": str(error)}).encode() + b"\n")
                        selector.unregister(client)
                        clients.pop(client, None)
                        client.close()
    finally:
        for client in clients:
            client.close()
        if listener is not None:
            listener.close()
        if socket_path is not None and socket_identity is not None:
            _unlink_owned_socket(socket_path, socket_identity)
        if profile_lock_fd is not None:
            os.close(profile_lock_fd)


def main() -> int:
    try:
        return serve()
    except (BrokenPipeError, EOFError):
        return 0
    except Exception as error:
        print(f"workspace-state native host: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
