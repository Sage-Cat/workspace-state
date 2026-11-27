"""Pure read-only views of a saved snapshot: browser state, placements, counts."""
from __future__ import annotations

from typing import Any

from .browser import browser_windows
from .conversations import identity as conversation_identity, key as conversation_key

BROWSER_KEY = "google_chrome"


def _browser_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read version-4 browser categories and legacy `chrome` snapshots."""
    browsers = snapshot.get("browsers")
    if isinstance(browsers, dict):
        value = browsers.get(BROWSER_KEY, {})
        return value if isinstance(value, dict) else {}
    value = snapshot.get("chrome", {})
    return value if isinstance(value, dict) else {}


def _session_workspace(session: dict[str, Any], names: list[str]) -> str:
    return _placement_workspace(session.get("placement"), names)


def _terminal_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return version-2+ terminals or synthesize them from a version-1 snapshot."""
    if "terminals" in snapshot:
        return list(snapshot.get("terminals", []))
    return [
        {"session": session["name"], "placement": session.get("placement")}
        for session in snapshot.get("sessions", []) if session.get("attached")
    ]


def _placement_workspace(placement: dict[str, Any] | None, names: list[str]) -> str:
    saved_name = (placement or {}).get("workspace_name")
    if saved_name:
        return str(saved_name)
    index = int((placement or {}).get("workspace", -1))
    return names[index] if 0 <= index < len(names) else "Unassigned"


def _named_placement(
    placement: dict[str, Any] | None,
    names: list[str],
) -> dict[str, Any] | None:
    if placement is None:
        return None
    result = dict(placement)
    index = int(result.get("workspace", -1))
    if not result.get("workspace_name") and 0 <= index < len(names):
        result["workspace_name"] = names[index]
    return result


def _workspace_groups(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    from .agent_deck_restore import scope_contains, scopes
    from .pending import deck_item
    names = snapshot.get("desktop", {}).get("workspace_names", [])
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])}

    def empty_group() -> dict[str, Any]:
        return {
            "terminals": [], "session_names": set(), "tmux_windows": 0,
            "codex_ids": set(), "chrome_windows": [], "chrome_tabs": 0,
            "file_manager_windows": 0,
            "vscode_windows": 0,
        }

    groups: dict[str, dict[str, Any]] = {name: empty_group() for name in names}
    groups["Unassigned"] = empty_group()

    for terminal in _terminal_records(snapshot):
        workspace = _placement_workspace(terminal.get("placement"), names)
        group = groups.setdefault(workspace, empty_group())
        group["terminals"].append(terminal)
        group["session_names"].add(terminal["session"])

    assigned_sessions = {terminal["session"] for terminal in _terminal_records(snapshot)}
    groups["Unassigned"]["session_names"].update(set(sessions) - assigned_sessions)
    for group in groups.values():
        group_sessions = [sessions[name] for name in group["session_names"] if name in sessions]
        group["tmux_windows"] = sum(len(session.get("windows", [])) for session in group_sessions)
        group["codex_ids"] = {
            conversation_key(found)
            for session in group_sessions for window in session.get("windows", [])
            for pane in window.get("panes", [])
            if (found := conversation_identity(pane)) is not None
        }
        recipes = scopes(group_sessions)
        group["codex_ids"].update(
            deck_item(row) for row in (snapshot.get("agent_deck") or {}).get("sessions", [])
            if row["tool"] in {"claude", "codex"} and not row["archived"]
            and any(scope_contains(recipe, row) for recipe in recipes)
        )
    for profile, window in browser_windows(_browser_state(snapshot)):
        workspace = str(window.get("workspace") or "Unassigned")
        group = groups.setdefault(workspace, empty_group())
        group["chrome_windows"].append((profile, window))
        group["chrome_tabs"] += len(window.get("tabs", []))
    for window in (snapshot.get("file_manager") or {}).get("windows", []):
        placement = window.get("placement") or {}
        workspace = str(placement.get("workspace_name") or "Unassigned")
        group = groups.setdefault(workspace, empty_group())
        group["file_manager_windows"] += 1
    for window in (snapshot.get("vscode") or {}).get("windows", []):
        placement = window.get("placement") or {}
        workspace = str(placement.get("workspace_name") or window.get("workspace") or "Unassigned")
        groups.setdefault(workspace, empty_group())["vscode_windows"] += 1
    return groups


def _counts(snapshot: dict[str, Any]) -> tuple[int, int, int, int, int]:
    chrome = _browser_state(snapshot)
    chrome_windows = browser_windows(chrome)
    tabs = sum(len(window.get("tabs", [])) for _profile, window in chrome_windows)
    codex = sum(
        1 for session in snapshot.get("sessions", []) for window in session.get("windows", [])
        for pane in window.get("panes", []) if conversation_identity(pane)
    )
    if "agent_deck" in snapshot:
        codex = sum(row["tool"] in {"claude", "codex"} and not row["archived"]
                    for row in snapshot["agent_deck"]["sessions"])
    return (
        len(snapshot.get("terminals", [])), len(snapshot.get("sessions", [])), codex,
        len(chrome_windows), tabs,
    )
