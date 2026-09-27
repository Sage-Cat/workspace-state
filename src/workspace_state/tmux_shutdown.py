"""Safely stop the user tmux server during an actual system shutdown."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

SYSTEMCTL = "/usr/bin/systemctl"
TMUX = "/usr/bin/tmux"
COMMAND_TIMEOUT = 2.0


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


def _server_pid(path: Path) -> int | None:
    try:
        result = subprocess.run(
            [TMUX, "-S", str(path), "list-sessions", "-F", "#{pid}"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    values = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not values or any(not value.isdigit() for value in values):
        return None
    unique_values = set(values)
    if len(unique_values) != 1:
        return None
    pid = int(unique_values.pop())
    return pid if pid > 0 else None


def _is_tmux_server(pid: int, uid: int) -> bool:
    try:
        process_uid: int | None = None
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("Uid:"):
                process_uid = int(line.split()[1])
                break
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
    except (OSError, ValueError):
        return False
    return process_uid == uid and comm == "tmux: server"


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

    pid = _server_pid(socket_path)
    if pid is None or not _is_tmux_server(pid, uid):
        _log("tmux server identity validation failed; leaving tmux running")
        return 0

    # Re-check both socket identity and process identity after discovery, so a
    # replaced socket cannot cause a command to target a different server.
    if _socket_identity(socket_path, uid) != identity or not _is_tmux_server(pid, uid):
        _log("tmux socket or server changed during validation; leaving tmux running")
        return 0
    try:
        result = subprocess.run(
            [TMUX, "-S", str(socket_path), "kill-server"],
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        _log(f"tmux kill-server failed ({error.__class__.__name__})")
        return 0
    if result.returncode == 0:
        _log(f"requested normal tmux server shutdown (pid {pid})")
    else:
        _log("tmux kill-server returned an error; no signal was sent by wsctl")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
