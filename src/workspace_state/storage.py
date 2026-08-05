from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import atomic_json, data_home

SNAPSHOT_NAME = "current"


def path_for() -> Path:
    """Return the one canonical workspace-state recipe."""
    return data_home() / "snapshots" / f"{SNAPSHOT_NAME}.json"


@contextmanager
def state_lock() -> Iterator[None]:
    """Serialize capture-through-publication across manual and hook saves."""
    root = data_home()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    path = root / "state.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def validate(snapshot: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sessions", []), list):
        raise ValueError("workspace state has an invalid sessions collection")
    sessions: set[str] = set()
    for session in snapshot.get("sessions", []):
        if not isinstance(session, dict) or not isinstance(session.get("name"), str):
            raise ValueError("workspace state contains an invalid tmux session")
        if not isinstance(session.get("windows", []), list):
            raise ValueError(f"tmux session {session['name']!r} has invalid windows")
        sessions.add(session["name"])
        for window in session.get("windows", []):
            if not isinstance(window, dict) or not isinstance(window.get("panes", []), list):
                raise ValueError(f"tmux session {session['name']!r} has an invalid window")
    terminals = snapshot.get("terminals", [])
    if not isinstance(terminals, list):
        raise ValueError("workspace state has an invalid terminals collection")
    unknown: set[str] = set()
    for terminal in terminals:
        if not isinstance(terminal, dict):
            unknown.add("<invalid>")
        elif terminal.get("session") not in sessions:
            unknown.add(str(terminal.get("session")))
    if unknown:
        raise ValueError("terminal records reference missing tmux sessions: " + ", ".join(unknown))


def save(snapshot: dict[str, Any]) -> Path:
    path = path_for()
    value = dict(snapshot)
    value["name"] = SNAPSHOT_NAME
    value.pop("archived", None)
    validate(value)
    if path.is_file():
        try:
            previous = json.loads(path.read_text())
            validate(previous)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        else:
            atomic_json(data_home() / "recovery" / "current.last-good.json", previous)
    atomic_json(path, value)
    return path


def load() -> dict[str, Any]:
    path = path_for()
    if not path.exists():
        raise FileNotFoundError("workspace state has not been saved yet; run 'wsctl save'")
    value = json.loads(path.read_text())
    validate(value)
    return value
