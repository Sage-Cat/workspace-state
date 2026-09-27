"""Pane-local names, independent of window names and volatile application titles."""
from __future__ import annotations

import re
from typing import Any

from .util import CommandError, run


def validate_pane_names(pane: dict[str, Any]) -> None:
    for key in ("title", "label"):
        if key not in pane or (key == "label" and pane[key] is None):
            continue
        value = pane[key]
        if not isinstance(value, str) or len(value) > 16384 or "\0" in value:
            raise ValueError(f"Invalid tmux pane {key}")


def read_pane_names(pane_id: str, *, window_id: str | None = None) -> dict[str, Any]:
    if not re.fullmatch(r"%[0-9]+", pane_id):
        raise CommandError("Pane naming requires an explicit stable tmux pane ID")
    output = run([
        "tmux", "display-message", "-p", "-t", pane_id,
        "#{pane_id}\t#{window_id}\n#{pane_title}",
    ])
    identity, separator, title = output.removesuffix("\n").partition("\n")
    identifiers = identity.split("\t")
    if (not separator or len(identifiers) != 2 or identifiers[0] != pane_id
            or (window_id is not None and identifiers[1] != window_id)):
        raise CommandError("Tmux pane moved or disappeared while reading its name")
    # No -A: capture only the pane-local label, never an inherited option.
    # Empty output means absent; a newline means an explicitly empty label.
    label = run(["tmux", "show-options", "-p", "-qv", "-t", pane_id, "@pane_label"])
    result = {"title": title, "label": label.removesuffix("\n") if label else None}
    validate_pane_names(result)
    return result


def _literal_argument(value: str) -> str:
    # Even argv-based tmux commands interpret a trailing semicolon as a command
    # separator. Escape that delimiter without invoking a shell or config parser.
    return value[:-1] + r"\;" if value.endswith(";") else value


def restore_pane_names(pane_id: str, window_id: str, pane: dict[str, Any]) -> None:
    validate_pane_names(pane)
    if "title" not in pane and "label" not in pane:
        return  # Old recipes must not erase names set since their capture.
    if not re.fullmatch(r"@[0-9]+", window_id):
        raise CommandError("Pane naming requires an explicit stable tmux window ID")
    before = read_pane_names(pane_id, window_id=window_id)
    if "label" in pane and before["label"] != pane["label"]:
        if pane["label"] is None:
            run(["tmux", "set-option", "-p", "-u", "-t", pane_id, "@pane_label"])
        else:
            run(["tmux", "set-option", "-p", "-t", pane_id, "@pane_label",
                 _literal_argument(pane["label"])])
    # The explicit border label is the durable user name. Application OSC title
    # updates may already have replaced pane_title by the time we capture it.
    title = pane.get("label") or pane.get("title")
    if title is not None and before["title"] != title:
        # select-pane -T expands formats; quote hashes to restore literal text,
        # including strings such as #{pane_id} or #(shell-command).
        run(["tmux", "select-pane", "-t", pane_id, "-T",
             _literal_argument(title.replace("#", "##"))])
    after = read_pane_names(pane_id, window_id=window_id)
    if "label" in pane and after["label"] != pane["label"]:
        raise CommandError(f"Tmux pane label did not persist on {pane_id}")
    # Do not fight the application's title updates or install title hooks.
    # Only @pane_label is guaranteed durable against those updates.
