"""Start the tmux server in a scope of its own.

wsctl alone owns the tmux layout, and the server outlives whichever process
happened to create the first session. A server started inside the restore
worker's transient unit would die with that unit's `RuntimeMaxSec`, and one
started from the session agent would hang on the agent's cgroup. The first
session of a login is therefore created through `systemd-run --user --scope`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from collections.abc import Iterable, Mapping

from . import operations
from .util import CommandError, run

SCOPE_TIMEOUT_SECONDS = 15.0
# Variables that tie a process to the unit or operation that spawned it. Every
# pane of the server would inherit them, so the server never receives them.
_UNIT_VARIABLES = frozenset({
    "INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MAINPID", "NOTIFY_SOCKET",
    "WATCHDOG_PID", "WATCHDOG_USEC", "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES",
    "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "MANAGERPID",
})


def _manager_environment() -> dict[str, str] | None:
    """The user manager's environment, or None when it cannot be read."""
    try:
        result = subprocess.run(
            ["busctl", "--user", "--json=short", "get-property", "org.freedesktop.systemd1",
             "/org/freedesktop/systemd1", "org.freedesktop.systemd1.Manager", "Environment"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode:
            return None
        items = json.loads(result.stdout)["data"]
        return dict(item.split("=", 1) for item in items if "=" in item)
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None


def _without_launcher_additions(environment: Mapping[str, str]) -> dict[str, str]:
    """Drop what `bin/wsctl-python` prepended to the worker's own environment."""
    result = dict(environment)
    bin_dir = result.get("WSCTL_BIN_DIR")
    if not bin_dir:
        return result
    source = os.path.join(os.path.dirname(bin_dir.rstrip("/")), "src")
    paths = result.get("PYTHONPATH", "").split(os.pathsep)
    if paths and paths[0] == source:
        paths = paths[1:]
    if paths:
        result["PYTHONPATH"] = os.pathsep.join(paths)
    else:
        result.pop("PYTHONPATH", None)
    entries = result.get("PATH", "").split(os.pathsep)
    if entries and entries[0] == bin_dir:
        entries = entries[1:]
        local = os.path.join(os.environ.get("HOME", ""), ".local/bin")
        if entries and entries[0] == local and os.environ.get("HOME"):
            entries = entries[1:]
    result["PATH"] = os.pathsep.join(entries)
    return result


def server_environment() -> dict[str, str]:
    """The environment every pane of the new server inherits.

    It is the user manager's environment, not the restore worker's: the worker
    carries its release's `PATH` and `PYTHONPATH`, and a server that outlives a
    logout would keep every pane bound to that release. Without the manager
    the worker's own environment is used, minus what the launcher added.
    """
    manager = _manager_environment()
    if manager is None:
        print("wsctl: the user manager environment is unavailable; "
              "the tmux server inherits the worker environment without launcher additions",
              file=sys.stderr)
        source = _without_launcher_additions(os.environ)
    else:
        source = manager
    environment = {
        name: value for name, value in source.items()
        if not name.startswith("WSCTL_") and name not in _UNIT_VARIABLES
    }
    # A server started from Alacritty used to carry COLORTERM in its global
    # environment, and TUIs (Codex, Claude) enable truecolor from it. The
    # clients wsctl attaches are Alacritty, which supports truecolor.
    environment.setdefault("COLORTERM", "truecolor")
    return environment


def server_running() -> bool:
    try:
        return subprocess.run(
            ["tmux", "list-sessions"], capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _session_exists(name: str) -> bool:
    try:
        return subprocess.run(
            ["tmux", "has-session", "-t", f"={name}"], capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _created_by(name: str, nonce: str) -> bool:
    """The session carries this call's creation nonce, set by its own `new-session`."""
    try:
        result = subprocess.run(
            ["tmux", "show-options", "-qv", "-t", f"={name}:", "@wsctl-created"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == nonce


def _check_abort() -> None:
    context = operations.current()
    if context is not None and context.mode == "startup":
        from .startup_control import check_abort
        check_abort(context)


def _direct(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, timeout=10, env=server_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CommandError(f"{' '.join(command)}: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CommandError(f"{' '.join(command)}: {detail}")
    return result.stdout


class SessionNotCreated(CommandError):
    """A session with the requested name exists but this call did not create it."""


def new_session(arguments: Iterable[str]) -> str:
    """Run `tmux new-session` with `arguments`, in its own scope when it starts the server.

    A new server never starts in the caller's unit. Without systemd there is
    no unit to die with; otherwise a failed scope leaves the item pending
    unless the server already exists. After a failed scope, a session of that
    name counts as created only when it carries this call's nonce, which the
    same tmux invocation sets right after `new-session`; any other session is
    the user's and raises SessionNotCreated.
    """
    arguments = list(arguments)
    name = arguments[arguments.index("-s") + 1] if "-s" in arguments[:-1] else None
    nonce = uuid.uuid4().hex
    command = ["tmux", "new-session", *arguments]
    if name is not None:
        command += [";", "set-option", "-t", f"={name}:", "@wsctl-created", nonce]
    if server_running():
        return run(command)
    _check_abort()
    systemd_run = shutil.which("systemd-run")
    if systemd_run is None:
        print("wsctl: systemd-run is unavailable; the tmux server starts directly",
              file=sys.stderr)
        return _direct(command)
    scoped = [
        systemd_run, "--user", "--scope", "--collect", "--quiet",
        f"--unit=wsctl-tmux-server-{uuid.uuid4().hex[:8]}", "--slice=app.slice",
        "--", *command,
    ]
    try:
        result = subprocess.run(
            scoped, text=True, capture_output=True, timeout=SCOPE_TIMEOUT_SECONDS,
            env=server_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        detail = str(error)
    else:
        if result.returncode == 0:
            return result.stdout
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    print(f"wsctl: tmux server scope failed ({detail})", file=sys.stderr)
    # A user terminal opened at the same moment may have started the server;
    # tmux serves one socket, so the session may exist or can be created there.
    if not server_running():
        raise CommandError(f"tmux server did not start in its scope: {detail}")
    if name is not None and _session_exists(name):
        if _created_by(name, nonce):
            return ""
        raise SessionNotCreated(f"tmux session {name} exists but was not created by wsctl")
    return run(command)
