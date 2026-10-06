"""Serialize Codex initialization while retaining the pane's native terminal."""
from __future__ import annotations

import fcntl
from dataclasses import dataclass
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

from .capture import (
    _explicit_resume_uuid,
    _interactive_codex,
    _open_rollout_sessions,
    _pane_process_chain,
    _ProcessIdentity,
    _process_identity,
    _same_foreground_terminal,
)
from .codex_readiness import loaded_thread_ids
from .codex_directories import directory_ready, saved_cwd
from .login_status import runtime_root
from .util import atomic_json

START_TIMEOUT = 30.0
QUEUE_TIMEOUT = 600.0
MAX_ATTEMPTS = 3
DIRECTORY_TIMEOUT = 600.0
PROC_ROOT = Path("/proc")


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


@dataclass(frozen=True)
class _TerminalClient:
    chain: dict[int, _ProcessIdentity]
    argv: tuple[bytes, ...]
    row: int
    column: int
    in_mode: bool = False


def _client_unchanged(pid: int, client: _TerminalClient) -> bool:
    try:
        process = PROC_ROOT / str(pid)
        return (all(_process_identity(process_pid, PROC_ROOT) == identity
                    for process_pid, identity in client.chain.items())
                and (process / "comm").read_text().strip() == "codex"
                and tuple((process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")) == client.argv)
    except OSError:
        return False


def _terminal_client(pid: int, pane: str, *, allow_copy_mode: bool = False) -> _TerminalClient | None:
    """Bind an interactive foreground client to this pane across proc reads."""
    if not re.fullmatch(r"%\d+", pane):
        return None
    try:
        identity = _process_identity(pid, PROC_ROOT)
        process = PROC_ROOT / str(pid)
        argv = tuple((process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0"))
        if (identity is None or (process / "comm").read_text().strip() != "codex"
                or _interactive_codex(list(argv)) is not True):
            return None
        result = subprocess.run([
            "tmux", "display-message", "-p", "-t", pane,
            "#{pane_tty}\t#{cursor_y}\t#{cursor_x}\t#{pane_in_mode}\t#{pane_pid}",
        ], capture_output=True, text=True, timeout=1)
        if result.returncode:
            return None
        terminal, row, column, in_mode, pane_pid = result.stdout.strip().split("\t")
        pane_pid = int(pane_pid)
        pane_identity = _process_identity(pane_pid, PROC_ROOT)
        if (pane_identity is None or in_mode not in {"0", "1"}
                or (in_mode == "1" and not allow_copy_mode) or identity.stdin != terminal):
            return None
        chain = _pane_process_chain(pid, pane_pid, pane_identity, PROC_ROOT)
        if (chain is None or chain[pid] != identity
                or not _same_foreground_terminal(identity, pane_identity)):
            return None
        client = _TerminalClient(chain, argv, int(row), int(column), in_mode == "1")
        return client if _client_unchanged(pid, client) else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def _composer_layout(client: _TerminalClient, screen: str) -> bool:
    lines = screen.splitlines()
    if (not max(0, len(screen.rstrip().splitlines()) - 12) <= client.row < len(lines)
            or client.column < 2):
        return False
    composer = lines[client.row].lstrip()
    # Current renderers use › and »; other punctuation/symbol leaders are
    # safe only with the same cursor, terminal ownership and footer layout.
    if (not composer or unicodedata.category(composer[0])[0] not in {"P", "S"}
            or (len(composer) > 1 and not composer[1].isspace())):
        return False
    footer = "\n".join(lines[client.row + 1:client.row + 6])
    # Narrow panes omit usage hints, or clip their trailing "left". The model
    # and reasoning prefix remains a distinct footer even when the path clips.
    return bool(re.search(
        r"weekly.*left|context left|for shortcuts|"
        r"(?im:^[ \t]*gpt-\d[\w.-]*[ \t]+(?:default|none|minimal|low|medium|high|xhigh|max|ultra)[ \t]+·(?:[ \t]|$))",
        footer,
    ))


def _composer_ready(pid: int, pane: str, screen: str) -> bool:
    """Recognize the live terminal's composer layout, not one theme's glyph."""
    client = _terminal_client(pid, pane)
    return bool(client and not _startup_screen(screen) and _composer_layout(client, screen)
                and _client_unchanged(pid, client))


def _startup_screen(screen: str) -> bool:
    tail = "\n".join(screen.rstrip().splitlines()[-12:])
    return bool(re.search(
        r"Press enter to continue|Resuming session|model:[ \t]+loading|"
        r"Sign in (?:to Codex|with ChatGPT)|"
        r"(?im:^[ \t]*(?:ERROR|Error): failed to (?:initialize|load|resume)\b)",
        tail,
    ))


def _pane_title_snapshot(pane: str) -> tuple[str, int, str] | None:
    """Read raw title metadata; a failed read cannot hide a UUID conflict."""
    if not re.fullmatch(r"%\d+", pane):
        return None
    try:
        result = subprocess.run([
            "tmux", "display-message", "-p", "-t", pane,
            "#{pane_id}\t#{pane_pid}\t#{pane_title}",
        ], capture_output=True, text=True, timeout=1)
        fields = result.stdout.removesuffix("\n").split("\t", 2)
        if (result.returncode or len(fields) != 3 or fields[0] != pane
                or any(character in fields[2] for character in "\n\0")):
            return None
        pane_pid = int(fields[1])
        return (pane, pane_pid, fields[2]) if pane_pid > 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def resumed_session(pid: int, session_id: str, pane: str = "") -> bool:
    """Require actual thread ownership, never just a UUID in argv."""
    client = _terminal_client(pid, pane, allow_copy_mode=True)
    if client is None:
        return False
    owned = _open_rollout_sessions(pid, proc_root=PROC_ROOT)
    if owned:
        # Copy mode overlays history, not the live client's screen. Stable
        # rollout ownership proves this root independently of that overlay.
        return (owned == {session_id} and (client.in_mode or not _startup_screen(pane_text(pane)))
                and _open_rollout_sessions(pid, proc_root=PROC_ROOT) == owned
                and _client_unchanged(pid, client))
    if client.in_mode:
        return False
    # Daemon-backed TUIs do not own the rollout themselves. A loaded daemon
    # thread alone is insufficient: it can outlive its last attached client.
    # Import locally: the title proof reuses these foreground/composer guards.
    from . import codex_title

    title = _pane_title_snapshot(pane)
    if title is None or title[1] != tuple(client.chain)[-1]:
        return False
    native_title = bool(codex_title._UUID.fullmatch(title[2]) or codex_title._NATIVE_PREFIX.fullmatch(title[2]))
    if native_title:
        prefix = title[2][:-3] if title[2].endswith("...") else title[2]
        matches = (session_id.lower().startswith(prefix.lower()) if title[2].endswith("...")
                   else session_id.lower() == prefix.lower())
        if not matches:
            return False
    screen = pane_text(pane)
    if _startup_screen(screen) or not _composer_layout(client, screen):
        return False
    proof = codex_title.native_title_proof(pid, pane) if native_title else None
    if proof is not None:
        if (proof.session_id != session_id or proof.pid != pid or proof.pane_id != pane
                or proof.pane_pid != title[1] or proof.title != title[2]
                or proof.start_ticks != client.chain[pid].start_ticks):
            return False
    else:
        if _explicit_resume_uuid(list(client.argv)) != session_id:
            return False
        loaded = loaded_thread_ids() or set()
        # Ambiguous clipped titles veto argv too; the catalog never chooses
        # an identity independently of the exact resume command/proven title.
        if (session_id not in loaded or (native_title
                and codex_title._matched_title(title[2], loaded) != session_id.lower())):
            return False
    # The daemon probe can take a second. Re-read the visible screen and pane
    # after it, and reject PID reuse, a shell takeover or a foreground change.
    screen = pane_text(pane)
    latest = _terminal_client(pid, pane)
    return bool(latest and latest.chain == client.chain and latest.argv == client.argv
                and _pane_title_snapshot(pane) == title
                and not _startup_screen(screen) and _composer_layout(latest, screen)
                and not _open_rollout_sessions(pid, proc_root=PROC_ROOT)
                and _client_unchanged(pid, latest))


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
