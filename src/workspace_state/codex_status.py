"""Opt-in native /status evidence for an explicitly authorized manual save.

This helper sends terminal input. It must never be called by automatic capture,
startup, or shutdown. Stable samples reduce races; they cannot lock out a user
typing concurrently. A refusal never clears input, interrupts, or retries.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import os
from pathlib import Path
import re
import subprocess
import time
from types import MappingProxyType

from . import codex_resume
from .capture import (
    _interactive_codex, _pane_process_chain, _process_identity,
    _same_foreground_terminal,
)
from .codex_readiness import loaded_thread_ids

_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
_TITLE_PREFIX = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{5}\.\.\.", re.I)
_HEADER = re.compile(r"  >_ OpenAI Codex \(v\d+\.\d+\.\d+(?:[-+][\w.-]+)?\)")
_SESSION = re.compile(r"  Session:[ \t]+(" + _UUID.pattern + r")", re.I)
_METADATA = "#{pane_id}\t#{pane_pid}\t#{pane_tty}\t#{pane_in_mode}\t#{cursor_y}\t#{cursor_x}\t#{pane_width}\t#{pane_height}\t#{pane_title}"


class StatusRefused(RuntimeError):
    """The authorized diagnostic could not establish safe, fresh evidence."""


class _Unsettled(StatusRefused):
    """The same owned terminal is between two render frames."""


@dataclass(frozen=True)
class _Snapshot:
    client: codex_resume._TerminalClient
    title: str
    screen: str
    width: int
    height: int


@dataclass(frozen=True)
class StatusProof:
    session_id: str
    pid: int
    pane_id: str
    client: codex_resume._TerminalClient
    title: str
    codex_home: Path
    catalog: frozenset[str]
    screen: str
    status_block: str
    width: int
    height: int

    def capture_record(self) -> dict[str, str | int]:
        identity = self.client.chain[self.pid]
        return {"pid": self.pid, "session_id": self.session_id,
                "confidence": "native-status", "start_ticks": str(identity.start_ticks),
                "tty": identity.stdin}


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise StatusRefused("Codex /status diagnostic deadline elapsed")
    return remaining


def _deadline(timeout: float) -> float:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise StatusRefused("Codex /status timeout must be finite and positive")
    return time.monotonic() + min(timeout, 5.0)


def _tmux(pane: str, arguments: list[str], deadline: float) -> str:
    try:
        result = subprocess.run(["tmux", arguments[0], "-t", pane, *arguments[1:]], capture_output=True,
                                text=True, timeout=min(0.3, _remaining(deadline)))
        if result.returncode:
            raise StatusRefused("Codex /status terminal operation failed")
        return result.stdout
    except (OSError, subprocess.TimeoutExpired) as error:
        raise StatusRefused("Codex /status terminal operation did not complete") from error


def _snapshot(pid: int, pane: str, deadline: float) -> _Snapshot:
    root = codex_resume.PROC_ROOT
    try:
        identity = _process_identity(pid, root)
        process = root / str(pid)
        argv = tuple((process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0"))
        if identity is None or (process / "comm").read_text().strip() != "codex" or not _interactive_codex(list(argv)):
            raise StatusRefused("Codex /status requires an interactive native client")
        metadata = _tmux(pane, ["display-message", "-p", _METADATA], deadline).removesuffix("\n")
        identifier, pane_pid, terminal, mode, row, column, width, height, title = metadata.split("\t", 8)
        pane_pid, row, column, width, height = int(pane_pid), int(row), int(column), int(width), int(height)
        if width <= 0 or height <= 0 or not 0 <= row < height or not 0 <= column < width:
            raise ValueError("invalid pane dimensions or cursor")
        pane_identity = _process_identity(pane_pid, root)
        if (identifier != pane or pane_pid <= 0 or mode != "0" or pane_identity is None
                or identity.stdin != terminal or not _same_foreground_terminal(identity, pane_identity)
                or any(character in title for character in "\n\0")):
            raise StatusRefused("Codex /status requires the owned foreground pane outside copy mode")
        chain = _pane_process_chain(pid, pane_pid, pane_identity, root)
        if chain is None or chain[pid] != identity:
            raise StatusRefused("Codex /status client ownership changed")
        client = codex_resume._TerminalClient(MappingProxyType(chain), argv, row, column)
        screen = _tmux(pane, ["capture-pane", "-p"], deadline)
        after = _tmux(pane, ["display-message", "-p", _METADATA], deadline).removesuffix("\n")
        if metadata != after:
            original, latest = metadata.split("\t", 8), after.split("\t", 8)
            if len(latest) == 9 and original[6:8] != latest[6:8]:
                raise StatusRefused("Codex /status pane resized during observation")
            if (len(latest) == 9 and original[:4] == latest[:4] and original[6:] == latest[6:]
                    and codex_resume._client_unchanged(pid, client)):
                raise _Unsettled("Codex /status terminal render has not settled")
            raise StatusRefused("Codex /status terminal ownership changed during observation")
        if not codex_resume._client_unchanged(pid, client):
            raise StatusRefused("Codex /status client ownership changed")
        return _Snapshot(client, title, screen, width, height)
    except (OSError, ValueError, IndexError) as error:
        raise StatusRefused("Codex /status client metadata is unavailable") from error


def _safe_composer(snapshot: _Snapshot, *, typed: bool = False) -> bool:
    client, screen = snapshot.client, snapshot.screen
    lines = screen.splitlines()
    if not 0 <= client.row < len(lines) or client.in_mode or not codex_resume._composer_layout(client, screen):
        return False
    expected = {"› /status", "» /status"} if typed else {"›", "»", "› Ask Codex to do anything", "» Ask Codex to do anything"}
    if lines[client.row].strip() not in expected or client.column != (9 if typed else 2):
        return False
    tail = "\n".join(lines[max(0, client.row - 12):client.row + 6])
    footer = "\n".join(lines[client.row + 1:client.row + 6])
    return not (codex_resume._startup_screen(screen) or re.search(r"[\u2801-\u28ff]", footer) or re.search(
        r"(?im:esc to (?:interrupt|cancel)|"
        r"^[ \t]*(?:[•·][ \t]*)?(?:Working|Thinking|Reconnecting|Loading)\b|"
        r"^[ \t]*(?:Would you like|Do you want|Select (?:a|an) |Choose (?:a|an) |Sign in )|"
        r"press (?:enter|return).*(?:continue|confirm))", tail))


def _same_owner(first: _Snapshot, second: _Snapshot) -> bool:
    return (first.client.chain == second.client.chain and first.client.argv == second.client.argv
            and first.title == second.title and (first.width, first.height) == (second.width, second.height))


def _require_same_size(first: _Snapshot, second: _Snapshot) -> None:
    if (first.width, first.height) != (second.width, second.height):
        raise StatusRefused("Codex /status pane resized during diagnostic")


def _log(snapshot: _Snapshot) -> tuple[str, ...]:
    lines = snapshot.screen.splitlines()[:snapshot.client.row]
    while lines and not lines[-1].strip():
        lines.pop()
    return tuple(lines)


def _typed_log(snapshot: _Snapshot) -> tuple[str, ...]:
    lines = _log(snapshot)
    # Native 0.160.1 adds its slash completion menu above the composer. Remove
    # only this exact /status menu; wrapped or other menus remain unsupported.
    if (len(lines) >= 2 and re.fullmatch(
            r"[›»] /status[ \t]+show current session configuration and token usage", lines[-2])
            and re.fullmatch(r"  /statusline[ \t]+configure which items appear in the status line", lines[-1])):
        lines = lines[:-2]
        while lines and not lines[-1].strip():
            lines = lines[:-1]
    return lines


def _typing_log_unchanged(before: _Snapshot, typed: _Snapshot) -> bool:
    old, current = _log(before), _typed_log(typed)
    # A two-row native completion menu can scroll two old rows off an already
    # full pane. Only exact suffix cropping is supported; a redraw of the
    # welcome logo or any newly appended output deliberately refuses.
    if current == old or (0 < len(old) - len(current) <= 2 and len(current) >= 4
                         and old[-len(current):] == current):
        return True
    # At a full alternate-screen viewport, the exact two-row completion menu
    # can overlay the previous native report's final Limits/Credits row. Accept
    # only this observed one-row redraw, with every preceding byte unchanged
    # and a complete native status report in the baseline. This is not identity
    # evidence; Enter must still produce a separately verified new full report.
    if (current == old[:-1] and _log(typed) != current and old
            and re.fullmatch(r"  (?:Limits|Credits):[ \t]+\S.*", old[-1])):
        start = next((index for index in range(len(old) - 1, -1, -1) if old[index] == "/status"), None)
        return start is not None and _block(old[start:]) is not None
    return False


def _block(lines: tuple[str, ...]) -> tuple[str, str] | None:
    while lines and not lines[0].strip():
        lines = lines[1:]
    if len(lines) < 8 or lines[0] != "/status" or lines[1] != "" or not _HEADER.fullmatch(lines[2]):
        return None
    body = lines[3:]
    if any(line and not line.startswith("  ") for line in body) or sum(bool(_HEADER.fullmatch(line)) for line in lines) != 1:
        return None
    matches = [_SESSION.fullmatch(line) for line in body if line.lstrip().startswith("Session:")]
    if len(matches) != 1 or matches[0] is None:
        return None
    # These anchors distinguish the native status report from an isolated UUID
    # or an echoed slash command. Authenticated reports may add account/limits.
    for field in ("Server:", "Model:", "Directory:"):
        if sum(line.startswith("  " + field) for line in body) != 1:
            return None
    # Account-backed native reports show Context window; unauthenticated local
    # providers show Token usage. Neither variant permits duplicate fields.
    if sum(line.startswith(("  Token usage:", "  Context window:")) for line in body) != 1:
        return None
    return matches[0].group(1).lower(), "\n".join(lines)


def _fresh_block(before: _Snapshot, after: _Snapshot) -> tuple[str, str] | None:
    old, current = _log(before), _log(after)
    # Use the longest exact overlap, without searching historical blocks for a
    # convenient match. Identical repeated screens cannot prove fresh output.
    overlap = next((size for size in range(min(len(old), len(current)), 0, -1)
                    if old[-size:] == current[:size]), 0)
    if overlap < 4 or sum(bool(line.strip()) for line in old[-overlap:]) < 2:
        return None
    return _block(current[overlap:])


def _home(pid: int) -> Path:
    try:
        with (codex_resume.PROC_ROOT / str(pid) / "environ").open("rb") as stream:
            raw = stream.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError("oversized environment")
        controls = dict(entry.split(b"=", 1) for entry in raw.split(b"\0")
                        if entry.startswith((b"CODEX_HOME=", b"HOME=")))
        native = controls.get(b"CODEX_HOME")
        root = Path(os.fsdecode(native)) if native is not None else Path(os.fsdecode(controls[b"HOME"])) / ".codex"
        if not root.is_absolute():
            raise ValueError("relative home")
        return Path(os.path.abspath(root))
    except (OSError, KeyError, ValueError) as error:
        raise StatusRefused("Codex /status native configuration location is unavailable") from error


def _catalog(root: Path, deadline: float) -> frozenset[str]:
    loaded = loaded_thread_ids(root, timeout=min(0.3, _remaining(deadline)))
    if not loaded or any(not _UUID.fullmatch(identity) for identity in loaded):
        raise StatusRefused("Codex /status loaded identity catalog is unavailable")
    return frozenset(identity.lower() for identity in loaded)


def _title_matches(title: str, catalog: frozenset[str], session_id: str | None = None) -> bool:
    if not (_UUID.fullmatch(title) or _TITLE_PREFIX.fullmatch(title)):
        return True
    prefix = title[:-3].lower() if title.endswith("...") else title.lower()
    matches = {identity for identity in catalog if identity.startswith(prefix)}
    return len(matches) == 1 and (session_id is None or matches == {session_id})


def _pause(deadline: float, duration: float = 0.05) -> None:
    time.sleep(min(duration, _remaining(deadline)))


def probe(pid: int, pane: str, timeout: float = 5.0) -> StatusProof:
    """Send one authorized /status diagnostic, or raise without clearing input."""
    deadline = _deadline(timeout)
    if (isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0
            or not isinstance(pane, str) or not re.fullmatch(r"%\d+", pane)):
        raise StatusRefused("Codex /status requires an exact native PID and pane ID")
    before = _snapshot(pid, pane, deadline)
    if not _safe_composer(before):
        raise StatusRefused("Codex /status requires an empty idle composer without pending input or a modal")
    _pause(deadline, 0.2)
    stable = _snapshot(pid, pane, deadline)
    _require_same_size(before, stable)
    if stable != before or not _safe_composer(stable):
        raise StatusRefused("Codex /status composer did not remain stable")
    root = _home(pid)
    catalog = _catalog(root, deadline)
    if not _title_matches(before.title, catalog):
        raise StatusRefused("Codex /status native title is unavailable or ambiguous")
    latest = _snapshot(pid, pane, deadline)
    _require_same_size(before, latest)
    if latest != before or not _safe_composer(latest):
        raise StatusRefused("Codex /status composer changed before input")
    _tmux(pane, ["send-keys", "-l", "/status"], deadline)
    while True:
        try:
            typed = _snapshot(pid, pane, deadline)
        except _Unsettled:
            _pause(deadline)
            continue
        _require_same_size(before, typed)
        if not _same_owner(before, typed) or not _typing_log_unchanged(before, typed):
            raise StatusRefused("Codex /status owner or output changed while rendering the command")
        if _safe_composer(typed, typed=True):
            _pause(deadline, 0.2)
            try:
                confirmed = _snapshot(pid, pane, deadline)
            except _Unsettled:
                _pause(deadline)
                continue
            _require_same_size(before, confirmed)
            if confirmed != typed or not _safe_composer(confirmed, typed=True):
                raise StatusRefused("Codex /status command changed before Enter")
            break
        if not _safe_composer(typed):
            raise StatusRefused("Codex /status command was not rendered exactly; input was left intact")
        _pause(deadline)
    _tmux(pane, ["send-keys", "Enter"], deadline)
    while True:
        try:
            current = _snapshot(pid, pane, deadline)
        except _Unsettled:
            _pause(deadline)
            continue
        _require_same_size(before, current)
        if not _same_owner(before, current):
            raise StatusRefused("Codex /status owner or native title changed")
        if _safe_composer(current):
            fresh = _fresh_block(before, current)
            if fresh is not None:
                identity, block = fresh
                loaded = _catalog(root, deadline)
                if identity not in loaded or not _title_matches(current.title, loaded, identity):
                    raise StatusRefused("Codex /status UUID conflicts with the loaded catalog or native title")
                proof = StatusProof(identity, pid, pane, replace(current.client, chain=MappingProxyType(dict(current.client.chain))),
                                    current.title, root, loaded, current.screen, block, current.width, current.height)
                if not _revalidate(proof, deadline):
                    raise StatusRefused("Codex /status evidence changed before verification")
                return proof
        _pause(deadline)


def _revalidate(proof: StatusProof, deadline: float) -> bool:
    if _home(proof.pid) != proof.codex_home or not codex_resume._client_unchanged(proof.pid, proof.client):
        return False
    current = _snapshot(proof.pid, proof.pane_id, deadline)
    if (current.client != proof.client or current.title != proof.title or current.screen != proof.screen
            or (current.width, current.height) != (proof.width, proof.height)
            or not _safe_composer(current)):
        return False
    lines = _log(current)
    block_lines = tuple(proof.status_block.splitlines())
    if lines[-len(block_lines):] != block_lines or _block(block_lines) != (proof.session_id, proof.status_block):
        return False
    loaded = _catalog(proof.codex_home, deadline)
    latest = _snapshot(proof.pid, proof.pane_id, deadline)
    return (proof.session_id in loaded
            and _title_matches(current.title, loaded, proof.session_id)
            and latest == current and _safe_composer(latest)
            and codex_resume._client_unchanged(proof.pid, proof.client))


def revalidate(proof: StatusProof) -> bool:
    """Read-only late validation; new output, input, layout or identity refuses."""
    try:
        return isinstance(proof, StatusProof) and _revalidate(proof, _deadline(1.0))
    except StatusRefused:
        return False
