from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .desktop import capture_shell, workspace_names
from .tmux_names import (
    read_pane_names, read_pane_rename_policy, read_window_names, tmux_runtime_identity,
)
from .util import CommandError, run

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
ROLLOUT_RE = re.compile(
    r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$",
    re.I,
)


def _rows(command: list[str], fields: int, errors: list[str] | None = None) -> list[list[str]]:
    try:
        output = run(command)
    except (CommandError, FileNotFoundError) as error:
        if errors is not None:
            errors.append(str(error))
        return []
    rows = []
    for line in output.splitlines():
        parts = line.split("\t", fields - 1)
        if len(parts) == fields:
            rows.append(parts)
    return rows


def _parse_proc_stat(raw: str) -> tuple[int, int]:
    """Return PPID and start ticks, allowing spaces in Linux process names."""
    closing = raw.rfind(")")
    if closing < 0:
        raise ValueError("invalid /proc stat record")
    fields = raw[closing + 1:].split()
    return int(fields[1]), int(fields[19])


def _proc_children(root_pid: int, *, proc_root: Path = Path("/proc")) -> list[int]:
    pairs: dict[int, list[int]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent, _started = _parse_proc_stat((entry / "stat").read_text())
            pairs.setdefault(parent, []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    found, stack, seen = [], [root_pid], {root_pid}
    while stack:
        for child in pairs.get(stack.pop(), []):
            if child not in seen:
                seen.add(child)
                found.append(child)
                stack.append(child)
    return found


@dataclass(frozen=True)
class _ProcessIdentity:
    parent: int
    start_ticks: int
    process_group: int
    terminal_session: int
    tty_number: int
    foreground_group: int
    stdin: str


def _process_identity(pid: int, proc_root: Path) -> _ProcessIdentity | None:
    try:
        process = proc_root / str(pid)
        raw = (process / "stat").read_text()
        parent, ticks = _parse_proc_stat(raw)
        fields = raw[raw.rfind(")") + 1:].split()
        return _ProcessIdentity(parent, ticks, int(fields[2]), int(fields[3]),
                                int(fields[4]), int(fields[5]),
                                str((process / "fd" / "0").readlink()))
    except (OSError, ValueError, IndexError):
        return None


def _pane_process_chain(pid: int, pane_pid: int, pane: _ProcessIdentity,
                        proc_root: Path) -> dict[int, _ProcessIdentity] | None:
    """Prove ancestry independently of the earlier, potentially stale scan."""
    chain: dict[int, _ProcessIdentity] = {}
    while pid not in chain:
        identity = _process_identity(pid, proc_root)
        if identity is None:
            return None
        chain[pid] = identity
        if pid == pane_pid:
            return chain if identity == pane else None
        pid = identity.parent
    return None


def _same_foreground_terminal(process: _ProcessIdentity, pane: _ProcessIdentity) -> bool:
    return (pane.tty_number != 0 and process.stdin == pane.stdin
            and process.tty_number == pane.tty_number
            and process.terminal_session == pane.terminal_session
            and process.process_group == pane.foreground_group
            and process.foreground_group == pane.foreground_group)


def _interactive_codex(argv: list[bytes]) -> bool | None:
    # Shared daemons and command helpers can inherit the pane's ancestry. Their
    # rollout descriptors do not establish ownership by its interactive client.
    noninteractive = {b"app-server", b"app-server-daemon", b"daemon", b"exec",
                      b"mcp-server", b"mcp", b"debug", b"completion", b"apply",
                      b"login", b"logout", b"features", b"cloud", b"review",
                      b"help", b"sandbox"}
    value_options = {b"-c", b"--config", b"-m", b"--model", b"-p", b"--profile",
                     b"-C", b"--cd", b"-s", b"--sandbox", b"-a", b"--ask-for-approval",
                     b"-i", b"--image", b"--enable", b"--disable", b"--local-provider",
                     b"--remote"}
    flag_options = {b"--no-alt-screen", b"--search", b"--full-auto", b"--oss",
                    b"--dangerously-bypass-approvals-and-sandbox"}
    position = 1
    while position < len(argv):
        argument = argv[position]
        if argument == b"--":
            return True  # Everything after the delimiter is literal prompt text.
        if not argument.startswith(b"-"):
            return argument not in noninteractive
        option, separator, _value = argument.partition(b"=")
        if option in value_options:
            if not separator and position + 1 >= len(argv):
                return None
            position += 1 if separator else 2
        elif argument in flag_options:
            position += 1
        elif argument in {b"-h", b"--help", b"-V", b"--version"}:
            return False
        else:
            return None  # Unknown option grammar cannot prove an interactive role.
    return True


def _rollout_root_session(path: Path) -> str | None:
    """Return the root conversation UUID recorded by a Codex rollout."""
    match = ROLLOUT_RE.fullmatch(path.name)
    if match is None:
        return None
    try:
        with path.open() as stream:
            first = json.loads(stream.readline())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(first, dict):
        return None
    payload = first.get("payload", {})
    if first.get("type") != "session_meta" or not isinstance(payload, dict):
        return None

    session_id = payload.get("session_id")
    if isinstance(session_id, str) and UUID_RE.fullmatch(session_id):
        return session_id

    # A subagent rollout filename identifies the child, not the resumable root
    # conversation. Older metadata without session_id therefore cannot safely
    # be contracted from a subagent descriptor.
    source = payload.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return None

    rollout_id = payload.get("id")
    if isinstance(rollout_id, str) and UUID_RE.fullmatch(rollout_id):
        return rollout_id
    return match.group(1)


def _open_rollout_sessions(pid: int, *, proc_root: Path = Path("/proc")) -> set[str]:
    """Read all proven root identities without discarding conflicting evidence."""
    sessions: set[str] = set()
    try:
        for descriptor in (proc_root / str(pid) / "fd").iterdir():
            try:
                target = descriptor.readlink()
            except OSError:
                continue
            if "sessions" not in target.parts:
                continue
            session_id = _rollout_root_session(target)
            if session_id:
                sessions.add(session_id)
    except OSError:
        pass
    return sessions


def _session_from_open_files(pid: int, *, proc_root: Path = Path("/proc")) -> str | None:
    """Read the one root conversation owned by a live Codex process."""
    sessions = _open_rollout_sessions(pid, proc_root=proc_root)
    return next(iter(sessions)) if len(sessions) == 1 else None


def _explicit_resume_uuid(argv: list[bytes]) -> str | None:
    """Recognize the canonical resume grammar, never UUIDs embedded in prompts.

    Unknown options intentionally remain unresolved instead of guessing which
    argument is their value. Rollout ownership remains stronger evidence.
    """
    if len(argv) < 3 or argv[1] != b"resume":
        return None
    arguments = argv[2:]
    if arguments and arguments[0] == b"--no-alt-screen":
        arguments = arguments[1:]
    if not arguments:
        return None
    candidate = arguments[0].decode(errors="replace")
    return candidate if UUID_RE.fullmatch(candidate) else None


def codex_for_pane(pane_pid: int, cwd: str, *, proc_root: Path = Path("/proc"),
                   pane_id: str = "") -> dict[str, Any] | None:
    pane = _process_identity(pane_pid, proc_root)
    if pane is None:
        return None
    candidates: list[dict[str, Any]] = []
    for pid in [pane_pid, *_proc_children(pane_pid, proc_root=proc_root)]:
        try:
            process = proc_root / str(pid)
            comm = (process / "comm").read_text().strip()
            if comm != "codex" and comm not in {"python", "python3"} and not comm.startswith("python3."):
                continue
            chain = _pane_process_chain(pid, pane_pid, pane, proc_root)
            if chain is None or not _same_foreground_terminal(chain[pid], pane):
                continue
            argv = (process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            wrapper = None
            interactive = False
            if comm != "codex":
                # A serialized restore may still be waiting for its directory
                # or startup gate. Its exact UUID must survive a checkpoint,
                # although it is not evidence of a resumed conversation yet.
                if len(argv) == 4 and argv[1:3] == [b"-m", b"workspace_state.codex_resume"]:
                    identity = argv[3].decode(errors="replace")
                    if UUID_RE.fullmatch(identity):
                        wrapper = identity
                if wrapper is None:
                    continue
            else:
                interactive = _interactive_codex(argv)
                if interactive is False:
                    continue
            candidates.append({"pid": pid, "process": chain[pid], "chain": chain,
                               "comm": comm, "argv": argv,
                               "owned": _open_rollout_sessions(pid, proc_root=proc_root) if comm == "codex" and interactive else set(),
                               "resume": _explicit_resume_uuid(argv) if comm == "codex" and interactive else None,
                               "wrapper": wrapper, "interactive": comm == "codex" and interactive is True})
        except OSError:
            continue
    # Revalidate every selected process and ancestor after reading its evidence.
    # A reused PID or a pane process replaced during capture cannot supply an ID.
    stable = []
    for candidate in candidates:
        process = proc_root / str(candidate["pid"])
        try:
            if (all(_process_identity(pid, proc_root) == identity
                    for pid, identity in candidate["chain"].items())
                    and (process / "comm").read_text().strip() == candidate["comm"]
                    and (process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0") == candidate["argv"]
                    and (not candidate["interactive"]
                         or _open_rollout_sessions(candidate["pid"], proc_root=proc_root) == candidate["owned"])):
                stable.append(candidate)
        except OSError:
            continue
    candidates = stable
    if not candidates:
        return None

    def unresolved(confidence: str) -> dict[str, Any]:
        candidate = next((item for item in candidates if item["wrapper"] is None), candidates[0])
        return {"pid": candidate["pid"], "session_id": None, "confidence": confidence,
                "start_ticks": str(candidate["process"].start_ticks), "tty": candidate["process"].stdin}

    sessions = set().union(*(candidate["owned"] for candidate in candidates))
    if len(sessions) > 1:
        return unresolved("conflicting-rollouts")
    if sessions:
        session_id = next(iter(sessions))
        owner = next(candidate for candidate in candidates if session_id in candidate["owned"])
        # An argv can describe an earlier thread of its own live client. Its
        # actual rollout wins, but a different candidate's conflicting identity
        # remains ambiguous rather than being silently discarded.
        if any((candidate["resume"] or candidate["wrapper"]) not in {None, session_id}
               for candidate in candidates if candidate is not owner):
            return unresolved("conflicting-identities")
        return {"pid": owner["pid"], "session_id": session_id, "confidence": "open-rollout"}

    # A shared daemon owns every rollout, so its descriptors cannot identify
    # a terminal. Only a launch-configured, foreground native title can bind a
    # fresh thread to this exact pane. Imported here to avoid the resume cycle.
    from . import codex_resume
    if pane_id and proc_root == codex_resume.PROC_ROOT:
        from .codex_title import native_title_proof, _pane_title
        proven = [(candidate, native_title_proof(candidate["pid"], pane_id))
                  for candidate in candidates if candidate["interactive"]]
        proven = [(candidate, proof) for candidate, proof in proven if proof is not None]
        if len({proof.session_id for _, proof in proven}) > 1:
            return unresolved("conflicting-native-titles")
        if proven:
            owner, proof = proven[0]
            if any((candidate["resume"] or candidate["wrapper"]) not in {None, proof.session_id}
                   for candidate in candidates if candidate["pid"] not in owner["chain"]):
                return unresolved("conflicting-identities")
            if native_title_proof(owner["pid"], pane_id) != proof:
                return unresolved("changed-native-title")
            return {"pid": owner["pid"], "session_id": proof.session_id,
                    "confidence": "native-thread-title"}
        # A changed native UUID title can veto an obsolete resume argv. It is
        # never sufficient to positively identify an unsupported older thread.
        claim = _pane_title(pane_id)
        if claim is not None:
            prefix = claim[2].removesuffix("...").lower()
            if any(identity and not identity.lower().startswith(prefix)
                   for candidate in candidates
                   for identity in [candidate["resume"] or candidate["wrapper"]]):
                return unresolved("changed-thread-unbound")
            from .codex_readiness import loaded_thread_ids
            loaded = loaded_thread_ids()
            if loaded is not None and len({identity.lower() for identity in loaded
                                           if UUID_RE.fullmatch(identity)
                                           and identity.lower().startswith(prefix)}) > 1:
                return unresolved("ambiguous-native-title")

    identities = {candidate["resume"] or candidate["wrapper"] for candidate in candidates}
    identities.discard(None)
    if len(identities) > 1:
        return unresolved("conflicting-identities")
    if identities:
        session_id = next(iter(identities))
        owner = (next((candidate for candidate in candidates if candidate["resume"] == session_id), None)
                 or next(candidate for candidate in candidates if candidate["wrapper"] == session_id))
        return {"pid": owner["pid"], "session_id": session_id,
                "confidence": "command-line" if owner["resume"] else "restore-wrapper"}
    # Directory contents and nearby creation times cannot bind independent
    # fresh clients to immutable UUIDs, especially with a shared app-server.
    return unresolved("unknown")


def _alacritty_ancestor(pid: int) -> int | None:
    visited: set[int] = set()
    while pid > 1 and pid not in visited:
        visited.add(pid)
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
            if comm == "alacritty":
                return pid
            pid, _started = _parse_proc_stat(Path(f"/proc/{pid}/stat").read_text())
        except (OSError, ValueError, IndexError):
            return None
    return None


def capture(*, shell: dict[str, Any] | None = None, names: list[str] | None = None) -> dict[str, Any]:
    shell = capture_shell() if shell is None else shell
    names = workspace_names() if names is None else names
    shell_windows = {int(w.get("pid", -1)): w for w in shell.get("windows", [])}
    tmux_errors: list[str] = []
    clients: list[dict[str, Any]] = []
    for client_pid, session_name, _tty in _rows(
        ["tmux", "list-clients", "-F", "#{client_pid}\t#{session_name}\t#{client_tty}"], 3,
        tmux_errors,
    ):
        alacritty_pid = _alacritty_ancestor(int(client_pid))
        if alacritty_pid is not None:
            placement = shell_windows.get(alacritty_pid)
            if placement is not None:
                placement = dict(placement)
                workspace = int(placement.get("workspace", -1))
                if 0 <= workspace < len(names):
                    placement["workspace_name"] = names[workspace]
            clients.append({
                "session": session_name,
                "alacritty_pid": alacritty_pid,
                "placement": placement,
            })

    clients_by_session: dict[str, list[dict[str, Any]]] = {}
    for client in clients:
        clients_by_session.setdefault(client["session"], []).append(client)

    sessions: dict[str, dict[str, Any]] = {}
    pane_format = "\t".join([
        # Window names may contain tabs/newlines. Read them separately after
        # resolving the stable ID instead of putting them in a delimited row.
        "#{session_name}", "#{window_index}", "#{window_id}", "#{window_layout}",
        "#{window_active}", "#{pane_index}", "#{pane_id}", "#{pane_pid}",
        "#{pane_current_path}", "#{pane_current_command}", "#{pane_active}",
        "#{window_id}",
    ])
    for row in _rows(["tmux", "list-panes", "-a", "-F", pane_format], 12, tmux_errors):
        session_name, win_idx, _window_hint, layout, win_active, pane_idx, pane_id, pane_pid, cwd, command, pane_active, window_id = row
        if session_name not in sessions:
            sessions[session_name] = {
                "name": session_name,
                "attached": session_name in clients_by_session,
                "placement": (clients_by_session.get(session_name) or [{}])[0].get("placement"),
                "tmux_identity": tmux_runtime_identity(session_name),
                "windows": {},
            }
        session = sessions[session_name]
        if win_idx not in session["windows"]:
            try:
                window_names = read_window_names(window_id)
            except (CommandError, OSError, ValueError) as error:
                tmux_errors.append(f"window names for {window_id}: {error}")
                # Do not publish a guessed or truncated name as a complete capture.
                continue
            session["windows"][win_idx] = {
                "index": int(win_idx), **window_names, "layout": layout,
                "active": win_active == "1", "panes": [],
            }
        window = session["windows"][win_idx]
        codex = codex_for_pane(int(pane_pid), cwd, pane_id=pane_id)
        try:
            pane_names = read_pane_names(pane_id, window_id=window_id)
            pane_names["allow_rename"] = read_pane_rename_policy(pane_id)
        except (CommandError, OSError, ValueError) as error:
            tmux_errors.append(f"pane names for {pane_id}: {error}")
            pane_names = {}
        window["panes"].append({
            "index": int(pane_idx), "id": pane_id, "cwd": cwd,
            "command": command, "active": pane_active == "1", "codex": codex,
            **pane_names,
        })

    normalized = []
    for session in sessions.values():
        session["windows"] = sorted(session["windows"].values(), key=lambda value: value["index"])
        normalized.append(session)
    return {
        "version": 2,
        "name": "current",
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "desktop": {
            "workspace_names": names,
            "shell_companion": shell.get("available", False),
            "monitors": shell.get("monitors", []),
        },
        "terminals": clients,
        "sessions": sorted(normalized, key=lambda value: value["name"]),
        "capture_errors": {"tmux": tmux_errors},
    }
