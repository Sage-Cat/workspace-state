"""Provider-qualified identities for interactive tmux conversations.

`codex` is the on-disk legacy field. New providers use `conversation`; only
one of these fields may identify a pane. Titles and directories are never IDs.
"""
from __future__ import annotations

from typing import Any

from .capture import UUID_RE, codex_for_pane

PROVIDERS = ("codex", "claude")


def record(pane: dict[str, Any]) -> dict[str, Any] | None:
    value = pane.get("conversation")
    if isinstance(value, dict):
        return value
    legacy = pane.get("codex")
    if isinstance(legacy, dict):
        return {"provider": "codex", **legacy}
    return None


def identity(pane: dict[str, Any]) -> tuple[str, str] | None:
    value = record(pane)
    if value is None or value.get("provider") not in PROVIDERS:
        return None
    session_id = value.get("session_id")
    if value["provider"] == "claude" and value.get("persisted") is not True:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    if value["provider"] == "claude" and not UUID_RE.fullmatch(session_id):
        return None
    return str(value["provider"]), session_id


def key(value: tuple[str, str]) -> str:
    return f"{value[0]}:{value[1]}"


def from_key(value: str) -> tuple[str, str]:
    provider, separator, session_id = value.partition(":")
    if not separator or provider not in PROVIDERS or not UUID_RE.fullmatch(session_id):
        raise ValueError("invalid conversation identity")
    return provider, session_id.lower()


def saved_ids(session: dict[str, Any]) -> dict[tuple[int, int], str]:
    return {
        (int(window["index"]), int(pane["index"])): key(found)
        for window in session.get("windows", []) for pane in window.get("panes", [])
        if (found := identity(pane)) is not None
    }


def live_for_pane(pane_pid: int, cwd: str, *, pane_id: str = "") -> dict[str, Any] | None:
    codex = codex_for_pane(pane_pid, cwd, pane_id=pane_id)
    from .claude_identity import claude_for_pane
    claude = claude_for_pane(pane_pid, cwd, pane_id=pane_id)
    if codex and claude:
        return {"provider": "ambiguous", "session_id": None, "confidence": "multiple-clients"}
    if codex:
        return {"provider": "codex", **codex}
    if claude:
        return {"provider": "claude", **claude}
    return None


def live_identity(pane_pid: int, cwd: str, *, pane_id: str = "", require_ready: bool = False) -> str | None:
    value = live_for_pane(pane_pid, cwd, pane_id=pane_id)
    if not value or value.get("provider") not in PROVIDERS:
        return None
    found = identity({"conversation": value})
    if found is None:
        return None
    provider, session_id = found
    if require_ready:
        if provider == "codex":
            from .codex_resume import resumed_session
            if value.get("confidence") == "restore-wrapper" or not resumed_session(value["pid"], session_id, pane_id):
                return None
        else:
            from .claude_identity import resumed_session
            if not resumed_session(value["pid"], session_id, pane_id):
                return None
    return key(found)


def capture_fields(pane_pid: int, cwd: str, *, pane_id: str = "") -> dict[str, Any]:
    value = live_for_pane(pane_pid, cwd, pane_id=pane_id)
    if not value:
        return {"codex": None}
    if value["provider"] == "codex":
        return {"codex": {k: v for k, v in value.items() if k != "provider"}}
    return {"codex": None, "conversation": value}
