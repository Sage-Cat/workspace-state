from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .util import atomic_json, data_home
from .social_apps import validate_social_apps
from .file_manager import validate_file_manager
from .vscode import validate_vscode
from .tmux_names import validate_pane_names
from .checkpoint import migrate, keep_generation

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
    migrate(snapshot)  # Reject unknown schemas before interpreting their fields.
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("sessions", []), list):
        raise ValueError("workspace state has an invalid sessions collection")
    if "social_apps" in snapshot:
        validate_social_apps(snapshot["social_apps"])
    if "file_manager" in snapshot:
        validate_file_manager(snapshot["file_manager"])
    if "vscode" in snapshot:
        validate_vscode(snapshot["vscode"])
    sessions: set[str] = set()
    for session in snapshot.get("sessions", []):
        if not isinstance(session, dict) or not isinstance(session.get("name"), str):
            raise ValueError("workspace state contains an invalid tmux session")
        if session["name"] in sessions:
            raise ValueError(f"workspace state contains duplicate tmux session {session['name']!r}")
        if not isinstance(session.get("windows", []), list):
            raise ValueError(f"tmux session {session['name']!r} has invalid windows")
        sessions.add(session["name"])
        window_indexes: set[int] = set()
        for window in session.get("windows", []):
            if (
                not isinstance(window, dict)
                or not isinstance(window.get("name"), str)
                or not isinstance(window.get("layout"), str)
                or not isinstance(window.get("panes"), list)
                or not window["panes"]
            ):
                raise ValueError(f"tmux session {session['name']!r} has an invalid window")
            try:
                window_index = int(window["index"])
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError(f"tmux session {session['name']!r} has an invalid window index") from error
            if window_index in window_indexes:
                raise ValueError(f"tmux session {session['name']!r} has duplicate window indexes")
            window_indexes.add(window_index)
            pane_indexes: set[int] = set()
            for pane in window["panes"]:
                if (
                    not isinstance(pane, dict)
                    or not isinstance(pane.get("cwd"), str)
                    or not isinstance(pane.get("command"), str)
                ):
                    raise ValueError(f"tmux session {session['name']!r} has an invalid pane")
                try:
                    pane_index = int(pane["index"])
                except (KeyError, TypeError, ValueError) as error:
                    raise ValueError(f"tmux session {session['name']!r} has an invalid pane index") from error
                if pane_index in pane_indexes:
                    raise ValueError(f"tmux session {session['name']!r} has duplicate pane indexes")
                pane_indexes.add(pane_index)
                validate_pane_names(pane)
                codex = pane.get("codex")
                session_id = codex.get("session_id") if isinstance(codex, dict) else None
                if codex is not None and (
                    not isinstance(codex, dict)
                    or session_id is not None
                    and (not isinstance(session_id, str) or not session_id)
                ):
                    raise ValueError(f"tmux session {session['name']!r} has an invalid Codex identity")
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

    browsers = snapshot.get("browsers")
    chrome = snapshot.get("chrome")
    if browsers is not None:
        if not isinstance(browsers, dict):
            raise ValueError("workspace state has an invalid browsers collection")
        chrome = browsers.get("google_chrome")
    if chrome is not None:
        if not isinstance(chrome, dict) or not isinstance(chrome.get("profiles", []), list):
            raise ValueError("workspace state has an invalid Google Chrome collection")
        for profile in chrome.get("profiles", []):
            if (
                not isinstance(profile, dict)
                or not isinstance(profile.get("profile"), str)
                or not isinstance(profile.get("windows", []), list)
            ):
                raise ValueError("workspace state contains an invalid Google Chrome profile")
            for window in profile.get("windows", []):
                if not isinstance(window, dict) or not isinstance(window.get("tabs", []), list):
                    raise ValueError(
                        f"Google Chrome profile {profile['profile']!r} contains an invalid window"
                    )
                if not all(isinstance(tab, dict) for tab in window.get("tabs", [])):
                    raise ValueError(
                        f"Google Chrome profile {profile['profile']!r} contains an invalid tab"
                    )


def save(snapshot: dict[str, Any]) -> Path:
    path = path_for()
    value = migrate(snapshot)
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
            keep_generation(data_home() / "recovery" / "history", migrate(previous))
    atomic_json(path, value)
    return path


def load() -> dict[str, Any]:
    path = path_for()
    if not path.exists():
        raise FileNotFoundError("workspace state has not been saved yet; run 'wsctl save'")
    value = migrate(json.loads(path.read_text()))
    validate(value)
    return value
