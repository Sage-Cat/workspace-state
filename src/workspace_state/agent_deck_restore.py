"""Keep Agent Deck session recovery separate from terminal window recovery."""
from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
import time
import tomllib
from typing import Any

from . import agent_deck, conversation_guard, operations, tmux_server
from .checkpoint import CURRENT_VERSION, category_provenance
from .conversations import identity, record
from .pending import conversation_item, dashboard_item
from .util import CommandError


def scope_contains(recipe: dict[str, str], row: dict[str, Any]) -> bool:
    group = recipe["group"]
    return recipe["profile"] == row["profile"] and (
        not group or row["group"] == group or row["group"].startswith(group + "/"))


def scopes(sessions) -> list[dict]:
    return [pane["agent_deck"] for session in sessions for window in session.get("windows", [])
            for pane in window.get("panes", []) if "agent_deck" in pane]


def terminal_layout(sessions: list[dict]) -> list[dict]:
    """Keep terminal placement and shell layout while dropping old AI-only tabs."""
    output = []
    for session in sessions:
        windows = []
        has_dashboard = bool(scopes([session]))
        for window in session.get("windows", []):
            panes = window["panes"]
            if has_dashboard and panes and all(record(pane) is not None for pane in panes):
                continue
            windows.append({**window, "panes": [
                {key: value for key, value in pane.items() if key not in {"codex", "conversation"}}
                for pane in panes
            ]})
        if windows:
            output.append({**session, "windows": sorted(windows, key=lambda item: int(item["index"]))})
    return output


def carry_dashboards(snapshot: dict, previous: dict | None, seen: Collection[str] = ()) -> list[str]:
    """Keep a dashboard scope that this capture could not read for its tab.

    A scope is read from the running process, so a capture by an older release
    or a failed read leaves the tab looking like a plain shell, and the next
    restore would open a shell. The scope saved earlier for the same tab (the
    same session, window index, pane index and window name) is kept while the
    tab runs nothing else. A scope that this capture already reads in as many
    tabs as the earlier one did is not carried twice.

    `seen` holds the items observed live during this login. A dashboard seen
    live and gone now was closed by the user and is not brought back. The kept
    scope is no observation: callers run this after the observations of the
    capture were recorded, so it never counts as seen.
    """
    if not previous or "agent_deck" not in snapshot:
        return []
    saved = {}
    for session in previous.get("sessions", []):
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                if "agent_deck" in pane:
                    if dashboard_item(session["name"], pane["agent_deck"]) in seen:
                        continue
                    saved[(session["name"], int(window["index"]), int(pane["index"]))] = (
                        window["name"], pane["agent_deck"])
    if not saved:
        return []
    from collections import Counter
    from .agent_deck import validate_dashboard

    def kind(recipe: dict) -> tuple:
        return recipe["profile"], recipe["group"]

    wanted = Counter(kind(recipe) for _name, recipe in saved.values())
    present = Counter(kind(recipe) for recipe in scopes(snapshot.get("sessions", [])))
    carried = []
    for session in snapshot.get("sessions", []):
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                key = (session["name"], int(window["index"]), int(pane["index"]))
                earlier = saved.get(key)
                if (earlier is None or earlier[0] != window["name"] or "agent_deck" in pane
                        or pane.get("command") == "agent-deck"
                        or pane.get("codex") or pane.get("conversation")
                        or present[kind(earlier[1])] >= wanted[kind(earlier[1])]):
                    continue
                try:
                    validate_dashboard(earlier[1])
                except ValueError:
                    continue
                pane["agent_deck"] = dict(earlier[1])
                present[kind(earlier[1])] += 1
                carried.append(f"{key[0]}:{key[1]}.{key[2]}")
    return carried


def binding(row: dict) -> tuple:
    return row["profile"], row["id"], row["tool"], row["native_id"], row["account"], row["group"]


@dataclass(frozen=True)
class Outcome:
    ready: tuple[dict, ...]
    total: int
    detail: str = ""
    pending: tuple[dict, ...] = ()
    legacy_retained: int = 0
    # Open records without a native conversation ID. Nothing can resume or
    # verify them, so they are never ready and always need attention.
    unresolved: tuple[dict, ...] = ()
    # Inactive restoration starts nothing: expected conversations that are not
    # already open stay stopped instead of pending.
    stopped: tuple[dict, ...] = ()
    inactive: bool = False

    @property
    def verified(self) -> bool:
        if self.inactive:
            return not self.detail
        return len(self.ready) == self.total and not self.detail and not self.unresolved


def requested_rows(snapshot: dict, sessions: list[dict], *, restricted: bool) -> list[dict]:
    choices = scopes(sessions)
    return [row for row in (snapshot.get("agent_deck") or {}).get("sessions", [])
            if row["open"] and not row["archived"] and
            (not restricted or any(scope_contains(recipe, row) for recipe in choices))]


def _native(row: dict) -> tuple:
    return row["tool"], row["native_id"]


def _settings() -> dict[str, bool]:
    config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    path = (config if config.is_absolute() else Path.home() / ".config") / "workspace-state/agent-deck.toml"
    try:
        with path.open("rb") as stream:
            settings = tomllib.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise CommandError(f"Cannot read Agent Deck restore configuration {path}: {error}") from error
    if settings.keys() - {"registered_only", "start_conversations"} or any(
            type(value) is not bool for value in settings.values()):
        raise CommandError(f"Invalid Agent Deck restore configuration {path}. "
                           "Expected a boolean registered_only or start_conversations.")
    return settings


def start_setting() -> tuple[bool, str]:
    """Return the start policy, and why an unreadable configuration was ignored."""
    try:
        return start_conversations(), ""
    except CommandError as error:
        return False, str(error)


def _registered_only() -> bool:
    return _settings().get("registered_only", False)


def start_conversations() -> bool:
    """Whether restoration may start Agent Deck conversations (default: never)."""
    return _settings().get("start_conversations", False)


def _retained_native(snapshot: dict, legacy: set[tuple], current: list[dict], *,
                     strict: bool = True) -> set[tuple]:
    if (not legacy or snapshot.get("version") != CURRENT_VERSION
            or "agent_deck" not in snapshot or not scopes(snapshot.get("sessions", []))
            or category_provenance(snapshot, "terminals").get("state") != "captured"):
        return set()
    registered = {conversation_item(_native(row))
                  for rows in (snapshot["agent_deck"]["sessions"], current)
                  for row in rows if row["native_id"]}
    unregistered = {ident for ident in legacy if conversation_item(ident) not in registered}
    try:
        registered_only = bool(unregistered) and _registered_only()
    except CommandError:
        # Inactive restoration starts nothing and its caller already reports
        # the unreadable file, so only the migration note falls back.
        if strict:
            raise
        registered_only = False
    if registered_only:
        return unregistered
    pending = snapshot.get("pending") or {}
    return {ident for ident in unregistered if conversation_item(ident) in pending}


class LegacyObservationError(RuntimeError):
    pass


def _legacy_tmux(argv: list[str], timeout: float) -> str:
    # The legacy reader expects TAB, which newer tmux can rewrite before any
    # client attaches. The session ID suffices for this duplicate-only check.
    if argv != ["tmux", "list-panes", "-a", "-F", conversation_guard.PANE_FORMAT]:
        raise LegacyObservationError("Unexpected client observation command")
    fields = "#{pane_id}|#{pane_pid}|#{session_id}|#{window_index}|#{pane_index}|#{pane_current_path}"
    try:
        result = subprocess.run(["tmux", "-L", "default", "list-panes", "-a", "-F", fields],
                                capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LegacyObservationError("Could not verify clients outside Agent Deck") from error
    if result.returncode:
        if ("no server running" in result.stderr or
                "error connecting to " in result.stderr and "No such file or directory" in result.stderr):
            return ""
        raise LegacyObservationError("Could not verify clients outside Agent Deck")
    rows = []
    for line in result.stdout.splitlines():
        if not re.fullmatch(r"%[0-9]+\|[0-9]+\|\$[0-9]+\|[0-9]+\|[0-9]+\|/.*", line):
            raise LegacyObservationError("Could not verify clients outside Agent Deck")
        rows.append("\t".join(line.split("|", 5)))
    return "\n".join(rows)


def restore(snapshot: dict, sessions: list[dict], *, deadline: float,
            restricted: bool = False, dry_run: bool = False, start: bool) -> Outcome:
    expected = requested_rows(snapshot, sessions, restricted=restricted)
    # Older snapshots only carry native identifiers. They can point to
    # existing registry entries but cannot authorize creation of a new entry.
    legacy = {
        ident for s in sessions for w in s.get("windows", []) for pane in w.get("panes", [])
        if (ident := identity(pane)) is not None
    } - {_native(row) for row in expected}
    if dry_run or not (expected or legacy):
        return Outcome((), len(expected) + len(legacy))

    context = operations.current()

    def remaining() -> float:
        if context is not None:
            context.check()
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            raise TimeoutError("Agent Deck restoration deadline expired")
        return time_left

    try:
        current = agent_deck.inventory(timeout=remaining())["sessions"]
    except (agent_deck.AgentDeckError, TimeoutError) as error:
        if not start:
            return Outcome((), len(expected), str(error), stopped=tuple(expected), inactive=True)
        return Outcome((), len(expected) + len(legacy), str(error), tuple(expected),
                       unresolved=tuple(row for row in expected if not row["native_id"]))

    retained = _retained_native(snapshot, legacy, current, strict=start)
    by_native: dict[tuple, list[dict]] = {}
    for row in current:
        if row["native_id"] and not row["archived"]:
            by_native.setdefault(_native(row), []).append(row)
    missing_legacy = 0
    choices = scopes(sessions) if restricted else []
    for ident in sorted(legacy - retained):
        peers = by_native.get(ident, [])
        if len(peers) == 1 and not peers[0]["stopped"] and (
                not restricted or any(scope_contains(recipe, peers[0]) for recipe in choices)):
            expected.append({**peers[0], "open": True})
        else:
            missing_legacy += 1
    by_binding = {binding(row): row for row in current if not row["archived"]}
    exact = [binding(row) for row in expected if row["native_id"]
             and len(by_native.get(_native(row), [])) == 1 and binding(row) in by_binding]
    ready = {key for key in exact if by_binding[key]["open"]}
    if not start:
        open_ids = {(row["profile"], row["id"]) for row in current if row["open"] and not row["archived"]}
        running = [row for row in expected
                   if (binding(row) in ready if row["native_id"] else (row["profile"], row["id"]) in open_ids)]
        # Legacy identities absent from Agent Deck are not items to run here.
        return Outcome(tuple(running), len(expected),
                       stopped=tuple(row for row in expected if row not in running),
                       legacy_retained=len(retained), inactive=True)
    to_start = [key for key in exact if key not in ready and not by_binding[key]["stopped"]]
    accepted = set()
    detail = ""
    for key in to_start:
        row = by_binding[key]
        try:
            # Older clients may lack a UUID but expose a matching native title
            # prefix. Recheck that evidence just before each guarded launch.
            with tmux_server.workspace_socket():
                legacy_clients = conversation_guard.live_locations(
                    runner=lambda argv: _legacy_tmux(argv, min(remaining(), 3.0)),
                )
            if conversation_guard.matching(legacy_clients, *_native(row)):
                continue
            agent_deck.start_exact(row, timeout=min(remaining(), 8.0))
        except agent_deck.AgentDeckError as error:
            detail = str(error)
        except TimeoutError as error:
            detail = str(error)
            break
        except LegacyObservationError as error:
            detail = str(error)
            break
        else:
            accepted.add(key)
    while accepted - ready:
        try:
            live = agent_deck.inventory(timeout=remaining())["sessions"]
        except (agent_deck.AgentDeckError, TimeoutError) as error:
            detail = str(error)
            break
        ready.update(binding(row) for row in live if row["open"] and binding(row) in accepted)
        if accepted - ready:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return Outcome(tuple(row for row in expected if row["native_id"] and binding(row) in ready),
                   len(expected) + missing_legacy, detail,
                   tuple(row for row in expected if not row["native_id"] or binding(row) not in ready),
                   legacy_retained=len(retained),
                   unresolved=tuple(row for row in expected if not row["native_id"]))
