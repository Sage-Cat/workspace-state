from __future__ import annotations

import fcntl
import json
import os
import selectors
import socket
import stat
import struct
import sys
import time
from dataclasses import dataclass, field
import uuid
from pathlib import Path
from typing import Any, BinaryIO

from .browser import profile_socket_path, runtime_dir
from .deployment import build_fingerprint

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
    # The kernel socket descriptor and its filesystem name have different
    # inodes. Cleanup must compare identities from the same namespace.
    details = Path(value.getsockname()).lstat()
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


MAX_CLIENTS = 128
CLIENT_READ_SECONDS = 5.0
CLIENT_REQUEST_SECONDS = 65.0
CLIENT_WRITE_SECONDS = 5.0
NATIVE_WRITE_SECONDS = 10.0
MAX_QUEUED_OUTPUT = MAX_CHROME_TO_HOST


@dataclass
class _Client:
    deadline: float
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    request_id: str | None = None


def serve(stdin: BinaryIO = sys.stdin.buffer, stdout: BinaryIO = sys.stdout.buffer) -> int:
    """One bounded request per client; no peer can block the selector loop."""
    selector = selectors.DefaultSelector()
    selector.register(stdin, selectors.EVENT_READ, ("native", None))
    native_buffer, native_outgoing = bytearray(), bytearray()
    native_write_deadline = None
    output_registered = False
    output_fd = stdout.fileno()
    previous_blocking = os.get_blocking(output_fd)
    os.set_blocking(output_fd, False)
    listener = None
    socket_path = socket_identity = profile_lock_fd = None
    clients: dict[socket.socket, _Client] = {}
    pending: dict[str, socket.socket] = {}

    def close_client(client):
        state = clients.pop(client, None)
        if state and state.request_id:
            pending.pop(state.request_id, None)
        try:
            selector.unregister(client)
        except (KeyError, ValueError):
            pass
        client.close()

    def queue_native(value):
        nonlocal output_registered, native_write_deadline
        frame = encode_native_message(value)
        if len(native_outgoing) + len(frame) > MAX_QUEUED_OUTPUT:
            raise ValueError("native output queue is full")
        if not native_outgoing:
            native_write_deadline = time.monotonic() + NATIVE_WRITE_SECONDS
        native_outgoing.extend(frame)
        if not output_registered:
            selector.register(output_fd, selectors.EVENT_WRITE, ("output", None))
            output_registered = True

    def reply(client, value):
        state = clients.get(client)
        if state is None:
            return
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\n"
        if len(payload) + sum(len(item.outgoing) for item in clients.values()) > MAX_QUEUED_OUTPUT:
            close_client(client)
            return
        state.outgoing = bytearray(payload)
        state.deadline = time.monotonic() + CLIENT_WRITE_SECONDS
        selector.modify(client, selectors.EVENT_READ | selectors.EVENT_WRITE, ("client", None))

    try:
        while True:
            now = time.monotonic()
            for client, state in list(clients.items()):
                if now >= state.deadline:
                    close_client(client)
            if native_write_deadline is not None and now >= native_write_deadline:
                return 1
            for key, events in selector.select(.1):
                kind, _ = key.data
                if kind == "output":
                    try:
                        written = os.write(output_fd, native_outgoing)
                    except BlockingIOError:
                        continue
                    except OSError:
                        return 0
                    del native_outgoing[:written]
                    if not native_outgoing:
                        selector.unregister(output_fd)
                        output_registered = False
                        native_write_deadline = None
                elif kind == "native":
                    chunk = os.read(stdin.fileno(), 65536)
                    if not chunk:
                        return 0
                    native_buffer.extend(chunk)
                    for message in decode_native_messages(native_buffer):
                        if not isinstance(message, dict):
                            raise ValueError("invalid native message")
                        if message.get("type") == "hello":
                            if listener is None:
                                profile, profile_directory = _resolved_profile(message)
                                profile_lock_fd = _acquire_profile_lock(profile)
                                listener, socket_path, socket_identity = _prepare_listener(profile)
                                selector.register(listener, selectors.EVENT_READ, ("listener", None))
                            queue_native({"type": "hello", "ok": True, "profile": profile,
                                          "profileDirectory": profile_directory})
                            continue
                        client = pending.pop(str(message.get("id") or ""), None)
                        if client is not None:
                            if isinstance(message.get("result"), dict):
                                message = {**message, "result": {**message["result"], "native_host_build": build_fingerprint()}}
                            reply(client, message)
                elif kind == "listener":
                    try:
                        client, _ = listener.accept()
                    except BlockingIOError:
                        continue
                    if len(clients) >= MAX_CLIENTS:
                        client.close()
                        continue
                    client.setblocking(False)
                    clients[client] = _Client(time.monotonic() + CLIENT_READ_SECONDS)
                    selector.register(client, selectors.EVENT_READ, ("client", None))
                elif kind == "client":
                    client = key.fileobj
                    state = clients.get(client)
                    if state is None:
                        continue
                    if events & selectors.EVENT_READ:
                        try:
                            chunk = client.recv(65536)
                        except BlockingIOError:
                            chunk = None
                        except OSError:
                            chunk = b""
                        if chunk == b"":
                            close_client(client)
                            continue
                        if chunk:
                            # Never redispatch the first frame when a client
                            # sends more bytes while its request is pending.
                            if state.request_id is not None or state.outgoing:
                                close_client(client)
                                continue
                            state.incoming.extend(chunk)
                            if len(state.incoming) > MAX_HOST_TO_CHROME:
                                reply(client, {"ok": False, "error": "request is too large"})
                            elif b"\n" in state.incoming:
                                raw, trailing = bytes(state.incoming).split(b"\n", 1)
                                try:
                                    message = json.loads(raw)
                                    if not isinstance(message, dict) or not isinstance(message.get("action"), str) or trailing.strip():
                                        raise ValueError("exactly one object request is required")
                                    request_id = uuid.uuid4().hex
                                    queue_native({**message, "id": request_id})
                                    state.request_id = request_id
                                    state.incoming.clear()
                                    state.deadline = time.monotonic() + CLIENT_REQUEST_SECONDS
                                    pending[request_id] = client
                                except (ValueError, TypeError) as error:
                                    reply(client, {"ok": False, "error": str(error)})
                    if client in clients and events & selectors.EVENT_WRITE and state.outgoing:
                        try:
                            written = client.send(state.outgoing)
                        except BlockingIOError:
                            continue
                        except OSError:
                            close_client(client)
                            continue
                        del state.outgoing[:written]
                        if not state.outgoing:
                            close_client(client)
    finally:
        for client in list(clients):
            close_client(client)
        selector.close()
        os.set_blocking(output_fd, previous_blocking)
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
