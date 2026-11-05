"""Bind Claude's hook-reported conversation UUID to one live tmux pane."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from .capture import (
    UUID_RE, _pane_process_chain, _parse_proc_stat, _proc_children,
    _process_identity, _same_foreground_terminal,
)
from . import conversation_guard
from .login_status import runtime_root
from .util import atomic_json


PANE_RE = re.compile(r"%[0-9]+\Z")
MAX_HOOK_INPUT = 65536
MAX_TRANSCRIPT_PROOF_BYTES = 262144


def _receipt_dir() -> Path:
    return runtime_root() / "claude-sessions"


def _receipt_path(pane_id: str) -> Path:
    if PANE_RE.fullmatch(pane_id) is None:
        raise ValueError("invalid tmux pane ID")
    return _receipt_dir() / f"{pane_id[1:]}.json"


def _argv(pid: int, proc_root: Path) -> tuple[bytes, ...]:
    return tuple((proc_root / str(pid) / "cmdline").read_bytes().rstrip(b"\0").split(b"\0"))


def _interactive(argv: tuple[bytes, ...]) -> bool:
    if not argv or Path(os.fsdecode(argv[0])).name != "claude":
        return False
    arguments = argv[1:]
    if not arguments:
        return True
    noninteractive = {
        b"-p", b"--print", b"--output-format", b"--input-format",
        b"--json-schema", b"--help", b"-h", b"--version", b"-v",
    }
    subcommands = {
        b"auth", b"agents", b"attach", b"auto-mode", b"daemon", b"doctor",
        b"gateway", b"import", b"install", b"logs", b"mcp", b"plugin",
        b"plugins", b"purge", b"remote-control", b"respawn", b"rm",
        b"self-hosted-runner", b"setup-token", b"stop", b"kill", b"update",
    }
    if arguments[0] in subcommands:
        return False
    return not any(arg.split(b"=", 1)[0] in noninteractive for arg in arguments)


def _claude_ancestor(hook_pid: int, proc_root: Path) -> tuple[int, Any, tuple[bytes, ...]] | None:
    """Find the Claude process that invoked this hook, never a sibling client."""
    pid = hook_pid
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(seen) < 64:
        seen.add(pid)
        try:
            parent, _started = _parse_proc_stat((proc_root / str(pid) / "stat").read_text())
            pid = parent
            if (proc_root / str(pid) / "comm").read_text().strip() != "claude":
                continue
            identity = _process_identity(pid, proc_root)
            argv = _argv(pid, proc_root)
            if (identity is not None and identity.tty_number != 0
                    and identity.stdin.startswith("/dev/pts/") and _interactive(argv)):
                return pid, identity, argv
        except (OSError, ValueError, IndexError):
            return None
    return None


def _transcript_ready(path_value: Any, session_id: str) -> bool:
    if not isinstance(path_value, str):
        return False
    path = Path(path_value)
    if not path.is_absolute() or path.name.lower() != f"{session_id.lower()}.jsonl":
        return False
    # A filename alone is not a durable conversation. A fresh client can
    # create an empty transcript before its first persisted record arrives.
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size == 0):
                return False
            raw = stream.read(MAX_TRANSCRIPT_PROOF_BYTES)
            for line in raw.splitlines():
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if (isinstance(record, dict)
                        and str(record.get("sessionId", "")).lower() == session_id.lower()):
                    return True
    except OSError:
        return False
    return False


def _hook_lock_path() -> Path:
    return _receipt_dir() / ".lock"


def _record_event(payload: dict[str, Any], event: str, *, proc_root: Path,
                  hook_pid: int, environ: dict[str, str]) -> bool:
    pane_id = environ.get("TMUX_PANE", "")
    session_id = payload.get("session_id")
    if (payload.get("hook_event_name") != event
            or PANE_RE.fullmatch(pane_id) is None
            or not isinstance(session_id, str) or UUID_RE.fullmatch(session_id) is None
            or payload.get("agent_type")):
        return False
    owner = _claude_ancestor(hook_pid, proc_root)
    event_identity = _process_identity(hook_pid, proc_root)
    if owner is None or event_identity is None:
        return False
    pid, identity, argv = owner
    record = {
        "version": 1, "provider": "claude", "pane_id": pane_id,
        "pid": pid, "start_ticks": identity.start_ticks,
        "tty": identity.stdin, "tty_number": identity.tty_number,
        "terminal_session": identity.terminal_session,
        "process_group": identity.process_group,
        "event_start_ticks": event_identity.start_ticks,
        "session_id": session_id,
        "transcript_path": payload.get("transcript_path") if event == "SessionStart" else None,
    }
    directory = _receipt_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    descriptor = os.open(_hook_lock_path(), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        path = _receipt_path(pane_id)
        try:
            existing = json.loads(path.read_text())
        except (OSError, ValueError):
            existing = None
        same_client = (isinstance(existing, dict) and all(existing.get(key) == record[key]
                       for key in ("pid", "start_ticks", "pane_id")))
        previous_tick = existing.get("event_start_ticks") if same_client else None
        if event == "SessionEnd":
            # An old SessionEnd must not erase a newer SessionStart in this pane.
            if same_client and existing.get("session_id") == session_id:
                if isinstance(previous_tick, int) and previous_tick > record["event_start_ticks"]:
                    return False
                record["session_id"] = None
                atomic_json(path, record)  # Keep a tombstone against late starts.
                return True
            return False
        if (_process_identity(pid, proc_root) != identity
                or _argv(pid, proc_root) != argv):
            return False
        if same_client and isinstance(previous_tick, int):
            if previous_tick > record["event_start_ticks"]:
                return False  # A newer SessionStart or SessionEnd already won.
            if previous_tick == record["event_start_ticks"]:
                if existing.get("session_id") != session_id:
                    # Process ticks have finite resolution. An ambiguous order
                    # cannot authorize either UUID, including after SessionEnd.
                    record["session_id"] = None
                    record["transcript_path"] = None
                    atomic_json(path, record)
                    return False
                if existing.get("session_id") is None:
                    return False
        atomic_json(path, record)
        return True
    finally:
        os.close(descriptor)


def record_session_start(payload: dict[str, Any], *, proc_root: Path = Path("/proc"),
                         hook_pid: int | None = None,
                         environ: dict[str, str] | None = None) -> bool:
    return _record_event(payload, "SessionStart", proc_root=proc_root,
                         hook_pid=os.getpid() if hook_pid is None else hook_pid,
                         environ=os.environ if environ is None else environ)


def record_session_end(payload: dict[str, Any], *, proc_root: Path = Path("/proc"),
                       hook_pid: int | None = None,
                       environ: dict[str, str] | None = None) -> bool:
    return _record_event(payload, "SessionEnd", proc_root=proc_root,
                         hook_pid=os.getpid() if hook_pid is None else hook_pid,
                         environ=os.environ if environ is None else environ)


def _read_receipt(pane_id: str) -> dict[str, Any] | None:
    try:
        path = _receipt_path(pane_id)
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) and value.get("version") == 1 else None


def _matches_receipt(receipt: dict[str, Any], pane_id: str, pid: int,
                     identity: Any) -> bool:
    return (receipt.get("provider") == "claude" and receipt.get("pane_id") == pane_id
            and receipt.get("pid") == pid
            and receipt.get("start_ticks") == identity.start_ticks
            and receipt.get("tty") == identity.stdin
            and receipt.get("tty_number") == identity.tty_number
            and receipt.get("terminal_session") == identity.terminal_session
            and receipt.get("process_group") == identity.process_group
            and isinstance(receipt.get("session_id"), str)
            and UUID_RE.fullmatch(receipt["session_id"]) is not None)


def _claimed_resume(pid: int, identity: Any, argv: tuple[bytes, ...],
                    proc_root: Path) -> str | None:
    """Return the UUID of a client that exactly matches its launcher's claim.

    wsctl claims the conversation and then execs `claude --resume UUID`, so
    the client keeps the claimant's PID and start time. wsctl resumes only
    persisted conversations.
    """
    if (len(argv) != 3 or Path(os.fsdecode(argv[0])).name != "claude"
            or argv[1] != b"--resume"):
        return None
    session_id = os.fsdecode(argv[2]).lower()
    if UUID_RE.fullmatch(session_id) is None:
        return None
    try:
        boot_id = (proc_root / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    claim = conversation_guard.read_claim("claude", session_id)
    if (not boot_id or claim != {"pid": pid, "start_ticks": identity.start_ticks,
                                 "boot_id": boot_id}):
        return None
    return session_id


def claude_for_pane(pane_pid: int, cwd: str, *, proc_root: Path = Path("/proc"),
                   pane_id: str = "") -> dict[str, Any] | None:
    pane = _process_identity(pane_pid, proc_root)
    if pane is None:
        return None
    candidates: list[tuple[int, Any, tuple[bytes, ...], dict[int, Any]]] = []
    for pid in [pane_pid, *_proc_children(pane_pid, proc_root=proc_root)]:
        try:
            process = proc_root / str(pid)
            if (process / "comm").read_text().strip() != "claude":
                continue
            argv = _argv(pid, proc_root)
            if not _interactive(argv):
                continue
            chain = _pane_process_chain(pid, pane_pid, pane, proc_root)
            if chain is not None and _same_foreground_terminal(chain[pid], pane):
                candidates.append((pid, chain[pid], argv, chain))
        except OSError:
            continue
    if not candidates:
        return None
    candidate = candidates[0]

    def unresolved(reason: str) -> dict[str, Any]:
        return {"pid": candidate[0], "session_id": None, "confidence": reason,
                "start_ticks": str(candidate[1].start_ticks), "tty": candidate[1].stdin,
                "persisted": False}

    if len(candidates) != 1 or PANE_RE.fullmatch(pane_id) is None:
        return unresolved("ambiguous-client" if len(candidates) != 1 else "missing-pane")
    pid, identity, argv, chain = candidate
    receipt = _read_receipt(pane_id)
    if receipt is None or not _matches_receipt(receipt, pane_id, pid, identity):
        resumed = _claimed_resume(pid, identity, argv, proc_root)
        if resumed is None:
            return unresolved("missing-hook-receipt")
        if (any(_process_identity(ancestor, proc_root) != stamp for ancestor, stamp in chain.items())
                or _argv(pid, proc_root) != argv):
            # No receipt was involved, so nothing "changed" in one.
            return unresolved("missing-hook-receipt")
        return {"pid": pid, "session_id": resumed, "confidence": "resume-claim",
                "persisted": True, "start_ticks": str(identity.start_ticks)}
    if (any(_process_identity(ancestor, proc_root) != stamp for ancestor, stamp in chain.items())
            or _argv(pid, proc_root) != argv
            or _read_receipt(pane_id) != receipt):
        return unresolved("changed-hook-receipt")
    session_id = receipt["session_id"]
    return {"pid": pid, "session_id": session_id, "confidence": "session-start-hook",
            "persisted": _transcript_ready(receipt.get("transcript_path"), session_id),
            "start_ticks": str(identity.start_ticks)}


def resumed_session(pid: int, session_id: str, pane_id: str,
                    *, proc_root: Path = Path("/proc")) -> bool:
    """Require a loaded, durable exact Claude session in its current pane."""
    if UUID_RE.fullmatch(session_id) is None or PANE_RE.fullmatch(pane_id) is None:
        return False
    try:
        result = subprocess.run([
            "tmux", "display-message", "-p", "-t", pane_id,
            "#{pane_id}\t#{pane_pid}\t#{pane_current_path}",
        ], capture_output=True, text=True, timeout=1)
        fields = result.stdout.removesuffix("\n").split("\t", 2)
        if result.returncode or len(fields) != 3 or fields[0] != pane_id:
            return False
        pane_pid = int(fields[1])
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False
    proof = claude_for_pane(pane_pid, fields[2], proc_root=proc_root, pane_id=pane_id)
    return bool(proof and proof.get("pid") == pid
                and proof.get("confidence") == "session-start-hook"
                and proof.get("session_id") == session_id and proof.get("persisted"))


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in (["start"], ["end"]):
        return 2
    try:
        raw = sys.stdin.buffer.read(MAX_HOOK_INPUT + 1)
        if len(raw) > MAX_HOOK_INPUT:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        if arguments == ["start"]:
            record_session_start(payload)
        else:
            record_session_end(payload)
    except (OSError, ValueError, TypeError, UnicodeError):
        # A missing receipt degrades a later checkpoint; never interrupt Claude.
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
