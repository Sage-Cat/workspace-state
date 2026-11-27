"""Open a dashboard when an old terminal checkpoint has no dashboard recipe."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import agent_deck, tmux_server
from .capture import _alacritty_ancestor, _process_identity, process_snapshot
from .desktop import capture_shell
from .restore import run, tmux_runtime_identity
from .tmux_names import literal_tmux_argument, read_window_names
from .util import CommandError


@dataclass(frozen=True)
class Pane:
    window: str
    index: int
    active: bool
    id: str
    pid: int


@dataclass(frozen=True)
class TerminalBinding:
    client_pid: int
    alacritty_pid: int
    window_id: int
    workspace_index: int
    desktop_epoch: str
    background: bool = field(compare=False)


def _panes(session: str) -> list[Pane]:
    fields = "#{window_id}|#{window_index}|#{window_active}|#{pane_id}|#{pane_pid}"
    output = run(["tmux", "list-panes", "-s", "-t", session + ":", "-F", fields])
    panes = []
    for line in output.splitlines():
        if not re.fullmatch(r"@[0-9]+\|[0-9]+\|[01]\|%[0-9]+\|[0-9]+", line):
            raise CommandError("Invalid tmux pane inventory")
        window, index, active, pane, pid = line.split("|")
        panes.append(Pane(window, int(index), active == "1", pane, int(pid)))
    if not panes:
        raise CommandError("No panes in the exact tmux session")
    return panes


def _terminal_binding(session: str, workspace: str) -> TerminalBinding:
    """Prove one exact tmux client is attached to the saved GNOME workspace."""
    clients = run(["tmux", "list-clients", "-t", session, "-F",
                   "#{session_id}|#{client_pid}"]).splitlines()
    if len(clients) != 1 or not re.fullmatch(re.escape(session) + r"\|[1-9][0-9]*", clients[0]):
        raise CommandError("Expected one attached client in the exact tmux session")
    client_pid = int(clients[0].split("|")[1])
    owner = _alacritty_ancestor(client_pid)
    if owner is None:
        raise CommandError("The exact tmux client is not attached through Alacritty")

    shell = capture_shell(timeout=3)
    active = shell.get("active_workspace")
    epoch = shell.get("enable_epoch")
    matches = [item.get("index") for item in shell.get("workspaces", [])
               if item.get("name") == workspace]
    if (not shell.get("available") or shell.get("screen_unavailable")
            or not isinstance(epoch, str) or not epoch or type(active) is not int
            or len(matches) != 1 or type(matches[0]) is not int):
        raise CommandError("Could not verify the exact live GNOME workspace")
    index = matches[0]
    windows = [item for item in shell.get("windows", []) if item.get("pid") == owner]
    if (len(windows) != 1 or type(windows[0].get("id")) is not int
            or windows[0].get("workspace") != index or windows[0].get("sticky") is not False):
        raise CommandError("The attached Alacritty window is not uniquely placed in the saved GNOME workspace")
    window = windows[0]
    return TerminalBinding(client_pid, owner, window["id"], index, epoch,
                           background=active != index and window.get("active") is not True)


def _empty_shell(pane: Pane) -> tuple[str, str] | None:
    """Recognize a plain foreground shell with a visible, empty prompt."""
    proc = Path("/proc")
    process = proc / str(pane.pid)
    identity = _process_identity(pane.pid, proc)
    if (identity is None or not identity.stdin.startswith("/dev/pts/")
            or identity.process_group != identity.foreground_group):
        return None
    try:
        children = process / "task" / str(pane.pid) / "children"
        if ((process / "comm").read_text().strip() not in {"bash", "dash", "fish", "sh", "zsh"}
                or children.read_text().strip()):
            return None
        fields = "#{pane_pid}|#{pane_in_mode}|#{alternate_on}|#{cursor_y}|#{cursor_x}"
        displayed = ["tmux", "display-message", "-p", "-t", pane.id, fields + "|#{pane_tty}"]
        before = run(displayed).rstrip("\n")
        pid, mode, alternate, row, column, tty = before.split("|")
        if pid != str(pane.pid) or mode != "0" or alternate != "0" or tty != identity.stdin:
            return None
        lines = run(["tmux", "capture-pane", "-p", "-t", pane.id]).split("\n")
        line = lines[int(row)]
        # Short, standalone prompt markers can be recognized without reading a
        # developer's prompt settings. Unrecognized prompts remain selected.
        if (int(column) != len(line.rstrip()) + 1
                or not re.fullmatch(r"(?:[$#%>]|[└╰─]*[❯➜])", line.strip())
                or any(tail.strip() for tail in lines[int(row) + 1:])):
            return None
        if (run(displayed).rstrip("\n") == before
                and _process_identity(pane.pid, proc) == identity
                and not children.read_text().strip()):
            return fields, before.rsplit("|", 1)[0]
    except (OSError, ValueError, IndexError, CommandError):
        return None
    return None


def _select_from_placeholder(identity: dict[str, str], pane: Pane, window: str,
                             blank: tuple[str, str]) -> bool:
    session = identity["session_id"]
    # Check the active window, pane and cursor again inside the tmux command
    # that changes the selection. No terminal input is sent.
    fields, expected = blank
    condition = ("#{==:#{pid}|#{window_id}|#{window_panes}|#{pane_id}|" + fields +
                 f",{identity['server_pid']}|{pane.window}|1|{pane.id}|{expected}" + "}")
    run(["tmux", "if-shell", "-F", "-t", session + ":", condition,
         f"select-window -t '{session}:{window}'"])
    return any(item.window == window and item.active for item in _panes(session))


def restore(saved: dict, workspace: str, scope: dict[str, str], *,
            name: str, placeholder: str | None, dry_run: bool = False) -> str:
    agent_deck.validate_dashboard(scope)
    if not name.strip() or "\0" in name or len(name) > 16384:
        raise ValueError("Invalid Agent Deck window name")
    old_index = None
    if placeholder is not None:
        old = [window for window in saved["windows"] if window["name"] == placeholder]
        if len(old) != 1 or len(old[0]["panes"]) != 1:
            raise ValueError("The saved placeholder must name one single-pane window")
        old_index = int(old[0]["index"])

    session_name = saved["name"]
    with tmux_server.workspace_socket():
        identity = tmux_runtime_identity(session_name)
        if identity is None:
            raise CommandError("The exact saved tmux session is not running on the workspace socket")
        session = identity["session_id"]
        grouped = run(["tmux", "display-message", "-p", "-t", session + ":", "#{session_grouped}"]).strip()
        if grouped != "0":
            raise CommandError("Cannot open an isolated dashboard in a grouped tmux session")
        binding = _terminal_binding(session, workspace)

        def check_binding() -> TerminalBinding:
            current = _terminal_binding(session, workspace)
            if tmux_runtime_identity(session_name) != identity or current != binding:
                raise CommandError("The exact tmux session or its Alacritty binding changed during restoration")
            return current

        panes = _panes(session)
        names = {window: read_window_names(window, command_runner=run)["name"]
                 for window in dict.fromkeys(pane.window for pane in panes)}
        with process_snapshot():
            matches = [pane for pane in panes if agent_deck.dashboard(pane.pid) == scope]
        if len(matches) > 1:
            raise CommandError("Several dashboards already match the requested Agent Deck scope")
        if matches:
            check_binding()
            return f"Agent Deck dashboard already verified in {session_name}: {names[matches[0].window]}"
        if name in names.values():
            raise CommandError("The requested dashboard window name is occupied. Choose a different --deck-window.")

        previous = [pane for pane in panes if pane.active]
        candidate = previous[0] if (len(previous) == 1 and previous[0].index == old_index
                                    and names[previous[0].window] == placeholder) else None
        command = agent_deck.dashboard_command(scope)
        check_binding()
        if dry_run:
            return f"Would open Agent Deck dashboard in {session_name}: {name}"
        window = run(["tmux", "new-window", "-d", "-t", session + ":",
                      "-n", literal_tmux_argument(name, expands_formats=True),
                      "-P", "-F", "#{window_id}", *command]).strip()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tmux_runtime_identity(session_name) != identity:
                raise CommandError("The exact tmux session changed while opening the dashboard")
            found = [pane for pane in _panes(session) if pane.window == window]
            if not found:
                break
            if (len(found) == 1 and agent_deck.dashboard(found[0].pid) == scope
                    and read_window_names(window, command_runner=run)["name"] == name):
                blank = _empty_shell(candidate) if candidate is not None else None
                current = check_binding()
                selected = (current.background and candidate is not None and blank is not None
                            and _select_from_placeholder(identity, candidate, window, blank))
                suffix = " (selected in the background workspace)" if selected else ""
                return f"Agent Deck dashboard verified in {session_name}: {name}{suffix}"
            time.sleep(0.10)
        raise CommandError("Could not verify the new Agent Deck dashboard process and window name")
