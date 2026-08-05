from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .capture import codex_for_pane
from .util import CommandError, run


def _live_pane(session: str, window: str, pane: str) -> tuple[int, str] | None:
    target = f"={session}:{window}.{pane}"
    try:
        output = run([
            "tmux", "display-message", "-p", "-t", target,
            "#{pane_pid}\t#{pane_current_path}",
        ]).rstrip("\n")
        pane_pid, cwd = output.split("\t", 1)
        return int(pane_pid), cwd
    except (CommandError, FileNotFoundError, ValueError):
        return None


def codex_resume_token(session: str, window: str, pane: str) -> str | None:
    """Return the compact command consumed by tmux-resurrect's `*` expansion."""
    live = _live_pane(session, window, pane)
    if live is None:
        return None
    pane_pid, cwd = live
    codex = codex_for_pane(pane_pid, cwd)
    session_id = (codex or {}).get("session_id")
    return f"wsctl-codex {session_id}" if session_id else None


def _recipe_codex_panes(recipe: dict[str, Any] | None) -> dict[tuple[str, str, str], dict[str, str]]:
    result = {}
    for session in (recipe or {}).get("sessions", []):
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                session_id = (pane.get("codex") or {}).get("session_id")
                if session_id:
                    result[(
                        str(session["name"]), str(window["index"]), str(pane["index"]),
                    )] = {
                        "token": f"wsctl-codex {session_id}",
                        "cwd": str(pane.get("cwd") or ""),
                        "window_name": str(window.get("name") or ""),
                    }
    return result


def _atomic_bytes(path: Path, content: bytes, mode: int) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode & 0o777)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def annotate_state_file(
    path: Path,
    recipe: dict[str, Any] | None = None,
    *,
    recipe_only: bool = False,
) -> dict[str, Any]:
    """Put exact Codex UUIDs into a freshly written tmux-resurrect state file."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"tmux-resurrect state file not found: {path}")

    original = path.read_text()
    lines = original.splitlines(keepends=True)
    window_names = {}
    for raw_line in lines:
        fields = raw_line.rstrip("\n").split("\t")
        if len(fields) >= 4 and fields[0] == "window":
            window_names[(fields[1], fields[2])] = fields[3].removeprefix(":")
    recipe_panes = _recipe_codex_panes(recipe)
    annotated = 0
    unresolved = 0
    rewritten: list[str] = []
    for raw_line in lines:
        newline = "\n" if raw_line.endswith("\n") else ""
        fields = raw_line.rstrip("\n").split("\t")
        if len(fields) == 11 and fields[0] == "pane":
            token = None if recipe_only else codex_resume_token(fields[1], fields[2], fields[5])
            looks_like_codex = fields[9] == "codex" or "codex" in fields[10].casefold()
            saved = recipe_panes.get((fields[1], fields[2], fields[5]))
            saved_matches = saved and (
                saved["cwd"] == fields[7].removeprefix(":")
                and saved["window_name"] == window_names.get((fields[1], fields[2]), "")
            )
            if not token and looks_like_codex and saved_matches:
                token = saved["token"]
            if token:
                fields[10] = f":{token}"
                annotated += 1
            elif looks_like_codex:
                # A plain `codex` would create a new conversation. Leave no
                # restorable process when its identity cannot be proven.
                fields[10] = ":"
                unresolved += 1
            raw_line = "\t".join(fields) + newline
        rewritten.append(raw_line)

    _atomic_bytes(path, "".join(rewritten).encode(), path.stat().st_mode)
    return {"annotated": annotated, "unresolved": unresolved}


def preserve_last_state(path: Path) -> Path:
    """Make a premature continuum save identical to the protected `last` state."""
    path = path.expanduser().resolve()
    last = path.parent / "last"
    try:
        protected = last.resolve(strict=True)
    except (FileNotFoundError, OSError) as error:
        raise FileNotFoundError("tmux-resurrect has no previous state to preserve") from error
    if protected.parent != path.parent or not protected.is_file():
        raise ValueError("tmux-resurrect last state resolves outside its state directory")
    if protected == path:
        return protected
    _atomic_bytes(path, protected.read_bytes(), protected.stat().st_mode)
    return protected
