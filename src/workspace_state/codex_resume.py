"""Serialize Codex initialization while retaining the pane's native terminal."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import unicodedata
import uuid

from .capture import _open_rollout_sessions
from .codex_readiness import loaded_thread_ids
from .codex_directories import directory_ready, saved_cwd
from .login_status import runtime_root
from .util import atomic_json

START_TIMEOUT = 30.0
QUEUE_TIMEOUT = 600.0
MAX_ATTEMPTS = 3
DIRECTORY_TIMEOUT = 600.0


def _process_stamp(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError):
        return None


def _record(session_id: str, state: str) -> None:
    atomic_json(runtime_root() / "codex-starts" / f"{session_id}.json", {
        "session_id": session_id, "state": state, "pid": os.getpid(),
        "start_ticks": _process_stamp(os.getpid()), "pane": os.environ.get("TMUX_PANE", ""),
    })


def _pending_ids(states: set[str]) -> set[str]:
    result = set()
    for path in (runtime_root() / "codex-starts").glob("*.json"):
        try:
            value = json.loads(path.read_text())
            stamp = _process_stamp(int(value["pid"]))
            if stamp and stamp == value["start_ticks"] and value["state"] in states:
                result.add(str(value["session_id"]))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def waiting_directory_ids() -> set[str]:
    return _pending_ids({"waiting-directory"})


def pending_start_ids() -> set[str]:
    return _pending_ids({"waiting-directory", "queued", "starting"})


def pane_text(pane: str, *, history: bool = False) -> str:
    if not re.fullmatch(r"%\d+", pane):
        return ""
    args = ["tmux", "capture-pane", "-p", "-t", pane]
    if history:
        args += ["-J", "-S", "-500"]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=1)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _composer_ready(pid: int, pane: str, screen: str) -> bool:
    """Recognize the live terminal's composer layout, not one theme's glyph."""
    if not re.fullmatch(r"%\d+", pane):
        return False
    try:
        result = subprocess.run([
            "tmux", "display-message", "-p", "-t", pane,
            "#{pane_tty}\t#{cursor_y}\t#{cursor_x}\t#{pane_in_mode}",
        ], capture_output=True, text=True, timeout=1)
        if result.returncode:
            return False
        terminal, row, column, in_mode = result.stdout.strip().split("\t")
        lines = screen.splitlines()
        row, column = int(row), int(column)
        if (in_mode != "0" or os.readlink(f"/proc/{pid}/fd/0") != terminal
                or not max(0, len(screen.rstrip().splitlines()) - 12) <= row < len(lines)
                or column < 2):
            return False
        composer = lines[row].lstrip()
        # Current renderers use › and »; other punctuation/symbol leaders are
        # safe only with the same cursor, terminal ownership and footer layout.
        if (not composer or unicodedata.category(composer[0])[0] not in {"P", "S"}
                or (len(composer) > 1 and not composer[1].isspace())):
            return False
        footer = "\n".join(lines[row + 1:row + 6])
        return bool(re.search(r"weekly.*left|context left|for shortcuts", footer))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def resumed_session(pid: int, session_id: str, pane: str = "") -> bool:
    """Require actual thread ownership, never just a UUID in argv."""
    owned = _open_rollout_sessions(pid)
    if owned:
        return owned == {session_id}
    # Daemon-backed TUIs do not own the rollout themselves. A loaded daemon
    # thread alone is insufficient: it can outlive its last attached client.
    screen = pane_text(pane)
    tail = "\n".join(screen.rstrip().splitlines()[-12:])
    if any(text in tail for text in ("Press enter to continue", "Resuming session", "model:       loading")):
        return False
    return _composer_ready(pid, pane, screen) and session_id in (loaded_thread_ids() or set())


def startup_lock_path() -> Path:
    home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).resolve()
    key = hashlib.sha256(os.fsencode(home)).hexdigest()[:20]
    root = runtime_root()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"codex-startup-{key}.lock"


def acquire_startup_lock(descriptor: int, timeout: float = QUEUE_TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)


def retryable_failure(output: str, marker: str) -> bool:
    # Never retry based on an earlier failure remaining in pane scrollback.
    if marker not in output:
        return False
    current = output.rsplit(marker, 1)[1]
    return bool(re.search(
        r"(?m)^ERROR: failed to initialize (?:sqlite local db|state runtime)"
        r"[^\n]*database is locked", current,
    ))


def resume(session_id: str) -> int:
    if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", session_id):
        print("wsctl: invalid Codex session UUID", file=sys.stderr)
        return 2
    pane = os.environ.get("TMUX_PANE", "")
    directory = saved_cwd(session_id)
    if directory is not None and not directory_ready(directory):
        _record(session_id, "waiting-directory")
        print(f"wsctl: waiting for saved Codex directory: {directory}", flush=True)
        deadline = time.monotonic() + DIRECTORY_TIMEOUT
        while not directory_ready(directory):
            if time.monotonic() >= deadline:
                _record(session_id, "failed")
                print("wsctl: saved directory unavailable; pane preserved", file=sys.stderr)
                return 1
            time.sleep(1)
    _record(session_id, "queued")
    descriptor = os.open(startup_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    child: subprocess.Popen | None = None
    cancelled = False
    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    try:
        print(f"wsctl: waiting for Codex startup slot: {session_id}", flush=True)
        if not acquire_startup_lock(descriptor):
            print("wsctl: Codex startup queue timed out; pane preserved", file=sys.stderr)
            return 1
        locked = True
        def terminate(_signum: int, _frame: object) -> None:
            nonlocal cancelled
            cancelled = True
            if child is not None and child.poll() is None:
                child.terminate()
        signal.signal(signal.SIGTERM, terminate)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if cancelled:
                return 143
            _record(session_id, "starting")
            marker = f"wsctl-start-{uuid.uuid4().hex}"
            print(f"wsctl: resuming {session_id}, attempt {attempt}/{MAX_ATTEMPTS} [{marker}]", flush=True)
            try:
                # All three terminal streams and the foreground process group
                # are inherited. No PTY relay, nested shell, or permission flags.
                # Inherit global config too: CLI overrides force embedded mode
                # and prevent Codex from using its shared background server.
                signal.signal(signal.SIGINT, old_int)
                child = subprocess.Popen([
                    "codex", "resume", "--no-alt-screen", session_id,
                ])
            except FileNotFoundError:
                print("wsctl: Codex is not installed; pane preserved", file=sys.stderr)
                return 127
            # SIGTERM can arrive inside Popen before its result is assigned to
            # child, when the handler has no new process to forward it to yet.
            if cancelled and child.poll() is None:
                child.terminate()
            # Ctrl-C belongs to the foreground Codex TUI, not its supervisor.
            signal.signal(signal.SIGINT, signal.SIG_IGN)
            deadline = time.monotonic() + START_TIMEOUT
            ready = False
            while child.poll() is None and time.monotonic() < deadline:
                if resumed_session(child.pid, session_id, pane):
                    ready = True
                    _record(session_id, "ready")
                    break
                time.sleep(0.2)
            if child.poll() is None:
                # A slow or unrecognized TUI is left intact; never kill or
                # relaunch a possibly live conversation to enforce a deadline.
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                locked = False
                if not ready:
                    _record(session_id, "unverified")
                    # The TUI owns the terminal now. Its input cursor can be
                    # anywhere, so a supervisor warning would corrupt the
                    # composer. The receipt exposes the unresolved state.
                result = child.wait()
                break
            result = child.returncode
            # With no live TUI left, Ctrl-C should cancel the supervisor's
            # classification/backoff rather than being silently discarded.
            signal.signal(signal.SIGINT, old_int)
            if ready or cancelled or result <= 0 or result in {130, 143} or attempt == MAX_ATTEMPTS or not retryable_failure(
                pane_text(pane, history=True), marker,
            ):
                break
            print("wsctl: transient Codex database lock; retrying startup", file=sys.stderr)
            time.sleep(attempt)
        if result:
            print(f"wsctl: Codex session {session_id} exited with status {result}; pane preserved", file=sys.stderr)
        return 143 if cancelled else result if result >= 0 else 128 - result
    finally:
        try:
            _record(session_id, "exited")
        finally:
            if locked:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


def main() -> int:
    return resume(sys.argv[1] if len(sys.argv) == 2 else "")


if __name__ == "__main__":
    raise SystemExit(main())
