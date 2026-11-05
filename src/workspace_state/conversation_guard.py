"""Keep each saved conversation in at most one terminal client.

A resume launcher records itself as the claimant of one exact provider UUID
and refuses when another live claimant holds it, or when a tmux pane already
shows a live client for the same conversation. A claim names a process by PID
and start time, so it ends with that process. Exec keeps the PID, so a
launcher that becomes the client keeps the claim for exactly as long as the
client runs, and no lock descriptor leaks into the client's children.

A claim follows a process, not a thread: a client that switches threads keeps
its launcher's claim. That can only refuse a resume, never start a second one.
"""
from __future__ import annotations

from collections.abc import Collection
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Iterator

from .util import CommandError, run

CLAIM_DIRECTORY = "conversation-claims"
CLAIM_WAIT = 5.0
PANE_FORMAT = "\t".join([
    "#{pane_id}", "#{pane_pid}", "#{session_name}", "#{window_index}",
    "#{pane_index}", "#{pane_current_path}",
])
PROC_ROOT = Path("/proc")


def normalized_key(provider: str, session_id: str) -> str:
    return f"{provider}:{session_id.lower()}"


def claim_path(provider: str, session_id: str) -> Path:
    from .login_status import runtime_root
    root = runtime_root() / CLAIM_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"{provider}-{session_id.lower()}.json"


def _boot_id() -> str:
    try:
        return (PROC_ROOT / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _start_ticks(pid: int) -> int | None:
    from .capture import _parse_proc_stat
    try:
        return _parse_proc_stat((PROC_ROOT / str(pid) / "stat").read_text())[1]
    except (OSError, ValueError, IndexError):
        return None


def _own_record() -> dict[str, Any]:
    pid = os.getpid()
    return {"pid": pid, "start_ticks": _start_ticks(pid), "boot_id": _boot_id()}


def _alive(record: dict[str, Any]) -> bool:
    pid = record.get("pid")
    if not isinstance(pid, int) or pid <= 0 or record.get("boot_id") != _boot_id():
        return False
    ticks = record.get("start_ticks")
    return ticks is not None and _start_ticks(pid) == ticks


@contextmanager
def _locked(path: Path) -> Iterator[int]:
    """Serialize the short read-check-write of one claim record."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        deadline = time.monotonic() + CLAIM_WAIT
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("conversation claim is busy") from None
                time.sleep(0.05)
        yield descriptor
    finally:
        os.close(descriptor)


def _read(descriptor: int) -> dict[str, Any] | None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    raw = os.read(descriptor, 4096)
    try:
        value = json.loads(raw) if raw else None
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _write(descriptor: int, value: dict[str, Any] | None) -> None:
    os.ftruncate(descriptor, 0)
    if value is not None:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, json.dumps(value, sort_keys=True).encode())


def claim(provider: str, session_id: str) -> bool:
    """Claim the conversation for this process; False while a live claimant holds it."""
    own = _own_record()
    with _locked(claim_path(provider, session_id)) as descriptor:
        current = _read(descriptor)
        if current is not None and current != own and _alive(current):
            return False
        _write(descriptor, own)
        return True


def release(provider: str, session_id: str) -> None:
    """Drop this process's claim; a claim held by another process stays."""
    own = _own_record()
    with _locked(claim_path(provider, session_id)) as descriptor:
        if _read(descriptor) == own:
            _write(descriptor, None)


def _process_table() -> tuple[dict[int, list[int]], dict[int, str]]:
    from .capture import _parse_proc_stat
    children: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_text()
            parent, _started = _parse_proc_stat(raw)
        except (OSError, ValueError, IndexError):
            continue
        pid = int(entry.name)
        children.setdefault(parent, []).append(pid)
        names[pid] = raw[raw.find("(") + 1:raw.rfind(")")]
    return children, names


def _client_providers(pane_pid: int, children: dict[int, list[int]],
                      names: dict[int, str]) -> set[str]:
    """Return which providers could own this pane, from process names only."""
    found: set[str] = set()
    stack, seen = [pane_pid], {pane_pid}
    while stack:
        pid = stack.pop()
        name = names.get(pid, "")
        if name == "codex" or name == "python" or name.startswith("python3"):
            found.add("codex")  # Includes a waiting Codex launcher.
        elif name == "claude":
            found.add("claude")
        for child in children.get(pid, []):
            if child not in seen:
                seen.add(child)
                stack.append(child)
    return found


def _codex_identity(pane_pid: int, cwd: str, pane_id: str) -> tuple[str, str] | None:
    """Return (session ID or native title prefix, evidence) for a Codex pane.

    An argv names the thread a client started with. When the client's native
    title names another thread, it has moved on and no longer proves the argv
    thread. A foreground client without other evidence is known by its native
    title prefix. App-server probes stay in capture: every launcher builds
    this index.
    """
    from .capture import codex_for_pane
    from .codex_title import _pane_title
    found = codex_for_pane(pane_pid, cwd)
    if not found:
        return None
    session_id = found.get("session_id")
    evidence = str(found.get("confidence") or "")
    if session_id and evidence in {"open-rollout", "restore-wrapper"}:
        return str(session_id).lower(), evidence
    title = _pane_title(pane_id)
    prefix = (title[2].removesuffix("...").lower()
              if title is not None and title[1] == pane_pid else None)
    if session_id:
        if prefix is not None and not str(session_id).lower().startswith(prefix):
            return None
        return str(session_id).lower(), evidence
    if prefix is not None and evidence == "unknown":
        return (prefix if len(prefix) == 36 else prefix + "..."), "native-title"
    return None


def pane_identity(pane_pid: int, cwd: str, pane_id: str,
                  providers: Collection[str] = ("codex", "claude")) -> tuple[str, str, str] | None:
    """Return (provider, session ID or title prefix, evidence) from cheap evidence.

    Codex identity comes from an exact resume argv, an open rollout, a waiting
    launcher or the native title; Claude identity from its session hook
    receipt.
    """
    found = []
    if "codex" in providers:
        codex = _codex_identity(pane_pid, cwd, pane_id)
        if codex is not None:
            found.append(("codex", *codex))
    if "claude" in providers:
        from .claude_identity import claude_for_pane
        claude = claude_for_pane(pane_pid, cwd, pane_id=pane_id)
        if claude and claude.get("session_id"):
            found.append(("claude", str(claude["session_id"]).lower(), str(claude.get("confidence") or "")))
    return found[0] if len(found) == 1 else None


def matching(index: dict[str, Any], provider: str, session_id: str) -> list[Any]:
    """Return the flattened index entries of one exact conversation.

    Entries recorded under a native title prefix of the same provider match
    too: the 27 hexadecimal digits of a native prefix identify one thread.
    """
    key = normalized_key(provider, session_id)
    found = list(index.get(key, ()))
    for candidate, values in index.items():
        if candidate.endswith("...") and key.startswith(candidate[:-3]):
            found.extend(values)
    return found


def live_locations(*, exclude_sessions: Collection[str] = (), exclude_pane: str = "",
                   include_launchers: bool = True,
                   runner: Callable[[list[str]], str] = run) -> dict[str, list[str]]:
    """Map live provider-qualified conversations to their `session:window.pane`.

    Codex launchers that still wait to start a client count only when
    `include_launchers` is true. Concurrent launchers coordinate through
    `claim`, so a launcher looking for a real client excludes them.
    """
    try:
        output = runner(["tmux", "list-panes", "-a", "-F", PANE_FORMAT])
    except (CommandError, FileNotFoundError):
        return {}
    from .capture import process_snapshot
    children, names = _process_table()
    result: dict[str, list[str]] = {}
    with process_snapshot(children):
        for line in output.splitlines():
            parts = line.split("\t", 5)
            if len(parts) != 6:
                continue
            pane_id, pane_pid, session, window, pane, cwd = parts
            if pane_id == exclude_pane or session in exclude_sessions:
                continue
            try:
                providers = _client_providers(int(pane_pid), children, names)
                found = pane_identity(int(pane_pid), cwd, pane_id, providers) if providers else None
            except (OSError, ValueError):
                continue
            if found is None:
                continue
            provider, session_id, evidence = found
            if not include_launchers and evidence == "restore-wrapper":
                continue
            result.setdefault(normalized_key(provider, session_id), []).append(f"{session}:{window}.{pane}")
    return result


def running_elsewhere(provider: str, session_id: str, *, exclude_pane: str = "",
                      runner: Callable[[list[str]], str] = run) -> str | None:
    """Return where a live client already holds this conversation, if anywhere."""
    locations = live_locations(exclude_pane=exclude_pane, include_launchers=False, runner=runner)
    found = matching(locations, provider, session_id)
    return found[0] if found else None


def refuse_message(provider: str, session_id: str, location: str | None) -> str:
    label = "Codex" if provider == "codex" else "Claude"
    where = f"already runs in {location}" if location else "is already being resumed in another pane"
    return (f"wsctl: {label} conversation {session_id} {where}; "
            "no second client was started and this pane stays a shell")


def live_sessions(*, runner: Callable[[list[str]], str] = run) -> dict[str, set[str]]:
    """Map live clients (not waiting launchers) to the tmux sessions showing them."""
    return {
        key: {location.rsplit(":", 1)[0] for location in locations}
        for key, locations in live_locations(include_launchers=False, runner=runner).items()
    }
