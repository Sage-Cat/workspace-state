"""Pane-local names, independent of window names and volatile application titles."""
from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .util import CommandError, run


def validate_pane_names(pane: dict[str, Any]) -> None:
    for key in ("title", "label"):
        if key not in pane or (key == "label" and pane[key] is None):
            continue
        value = pane[key]
        if not isinstance(value, str) or len(value) > 16384 or "\0" in value:
            raise ValueError(f"Invalid tmux pane {key}")
    if "allow_rename" in pane and not isinstance(pane["allow_rename"], bool):
        raise ValueError("Invalid tmux pane allow_rename")


def tmux_runtime_identity(
    name: str, *, command_runner: Callable[[list[str]], str] | None = None,
) -> dict[str, str] | None:
    """Identify a tmux server/session without trusting reusable names or PIDs."""
    try:
        execute = run if command_runner is None else command_runner
        value = execute(["tmux", "display-message", "-p", "-t", f"={name}:",
                         "#{pid}\t#{session_id}"]).strip().split("\t")
        if len(value) != 2 or not value[0].isdigit() or not re.fullmatch(r"\$[0-9]+", value[1]):
            return None
        stat = Path(f"/proc/{int(value[0])}/stat").read_text().rsplit(")", 1)[1].split()
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        if not re.fullmatch(r"[0-9a-f-]{36}", boot_id):
            return None
        return {"server_pid": value[0], "server_start_tick": stat[19],
                "session_id": value[1], "boot_id": boot_id}
    except (CommandError, OSError, ValueError, IndexError, AttributeError):
        return None


def _rename_policy(output: str) -> bool:
    value = output.strip()
    if value not in {"on", "off", "1", "0"}:
        raise CommandError("Tmux returned an invalid rename policy")
    return value in {"on", "1"}


def read_window_names(
    window_id: str, *, command_runner: Callable[[list[str]], str] | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"@[0-9]+", window_id):
        raise CommandError("Window naming requires an explicit stable tmux window ID")
    execute = run if command_runner is None else command_runner
    output = execute(["tmux", "display-message", "-p", "-t", window_id,
                      "#{window_id}\n#{window_name}"])
    identity, separator, name = output.removesuffix("\n").partition("\n")
    if not separator or identity != window_id:
        raise CommandError("Tmux window disappeared while reading its name")
    automatic_rename = _rename_policy(execute([
        "tmux", "show-options", "-A", "-w", "-qv", "-t", window_id, "automatic-rename",
    ]))
    return {"name": name, "automatic_rename": automatic_rename}


def read_pane_rename_policy(pane_id: str) -> bool:
    if not re.fullmatch(r"%[0-9]+", pane_id):
        raise CommandError("Pane naming requires an explicit stable tmux pane ID")
    return _rename_policy(run([
        "tmux", "show-options", "-A", "-p", "-qv", "-t", pane_id, "allow-rename",
    ]))


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


def literal_tmux_argument(value: str, *, expands_formats: bool = False) -> str:
    if expands_formats:
        value = value.replace("#", "##")
    # Even argv-based tmux commands interpret a trailing semicolon as a command
    # separator. Escape that delimiter without invoking a shell or config parser.
    return value[:-1] + r"\;" if value.endswith(";") else value


def restore_pane_names(pane_id: str, window_id: str, pane: dict[str, Any]) -> None:
    validate_pane_names(pane)
    if not any(key in pane for key in ("title", "label", "allow_rename")):
        return  # Old recipes must not erase names set since their capture.
    if not re.fullmatch(r"@[0-9]+", window_id):
        raise CommandError("Pane naming requires an explicit stable tmux window ID")
    before = read_pane_names(pane_id, window_id=window_id)
    if "allow_rename" in pane and read_pane_rename_policy(pane_id) != pane["allow_rename"]:
        run(["tmux", "set-option", "-p", "-t", pane_id, "allow-rename",
             "on" if pane["allow_rename"] else "off"])
    if "label" in pane and before["label"] != pane["label"]:
        if pane["label"] is None:
            run(["tmux", "set-option", "-p", "-u", "-t", pane_id, "@pane_label"])
        else:
            run(["tmux", "set-option", "-p", "-t", pane_id, "@pane_label",
                 literal_tmux_argument(pane["label"])])
    # The explicit border label is the durable user name. Application OSC title
    # updates may already have replaced pane_title by the time we capture it.
    title = pane.get("label") or pane.get("title")
    if title is not None and before["title"] != title:
        # select-pane -T expands formats; quote hashes to restore literal text,
        # including strings such as #{pane_id} or #(shell-command).
        run(["tmux", "select-pane", "-t", pane_id, "-T",
             literal_tmux_argument(title, expands_formats=True)])
    after = read_pane_names(pane_id, window_id=window_id)
    if "label" in pane and after["label"] != pane["label"]:
        raise CommandError(f"Tmux pane label did not persist on {pane_id}")
    if "allow_rename" in pane and read_pane_rename_policy(pane_id) != pane["allow_rename"]:
        raise CommandError(f"Tmux pane rename policy did not persist on {pane_id}")
    # Do not fight the application's title updates or install title hooks.
    # Only @pane_label is guaranteed durable against those updates.
