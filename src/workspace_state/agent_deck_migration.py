"""Open a dashboard when an old terminal checkpoint has no dashboard recipe."""

from __future__ import annotations

import os
import re
import shlex
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


def _dashboard_shell_command(command: list[str]) -> str:
    """The dashboard under a shell, so its exit leaves a usable tab.

    A window that closes would renumber the tabs behind it with
    `renumber-windows on`.
    """
    shell = os.environ.get("SHELL", "/bin/sh")
    return " ".join(shlex.quote(value) for value in command) + "; exec " + shlex.quote(shell)


def _respawn_if_unchanged(identity: dict[str, str], pane: Pane, blank: tuple[str, str],
                          command: list[str]) -> list[str]:
    """Respawn the placeholder only if tmux still sees the checked empty shell.

    The pane, its process, window, cursor and activity are compared inside the
    tmux command that kills the shell, so a shell that became active or got
    input after the check is never killed.
    """
    fields, expected = blank
    home = os.environ.get("HOME") or str(Path.home())
    condition = ("#{==:#{pid}|#{window_id}|#{window_panes}|#{pane_id}|" + fields + "|#{window_active}" +
                 f",{identity['server_pid']}|{pane.window}|1|{pane.id}|{expected}|{int(pane.active)}" + "}")
    respawn = " ".join(shlex.quote(value) for value in [
        "respawn-pane", "-k", "-t", pane.id, "-c", home, _dashboard_shell_command(command)])
    return ["tmux", "if-shell", "-F", "-t", pane.id, condition, respawn]


def _new_dashboard_window(session: str, index: int, command: list[str], name: str) -> list[str]:
    """A new window at an exact index. `new-window -a` is never used: it would shift later tabs."""
    home = os.environ.get("HOME") or str(Path.home())
    return ["tmux", "new-window", "-d", "-t", f"{session}:{index}",
            "-n", literal_tmux_argument(name, expands_formats=True), "-c", home,
            "-P", "-F", "#{window_id}", _dashboard_shell_command(command)]


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

        indexes = {pane.index for pane in panes}
        candidate = blank = None
        if old_index is not None and binding.background:
            holders = [pane for pane in panes if pane.index == old_index]
            if (len(holders) == 1 and names[holders[0].window] == placeholder
                    and (blank := _empty_shell(holders[0])) is not None):
                candidate = holders[0]
        index = old_index if old_index is not None and old_index not in indexes else max(indexes) + 1
        # A tmux window name is not a key: another tab may already use it. The
        # dashboard still goes to its own index, and the clash is reported.
        shared = sorted({pane.index for pane in panes if names[pane.window] == name
                         and (candidate is None or pane.window != candidate.window)})
        command = agent_deck.dashboard_command(scope)
        check_binding()
        suffix = (f" (the name is also used by window {', '.join(map(str, shared))})" if shared else "")
        if dry_run:
            return (f"Would open Agent Deck dashboard in {session_name}: {name} at window "
                    f"{candidate.index if candidate else index}{suffix}")
        in_place = None
        if candidate is not None:
            before = candidate.pid
            run(_respawn_if_unchanged(identity, candidate, blank, command))
            current = run(["tmux", "display-message", "-p", "-t", candidate.id, "#{pane_pid}"]).strip()
            if current != str(before):
                in_place = candidate
        if in_place is not None:
            window = in_place.window
            run(["tmux", "rename-window", "-t", window, literal_tmux_argument(name, expands_formats=True)])
        else:
            window = run(_new_dashboard_window(session, index, command, name)).strip()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if tmux_runtime_identity(session_name) != identity:
                raise CommandError("The exact tmux session changed while opening the dashboard")
            found = [pane for pane in _panes(session) if pane.window == window]
            if not found:
                break
            if (len(found) == 1 and agent_deck.dashboard(found[0].pid) == scope
                    and read_window_names(window, command_runner=run)["name"] == name):
                check_binding()
                where = "in its saved tab" if in_place is not None else f"at window {found[0].index}"
                note = ""
                if in_place is None and old_index is not None and found[0].index != old_index:
                    note = (f". ATTENTION: the saved tab {old_index} is in use, so the dashboard is at "
                            f"window {found[0].index}, not at its saved index")
                return f"Agent Deck dashboard verified in {session_name}: {name} {where}{suffix}{note}"
            time.sleep(0.10)
        raise CommandError("Could not verify the new Agent Deck dashboard process and window name")
