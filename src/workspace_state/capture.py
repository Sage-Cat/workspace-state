from __future__ import annotations

import json
import os
import re
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .desktop import capture_shell, workspace_names
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


def _proc_children(root_pid: int) -> list[int]:
    pairs: dict[int, list[int]] = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent, _started = _parse_proc_stat((entry / "stat").read_text())
            pairs.setdefault(parent, []).append(int(entry.name))
        except (OSError, ValueError, IndexError):
            continue
    found, stack = [], [root_pid]
    while stack:
        children = pairs.get(stack.pop(), [])
        found.extend(children)
        stack.extend(children)
    return found


def _process_start(pid: int) -> float | None:
    try:
        _parent, ticks = _parse_proc_stat(Path(f"/proc/{pid}/stat").read_text())
        boot = float(next(line.split()[1] for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime ")))
        return boot + ticks / os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    except (OSError, ValueError, StopIteration, IndexError):
        return None


def _session_candidates(cwd: str) -> list[tuple[float, str]]:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions"
    candidates: list[tuple[float, str]] = []
    for path in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            first = json.loads(path.open().readline())
            payload = first.get("payload", {})
            if first.get("type") != "session_meta" or payload.get("cwd") != cwd:
                continue
            stamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00")).timestamp()
            session_id = payload.get("session_id") or payload.get("id")
            if session_id:
                candidates.append((stamp, str(session_id)))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            continue
    return candidates


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


def _session_from_open_files(pid: int, *, proc_root: Path = Path("/proc")) -> str | None:
    """Read the one root conversation owned by a live Codex process."""
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
    return next(iter(sessions)) if len(sessions) == 1 else None


def codex_for_pane(pane_pid: int, cwd: str) -> dict[str, Any] | None:
    codex_pid = None
    command = ""
    for pid in [pane_pid, *_proc_children(pane_pid)]:
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip()
            if comm != "codex":
                continue
            raw = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").strip()
            codex_pid, command = pid, raw
            break
        except OSError:
            continue
    if codex_pid is None:
        return None

    session_id = _session_from_open_files(codex_pid)
    confidence = "open-rollout" if session_id else "unknown"
    match = UUID_RE.search(command)
    if not session_id and match:
        session_id = match.group(0)
        confidence = "command-line"
    if not session_id:
        started = _process_start(codex_pid)
        if started is not None:
            choices = sorted(_session_candidates(cwd), key=lambda item: abs(item[0] - started))
            if choices and abs(choices[0][0] - started) <= 20:
                session_id = choices[0][1]
                confidence = "start-time"
    return {"pid": codex_pid, "session_id": session_id, "confidence": confidence}


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


def capture() -> dict[str, Any]:
    shell = capture_shell()
    names = workspace_names()
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
        "#{session_name}", "#{window_index}", "#{window_name}", "#{window_layout}",
        "#{window_active}", "#{pane_index}", "#{pane_id}", "#{pane_pid}",
        "#{pane_current_path}", "#{pane_current_command}", "#{pane_active}",
    ])
    for row in _rows(["tmux", "list-panes", "-a", "-F", pane_format], 11, tmux_errors):
        session_name, win_idx, win_name, layout, win_active, pane_idx, pane_id, pane_pid, cwd, command, pane_active = row
        session = sessions.setdefault(session_name, {
            "name": session_name,
            "attached": session_name in clients_by_session,
            # Retained for version-1 readers. New readers use the terminal list.
            "placement": (clients_by_session.get(session_name) or [{}])[0].get("placement"),
            "windows": {},
        })
        window = session["windows"].setdefault(win_idx, {
            "index": int(win_idx), "name": win_name, "layout": layout,
            "active": win_active == "1", "panes": [],
        })
        codex = codex_for_pane(int(pane_pid), cwd)
        window["panes"].append({
            "index": int(pane_idx), "id": pane_id, "cwd": cwd,
            "command": command, "active": pane_active == "1", "codex": codex,
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
