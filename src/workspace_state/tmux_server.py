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
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

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


_MANAGER_TTL_SECONDS = 30.0
_manager_cache: tuple[object, float, dict[str, str] | None] | None = None


def _cached_manager_environment() -> dict[str, str] | None:
    """One busctl read serves every pane command of a short operation.

    A failed read is kept as well, so an unreachable manager costs its timeout
    once, not for every tab. The cache belongs to one reader function.
    """
    global _manager_cache
    now = time.monotonic()
    cache = _manager_cache
    if cache is not None and cache[0] is _manager_environment and now < cache[1]:
        value = cache[2]
        return None if value is None else dict(value)
    value = _manager_environment()
    _manager_cache = (_manager_environment, now + _MANAGER_TTL_SECONDS,
                      None if value is None else dict(value))
    return value


def _user_bin(environment: Mapping[str, str]) -> str | None:
    """`~/.local/bin` when it is a safe, existing directory of this user."""
    home = environment.get("HOME") or os.environ.get("HOME") or ""
    path = os.path.join(home, ".local/bin")
    if not home or not os.path.isabs(path):
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & stat.S_IWOTH or info.st_uid not in {0, os.getuid()}:
        return None
    return path


def _package_bin_dir() -> str:
    """The `bin` directory of the checkout or release this package runs from."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), "bin")


def _without_launcher_additions(environment: Mapping[str, str]) -> dict[str, str]:
    """Drop what `bin/wsctl-python` prepended to the worker's own environment.

    Every entry of the launcher's own `bin` directory goes, wherever it stands
    and whether or not `WSCTL_BIN_DIR` survived, so a checkout or release
    directory never reaches a pane. The `~/.local/bin` the launcher put right
    behind it goes too; the user's own entry further on stays.
    """
    result = dict(environment)
    bin_dir = (result.get("WSCTL_BIN_DIR") or "").rstrip("/")
    own = {item for item in (bin_dir, _package_bin_dir()) if item}
    if bin_dir:
        source = os.path.join(os.path.dirname(bin_dir), "src")
        paths = result.get("PYTHONPATH", "").split(os.pathsep)
        if paths and paths[0] == source:
            paths = paths[1:]
        if paths and any(paths):
            result["PYTHONPATH"] = os.pathsep.join(paths)
        else:
            result.pop("PYTHONPATH", None)
    home = os.environ.get("HOME", "")
    local = os.path.join(home, ".local/bin") if home else None
    kept: list[str] = []
    after_own = False
    for entry in result.get("PATH", "").split(os.pathsep):
        if entry.rstrip("/") in own:
            after_own = True
            continue
        if after_own and local is not None and entry.rstrip("/") == local:
            after_own = False
            continue
        after_own = False
        kept.append(entry)
    result["PATH"] = os.pathsep.join(kept)
    return result


def server_environment(*, quiet: bool = False) -> dict[str, str]:
    """The environment every pane of the new server inherits.

    It is the user manager's environment, not the restore worker's: the worker
    carries its release's `PATH` and `PYTHONPATH`, and a server that outlives a
    logout would keep every pane bound to that release. Without the manager
    the worker's own environment is used, minus what the launcher added.
    """
    manager = _cached_manager_environment()
    if manager is None:
        if not quiet:
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
    # Conversation tabs run `wsctl-*-resume`, `claude` and `codex` from the
    # user's bin directory, which the manager's PATH may lack. Only that
    # directory is added, in front, like Agent Deck's own spawn PATH.
    user_bin = _user_bin(environment)
    entries = environment.get("PATH", "").split(os.pathsep) if environment.get("PATH") else []
    if user_bin is not None and user_bin not in entries:
        environment["PATH"] = os.pathsep.join([user_bin, *entries])
    return environment


# Commands that only read state and start no process of their own. Every other
# tmux command of wsctl runs with the clean client environment: a command that
# runs other commands (`if-shell`, `run-shell`, `source-file`, `bind-key`...)
# can create a pane, and tmux gives such a pane the client's `PATH`. A deny list
# of creating verbs would miss exactly those, so the allow list is the read-only one.
_READ_ONLY_VERBS = frozenset({
    "list-sessions", "list-windows", "list-panes", "list-clients", "list-buffers", "list-commands",
    "list-keys", "display-message", "show-options", "show-environment", "show-messages",
    "show-hooks", "show-buffer", "has-session", "capture-pane", "server-info", "info",
})


def tmux_verb(argv: list[str]) -> str | None:
    """The tmux command of `argv`, or None when `argv` is not a tmux invocation."""
    if not argv or os.path.basename(argv[0]) != "tmux":
        return None
    rest = argv[1:]
    # Skip global options (`-L default`, `-S socket`, `-f file`) before the verb.
    while rest and rest[0].startswith("-"):
        rest = rest[2:] if rest[0] in {"-L", "-S", "-f", "-c", "-T"} else rest[1:]
    return rest[0] if rest else ""


def creates_panes(argv: list[str]) -> bool:
    """True for a tmux command that may start a process, so its client `PATH` matters."""
    verb = tmux_verb(argv)
    return verb is not None and verb not in _READ_ONLY_VERBS


# Selects the tmux server a client talks to. The user manager does not carry
# them, but this process may (a test, or a wsctl run from inside a pane), and a
# client that loses them would reach another server than its sibling commands.
def client_environment(*, quiet: bool = False, starts_server: bool = False) -> dict[str, str]:
    """The server environment for a tmux client, still addressing the caller's server.

    A client that starts the server never gets `TMUX`: a server must not
    start believing it is nested in the pane of another.
    """
    environment = server_environment(quiet=quiet)
    # Socket variables come from the caller or not at all, never from the manager.
    environment.pop("TMUX", None)
    environment.pop("TMUX_TMPDIR", None)
    names = ("TMUX_TMPDIR",) if starts_server else ("TMUX", "TMUX_TMPDIR")
    environment.update({name: os.environ[name] for name in names if name in os.environ})
    return environment


def pane_environment(argv: list[str]) -> dict[str, str] | None:
    """The environment of the tmux client that creates a pane, or None.

    tmux 3.4 starts a pane from a detached client with the client's own
    `PATH`, and `new-window -e PATH=...` does not override it. The client
    therefore runs with the server environment, never with a release's or
    checkout's `PATH`.
    """
    return client_environment(quiet=True) if creates_panes(argv) else None


_WORKSPACE_SOCKET = ContextVar("workspace_tmux_socket", default=False)


@contextmanager
def workspace_socket():
    token = _WORKSPACE_SOCKET.set(True)
    try:
        yield
    finally:
        _WORKSPACE_SOCKET.reset(token)


def on_workspace_socket(argv: list[str]) -> list[str]:
    if argv and argv[0] == "tmux" and _WORKSPACE_SOCKET.get():
        return ["tmux", "-L", "default", *argv[1:]]
    return argv


def server_running() -> bool:
    try:
        return subprocess.run(
            on_workspace_socket(["tmux", "list-sessions"]), capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _session_exists(name: str) -> bool:
    try:
        return subprocess.run(
            on_workspace_socket(["tmux", "has-session", "-t", f"={name}"]),
            capture_output=True, text=True, timeout=10,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _created_by(name: str, nonce: str) -> bool:
    """The session carries this call's creation nonce, set by its own `new-session`."""
    try:
        result = subprocess.run(
            on_workspace_socket(["tmux", "show-options", "-qv", "-t", f"={name}:", "@wsctl-created"]),
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
            command, text=True, capture_output=True, timeout=10,
            env=client_environment(starts_server=True),
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
    command = on_workspace_socket(["tmux", "new-session", *arguments])
    if name is not None:
        command += [";", "set-option", "-t", f"={name}:", "@wsctl-created", nonce]
    if server_running():
        return run(command, env=client_environment())
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
            env=client_environment(starts_server=True),
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
    return run(command, env=client_environment())
