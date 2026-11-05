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
import stat
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
# Evidence strong enough to refuse a second client but never to verify one.
REFUSE_ONLY = frozenset({"native-title", "title-conflict"})
# Evidence of a launcher rather than a hook- or rollout-verified client.
LAUNCHER_EVIDENCE = frozenset({"restore-wrapper", "resume-claim"})


def normalized_key(provider: str, session_id: str) -> str:
    return f"{provider}:{session_id.lower()}"


def claim_path(provider: str, session_id: str) -> Path:
    from .login_status import runtime_root
    root = runtime_root() / CLAIM_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / f"{provider}-{session_id.lower()}.json"


def read_claim(provider: str, session_id: str) -> dict[str, Any] | None:
    """Read one claim record without creating directories or taking its lock."""
    from .login_status import runtime_root
    path = runtime_root() / CLAIM_DIRECTORY / f"{provider}-{session_id.lower()}.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077):
                return None
            value = json.loads(stream.read(4096))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


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


def _codex_identities(pane_pid: int, cwd: str, pane_id: str) -> list[tuple[str, str]]:
    """Return (session ID or native title prefix, evidence) pairs for a Codex pane.

    An argv names the thread a client started with. A native title naming
    another thread means the client moved on, or the title is stale from an
    earlier client: both threads then refuse a second client, and neither
    counts as verified. A foreground client without other evidence is known
    by its native title prefix, which can likewise only refuse. App-server
    probes stay in capture: every launcher builds this index.
    """
    from .capture import codex_for_pane
    from .codex_title import _pane_title
    found = codex_for_pane(pane_pid, cwd)
    if not found:
        return []
    session_id = str(found.get("session_id") or "").lower()
    evidence = str(found.get("confidence") or "")
    if session_id and evidence in {"open-rollout", "restore-wrapper"}:
        return [(session_id, evidence)]
    title = _pane_title(pane_id)
    prefix = (title[2].removesuffix("...").lower()
              if title is not None and title[1] == pane_pid else None)
    titled = (prefix if prefix is None or len(prefix) == 36 else prefix + "...")
    if session_id:
        if prefix is not None and not session_id.startswith(prefix):
            return [(session_id, "title-conflict"), (titled, "native-title")]
        return [(session_id, evidence)]
    if titled is not None and evidence == "unknown":
        return [(titled, "native-title")]
    return []


def pane_identities(pane_pid: int, cwd: str, pane_id: str,
                    providers: Collection[str] = ("codex", "claude")) -> list[tuple[str, str, str]]:
    """Return (provider, session ID or title prefix, evidence) from cheap evidence.

    Codex identity comes from an exact resume argv, an open rollout, a waiting
    launcher or the native title; Claude identity from its session hook
    receipt, or from the exact resume argv of a client that its launcher
    claimed (`resume-claim`). Clients of both providers in one pane prove neither.
    """
    codex = _codex_identities(pane_pid, cwd, pane_id) if "codex" in providers else []
    claude = []
    if "claude" in providers:
        from .claude_identity import claude_for_pane
        found = claude_for_pane(pane_pid, cwd, pane_id=pane_id)
        if found and found.get("session_id"):
            claude = [(str(found["session_id"]).lower(), str(found.get("confidence") or ""))]
    if codex and claude:
        return []
    return [("codex", *item) for item in codex] + [("claude", *item) for item in claude]


def matching(index: dict[str, Any], provider: str, session_id: str) -> list[Any]:
    """Return the flattened index entries of one exact conversation.

    Entries recorded under a native title prefix of the same provider match
    too. A prefix keeps 25 hexadecimal digits, so an accidental match is
    negligible, and such an entry can only refuse a second client.
    """
    key = normalized_key(provider, session_id)
    found = list(index.get(key, ()))
    for candidate, values in index.items():
        if candidate.endswith("...") and key.startswith(candidate[:-3]):
            found.extend(values)
    return found


def live_locations(*, exclude_sessions: Collection[str] = (), exclude_pane: str = "",
                   include_launchers: bool = True, verified_only: bool = False,
                   runner: Callable[[list[str]], str] = run) -> dict[str, list[str]]:
    """Map live provider-qualified conversations to their `session:window.pane`.

    Codex launchers that still wait to start a client (`restore-wrapper`) and
    Claude clients known only by their launcher's claim (`resume-claim`) count
    only when `include_launchers` is true. Concurrent launchers coordinate through
    `claim`, so a launcher looking for a real client excludes them.
    `verified_only` drops evidence that may refuse but never verify.
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
                found = pane_identities(int(pane_pid), cwd, pane_id, providers) if providers else []
            except (OSError, ValueError):
                continue
            for provider, session_id, evidence in found:
                if ((not include_launchers and evidence in LAUNCHER_EVIDENCE)
                        or (verified_only and evidence in REFUSE_ONLY)):
                    continue
                result.setdefault(normalized_key(provider, session_id), []).append(
                    f"{session}:{window}.{pane}")
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
    """Map verified live clients to the tmux sessions showing them.

    Waiting launchers and title-only evidence do not count: verification must
    never accept a conversation that only a native title suggests.
    """
    return {
        key: {location.rsplit(":", 1)[0] for location in locations}
        for key, locations in live_locations(include_launchers=False, verified_only=True,
                                             runner=runner).items()
    }
