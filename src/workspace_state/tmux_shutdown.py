"""Safely stop the user tmux server during an actual system shutdown."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import time
from pathlib import Path

SYSTEMCTL = "/usr/bin/systemctl"
COMMAND_TIMEOUT = 2.0
EXIT_TIMEOUT = 5.0
PROC = Path("/proc")
SO_ACCEPTCON = 0x10000  # /proc/net/unix flag of a listening socket


def _log(message: str) -> None:
    print(f"wsctl-tmux-shutdown: {message}")


def _system_is_stopping() -> bool:
    try:
        result = subprocess.run(
            [SYSTEMCTL, "is-system-running"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"system state unavailable; leaving tmux running ({error.__class__.__name__})")
        return False
    # systemctl may return non-zero for transitional states; stdout is the
    # authoritative state string and must match exactly.
    if result.stdout.strip() != "stopping":
        _log("system is not stopping; leaving tmux running")
        return False
    return True


def _socket_path(uid: int) -> Path:
    return Path(f"/tmp/tmux-{uid}/default")


def _socket_identity(path: Path, uid: int) -> tuple[int, int, int, int] | None:
    try:
        parent = path.parent.lstat()
        socket = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != uid
        or parent.st_mode & 0o077
        or not stat.S_ISSOCK(socket.st_mode)
        or socket.st_uid != uid
        # tmux's standard default socket is normally 0770. Group access is
        # unreachable behind the strictly owner-only parent directory; do not
        # reject the real default socket while accepting only custom sockets.
        or socket.st_mode & 0o002
    ):
        return None
    return (socket.st_dev, socket.st_ino, socket.st_uid, stat.S_IFMT(socket.st_mode))


class ServerLookupError(Exception):
    """The tmux server could not be identified from the socket alone."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind  # "not-found" or "ambiguous"


def _listening_inode(path: Path) -> int | None:
    """Return the inode of the socket listening on `path`, read from the kernel."""
    try:
        lines = (PROC / "net/unix").read_text().splitlines()
    except OSError:
        return None
    found: set[int] = set()
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 8 or fields[7] != str(path):
            continue
        try:
            flags = int(fields[3], 16)
            inode = int(fields[6])
        except ValueError:
            continue
        if flags & SO_ACCEPTCON and inode > 0:
            found.add(inode)
    return found.pop() if len(found) == 1 else None


def _process_uid_and_comm(pid: int) -> tuple[int, str] | None:
    try:
        process_uid: int | None = None
        for line in (PROC / str(pid) / "status").read_text().splitlines():
            if line.startswith("Uid:"):
                process_uid = int(line.split()[1])
                break
        comm = (PROC / str(pid) / "comm").read_text().strip()
    except (OSError, ValueError):
        return None
    return (process_uid, comm) if process_uid is not None else None


def _find_server(inode: int, uid: int) -> int:
    """Find the one tmux process of `uid` that holds the listening socket."""
    needle = f"socket:[{inode}]"
    holders: list[int] = []
    try:
        entries = [entry for entry in PROC.iterdir() if entry.name.isdigit()]
    except OSError as error:
        raise ServerLookupError("not-found", f"cannot read /proc ({error})") from error
    for entry in entries:
        pid = int(entry.name)
        identity = _process_uid_and_comm(pid)
        if identity is None or identity[0] != uid or not identity[1].startswith("tmux"):
            continue
        try:
            descriptors = list((entry / "fd").iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                if os.readlink(descriptor) == needle:
                    holders.append(pid)
                    break
            except OSError:
                continue
    if not holders:
        raise ServerLookupError("not-found", "no tmux process holds the listening socket")
    if len(holders) > 1:
        raise ServerLookupError("ambiguous", "several tmux processes hold the listening socket: "
                                + ", ".join(map(str, sorted(holders))))
    return holders[0]


def _is_server(pid: int, uid: int) -> bool:
    identity = _process_uid_and_comm(pid)
    return identity is not None and identity[0] == uid and identity[1].startswith("tmux")


def _wait_for_exit(pid: int, uid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if not _is_server(pid, uid):
            return True
        try:
            state = next(line for line in (PROC / str(pid) / "status").read_text().splitlines()
                         if line.startswith("State:"))
        except (OSError, StopIteration):
            return True
        if "Z" in state.split(":", 1)[1].split()[0]:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def run() -> int:
    """Run the stop hook; all refusal and absence paths are successful no-ops."""
    if not _system_is_stopping():
        return 0

    uid = os.getuid()
    socket_path = _socket_path(uid)
    identity = _socket_identity(socket_path, uid)
    if identity is None:
        _log("standard tmux socket absent or ownership/type validation failed")
        return 0

    # The server is found from the kernel's socket table, never by asking it:
    # a busy server cannot answer a client request in time.
    inode = _listening_inode(socket_path)
    if inode is None:
        _log("server not found: no single listening socket for the standard tmux socket")
        return 0
    try:
        pid = _find_server(inode, uid)
    except ServerLookupError as error:
        _log(f"server not found: {error}" if error.kind == "not-found"
             else f"ambiguous server: {error}; leaving tmux running")
        return 0

    # Re-check both socket identity and process identity after discovery, so a
    # replaced socket cannot cause a signal to reach a different server.
    if _socket_identity(socket_path, uid) != identity or not _is_server(pid, uid):
        _log("tmux socket or server changed during validation; leaving tmux running")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)  # tmux treats SIGTERM as kill-server
    except OSError as error:
        _log(f"could not signal the tmux server (pid {pid}): {error.__class__.__name__}")
        return 0
    if _wait_for_exit(pid, uid, EXIT_TIMEOUT):
        _log(f"tmux server stopped (pid {pid})")
    else:
        _log(f"timeout: tmux server (pid {pid}) did not exit within {EXIT_TIMEOUT:g}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
