"""Saved items that were not seen live during this login stay in the checkpoint.

An item is a tmux session, a provider conversation, a VS Code window, a Nemo
window or a desktop app that had windows. A checkpoint carries forward an item
that it had saved but does not see live, unless this login saw the item
earlier: then the user closed it. Carried items stay inline in their category,
so restore needs no merge and the category formats are unchanged. The
top-level `pending` index names them until they are restored or explicitly
forgotten. Plain shell windows have no identity and are not items.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable
from copy import deepcopy
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Callable

from .capture import UUID_RE
from .conversations import PROVIDERS, identity, record
from .util import atomic_json

SEEN_FILE = "seen-items.json"
CATEGORIES = ("terminals", "vscode", "file-manager", "social-apps")
# A waiting launcher or a claimed Claude client that has not loaded its
# conversation is an attempt, not a live conversation: if it fails, the
# conversation must stay pending instead of counting as closed by the user.
UNSEEN_EVIDENCE = frozenset({"restore-wrapper", "resume-claim"})
# Client evidence that says nothing against an earlier identity of the same
# process. Anything else contradicts it or is ambiguous.
NO_EVIDENCE = {"codex": frozenset({"unknown"}), "claude": frozenset({"missing-hook-receipt"})}
_DIGEST = re.compile(r"[0-9a-f]{16}\Z")
_SOCIAL_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")


def session_key(name: str) -> str:
    return f"session:{name}"


def conversation_item(found: tuple[str, str]) -> str:
    return f"{found[0]}:{found[1].lower()}"


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def deck_item(row: dict[str, Any]) -> str:
    fields = ("profile", "id", "tool", "native_id", "account", "group")
    return "agent-deck:" + _digest([row.get(key) for key in fields])


def dashboard_item(name: str, recipe: dict[str, str]) -> str:
    return "agent-deck-dashboard:" + _digest([name, recipe])


def vscode_key(item: dict[str, Any]) -> str:
    from .vscode import _identity
    try:
        value = _identity(item)
    except (KeyError, TypeError, AttributeError):
        value = item  # A record without the identity fields is its own identity.
    return "vscode:" + _digest(value)


def file_manager_key(window: dict[str, Any]) -> str:
    return "file-manager:" + _digest(window.get("locations"))


def social_key(app_id: str) -> str:
    return f"social-apps:{app_id}"


def category_of(item: object) -> str | None:
    """Return the category an item key belongs to; None for a malformed key."""
    if not isinstance(item, str):
        return None
    prefix, separator, rest = item.partition(":")
    if not separator or not rest:
        return None
    if prefix == "session":
        return "terminals"
    if prefix in PROVIDERS:
        return "terminals" if UUID_RE.fullmatch(rest) and rest == rest.lower() else None
    if prefix in {"agent-deck", "agent-deck-dashboard"}:
        return "terminals" if _DIGEST.fullmatch(rest) else None
    if prefix in {"vscode", "file-manager"}:
        return prefix if _DIGEST.fullmatch(rest) else None
    if prefix == "social-apps":
        return prefix if _SOCIAL_ID.fullmatch(rest) else None
    return None


def _panes(sessions: Iterable[dict[str, Any]]):
    for session in sessions:
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                yield session, window, pane


def live_keys(snapshot: dict[str, Any], categories: Collection[str]) -> set[str]:
    """Return the item keys that a fresh capture observed live."""
    keys: set[str] = set()
    if "terminals" in categories:
        keys.update(deck_item(row) for row in (snapshot.get("agent_deck") or {}).get("sessions", [])
                    if row["open"] and not row["archived"])
        for session in snapshot.get("sessions", []):
            keys.add(session_key(session["name"]))
        for session, _window, pane in _panes(snapshot.get("sessions", [])):
            if "agent_deck" in pane:
                keys.add(dashboard_item(session["name"], pane["agent_deck"]))
            found = identity(pane)
            if found and (record(pane) or {}).get("confidence") not in UNSEEN_EVIDENCE:
                keys.add(conversation_item(found))
    if "vscode" in categories:
        keys.update(vscode_key(item) for item in (snapshot.get("vscode") or {}).get("windows", []))
    if "file-manager" in categories:
        keys.update(file_manager_key(window)
                    for window in (snapshot.get("file_manager") or {}).get("windows", []))
    if "social-apps" in categories:
        keys.update(social_key(app_id) for app_id, value in (snapshot.get("social_apps") or {}).items()
                    if isinstance(value, dict) and value.get("mode") == "windowed")
    return keys


def seen_keys(directory: Path | None) -> set[str]:
    """Items this login has seen live; empty when the login record is unavailable."""
    if directory is None:
        return set()
    try:
        value = json.loads((directory / SEEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    keys = value.get("keys") if isinstance(value, dict) and value.get("version") == 1 else None
    return {item for item in keys if isinstance(item, str)} if isinstance(keys, list) else set()


def record_seen(directory: Path | None, keys: Iterable[str]) -> bool:
    """Add live observations to this login's record; False when it is unavailable."""
    keys = set(keys)
    if directory is None:
        return False
    try:
        descriptor = os.open(directory / "seen-items.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = seen_keys(directory)
            if not keys <= current:
                atomic_json(directory / SEEN_FILE, {"version": 1, "keys": sorted(current | keys)})
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return True


def _entries(snapshot: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    value = (snapshot or {}).get("pending")
    return value if isinstance(value, dict) else {}


def pending_layout(snapshot: dict[str, Any] | None) -> tuple[set[str], dict[str, set[int]]]:
    """Return pending session names and the carried windows of live sessions."""
    entries = _entries(snapshot)
    sessions = {item.partition(":")[2] for item in entries if item.startswith("session:")}
    windows: dict[str, set[int]] = {}
    for item, entry in entries.items():
        if item.partition(":")[0] in {*PROVIDERS, "agent-deck-dashboard"} and isinstance(entry, dict):
            name, index = entry.get("session"), entry.get("window")
            if isinstance(name, str) and name not in sessions and type(index) is int:
                windows.setdefault(name, set()).add(index)
    return sessions, windows


def live_sessions_only(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the saved sessions without pending sessions and carried windows."""
    sessions, windows = pending_layout(snapshot)
    result = []
    for session in snapshot.get("sessions", []):
        if session.get("name") in sessions:
            continue
        carried = windows.get(session.get("name"), set())
        if carried:
            session = {**session, "windows": [window for window in session.get("windows", [])
                                              if int(window["index"]) not in carried]}
            if not session["windows"]:
                continue  # Only conversations of a closed session remain.
        result.append(session)
    return result


def _shell_pane(pane: dict[str, Any]) -> dict[str, Any]:
    value = {key: deepcopy(item) for key, item in pane.items() if key != "conversation"}
    value["codex"] = None
    return value


def _carried_pane(pane: dict[str, Any], seen: set[str]) -> dict[str, Any]:
    """A pane outside the live server: no pane ID, and only unseen identities."""
    value = {key: deepcopy(item) for key, item in pane.items() if key != "id"}
    found = identity(pane)
    if record(pane) is not None and (found is None or conversation_item(found) in seen):
        value = _shell_pane(value)
    return value


def _carried_window(name: str, window: dict[str, Any], seen: set[str], *, index: int | None = None,
                    panes: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    value = {key: deepcopy(item) for key, item in window.items() if key != "panes"}
    if index is not None:
        # A new window holds only the carried panes; their saved layout and
        # selection described the original window.
        value.update(index=index, layout="tiled")
    value["active"] = False
    value["panes"] = [_carried_pane(pane, seen) for pane in (panes or window.get("panes", []))]
    for pane in value["panes"]:
        if "agent_deck" in pane and dashboard_item(name, pane["agent_deck"]) in seen:
            pane.pop("agent_deck")
    return value


def _pane_items(name, pane):
    if "agent_deck" in pane:
        yield dashboard_item(name, pane["agent_deck"])
    if (found := identity(pane)) is not None:
        item = conversation_item(found)
        if category_of(item) is not None:
            yield item


def _waiting(name: str, window: dict[str, Any], seen: set[str]) -> list[dict[str, Any]]:
    return [pane for pane in window.get("panes", [])
            if any(item not in seen for item in _pane_items(name, pane))]


def _index_terminal_items(name: str, windows: list[dict[str, Any]], entries, since, seen) -> None:
    for window in windows:
        for pane in window["panes"]:
            for item in _pane_items(name, pane):
                if item not in seen and item not in entries:
                    entries[item] = {"category": "terminals", "since": since(item),
                                     "session": name, "window": int(window["index"])}


def _carry_terminals(snapshot, previous, seen, entries, since) -> None:
    sessions = snapshot.setdefault("sessions", [])
    live = {session["name"]: session for session in sessions}
    clients = snapshot.setdefault("terminals", [])
    for saved in previous.get("sessions", []):
        name = saved.get("name")
        if not isinstance(name, str):
            continue
        if name not in live:
            item = session_key(name)
            if item in seen:
                # Closed or renamed during this login. The session and its
                # terminal client are gone; only windows holding conversations
                # that were never seen stay pending.
                windows = [_carried_window(name, window, seen) for window in saved.get("windows", [])
                           if _waiting(name, window, seen)]
                if windows:
                    sessions.append({**{key: deepcopy(value) for key, value in saved.items()
                                        if key != "windows"}, "windows": windows})
                    _index_terminal_items(name, windows, entries, since, seen)
                continue
            carried = {key: deepcopy(value) for key, value in saved.items() if key != "windows"}
            carried["windows"] = [_carried_window(name, window, seen) for window in saved.get("windows", [])]
            for window in carried["windows"]:
                window["active"] = bool(next((original.get("active") for original in saved["windows"]
                                              if original["index"] == window["index"]), False))
            sessions.append(carried)
            entries[item] = {"category": "terminals", "since": since(item)}
            _index_terminal_items(name, carried["windows"], entries, since, seen)
            clients.extend(deepcopy(client) for client in previous.get("terminals", [])
                           if isinstance(client, dict) and client.get("session") == name)
            continue
        target = live[name]
        used = {int(window["index"]) for window in target.get("windows", [])}
        for window in saved.get("windows", []):
            waiting = _waiting(name, window, seen | entries.keys())
            if not waiting:
                continue
            index = int(window["index"])
            if index in used:
                # Restore never starts a conversation inside an existing pane,
                # so an occupied position gets a window of its own.
                index = max(used) + 1
                carried = _carried_window(name, window, seen, index=index, panes=waiting)
            else:
                carried = _carried_window(name, window, seen)
            used.add(index)
            target["windows"].append(carried)
            _index_terminal_items(name, [carried], entries, since, seen)
        target["windows"].sort(key=lambda value: int(value["index"]))
    sessions.sort(key=lambda value: value["name"])


def _carry_deck(snapshot, previous, seen, entries, since) -> set[str]:
    deck = snapshot.get("agent_deck")
    if deck is None:
        return set()
    rows = deck["sessions"]
    ready = {conversation_item((row["tool"], row["native_id"])) for row in rows
             if row["tool"] in PROVIDERS and row["native_id"] and row["open"] and not row["archived"]}
    current = {(row["profile"], row["id"]): row for row in rows}
    for row in (previous.get("agent_deck") or {}).get("sessions", []):
        ident = deck_item(row)
        if not row["open"] or row["archived"] or ident in seen:
            continue
        latest = current.get((row["profile"], row["id"]))
        # Explicit archive, account change or native identity changes in the
        # live registry take precedence over an older request to reopen it.
        if latest is not None and (latest["archived"] or latest.get("stopped") or deck_item(latest) != ident):
            continue
        if latest is None:
            latest = deepcopy(row)
            rows.append(latest)
        latest["open"] = True
        entries[ident] = {"category": "terminals", "since": since(ident)}
    # Drop a legacy recipe only when Agent Deck actually owns a live client.
    return ready


def _carry_windows(snapshot, previous, category, make_key, limit, seen, entries, since,
                   *, unrestorable=None, required=()) -> list[str]:
    field = category.replace("-", "_")
    current = snapshot.get(field)
    saved = (previous.get(field) or {}).get("windows", [])
    if not isinstance(current, dict) or not saved:
        return []
    windows = current.setdefault("windows", [])
    live = {make_key(window) for window in windows}
    # Unresolved placements can share a project identity. Carry each required
    # destination before ordinary deduplication by identity.
    for window in required:
        item = make_key(window)
        windows.append(deepcopy(window))
        live.add(item)
        entries[item] = {"category": category, "since": since(item)}
    dropped = 0
    for window in saved:
        item = make_key(window)
        if item in live or item in seen or (unrestorable is not None and unrestorable(window)):
            continue
        if len(windows) >= limit:
            dropped += 1
            continue
        windows.append(deepcopy(window))
        live.add(item)
        entries[item] = {"category": category, "since": since(item)}
    return [f"{dropped} unrestored {category} window(s) exceed the saved window limit"] if dropped else []


def _carry_social(snapshot, previous, seen, entries, since) -> None:
    current = snapshot.get("social_apps")
    if not isinstance(current, dict):
        return
    for app_id, saved in (previous.get("social_apps") or {}).items():
        item = social_key(app_id)
        live = current.get(app_id)
        # Restore launches only windowed apps; an app that is no longer
        # configured has nothing to restore it.
        if (not isinstance(saved, dict) or saved.get("mode") != "windowed" or not isinstance(live, dict)
                or live.get("mode") == "windowed" or item in seen or category_of(item) is None):
            continue
        current[app_id] = deepcopy(saved)
        entries[item] = {"category": "social-apps", "since": since(item)}


def carry(snapshot: dict[str, Any], previous: dict[str, Any] | None, *, fresh: Collection[str],
          seen: set[str], now: str, required_vscode: Collection[dict] = ()) -> list[str]:
    """Carry unseen saved items of freshly captured categories into `snapshot`.

    A category that was not freshly captured keeps its previous data, so its
    pending entries are copied unchanged. Return warnings about items that
    could not be carried.
    """
    from .file_manager import MAX_WINDOWS as FILE_MANAGER_WINDOWS
    from .vscode import MAX_WINDOWS as VSCODE_WINDOWS, volatile_project
    previous = previous or {}
    before = _entries(previous)
    entries = {item: deepcopy(entry) for item, entry in before.items()
               if isinstance(entry, dict) and entry.get("category") not in fresh}

    def since(item: str) -> str:
        prior = before.get(item)
        value = prior.get("since") if isinstance(prior, dict) else None
        return value if isinstance(value, str) and value else now

    warnings = []
    if "terminals" in fresh:
        # An identity anywhere in this capture, whatever its evidence, is
        # never carried again: one conversation keeps one pane.
        present = {conversation_item(found) for _session, _window, pane in _panes(snapshot.get("sessions", []))
                   if (found := identity(pane)) is not None}
        present.update(dashboard_item(session["name"], pane["agent_deck"])
                       for session, _window, pane in _panes(snapshot.get("sessions", []))
                       if "agent_deck" in pane)
        registered = _carry_deck(snapshot, previous, seen, entries, since)
        _carry_terminals(snapshot, previous, seen | present | registered, entries, since)
    if "vscode" in fresh:
        # A folder that a reboot removes cannot be reopened; it is not pending.
        warnings += _carry_windows(snapshot, previous, "vscode", vscode_key, VSCODE_WINDOWS,
                                   seen, entries, since, unrestorable=volatile_project,
                                   required=required_vscode)
    if "file-manager" in fresh:
        warnings += _carry_windows(snapshot, previous, "file-manager", file_manager_key,
                                   FILE_MANAGER_WINDOWS, seen, entries, since)
    if "social-apps" in fresh:
        _carry_social(snapshot, previous, seen, entries, since)
    if entries:
        snapshot["pending"] = dict(sorted(entries.items()))
    else:
        snapshot.pop("pending", None)
    return warnings


def _server(session: dict[str, Any]) -> tuple[str, str, str] | None:
    value = session.get("tmux_identity")
    if not isinstance(value, dict):
        return None
    server = tuple(str(value.get(key) or "") for key in ("boot_id", "server_pid", "server_start_tick"))
    return server if all(server) else None


def _set_record(pane: dict[str, Any], provider: str, value: dict[str, Any]) -> None:
    if provider == "codex":
        pane.pop("conversation", None)
        pane["codex"] = {key: item for key, item in value.items() if key != "provider"}
    else:
        pane["codex"] = None
        pane["conversation"] = {**value, "provider": provider}


def keep_unchanged_clients(snapshot: dict[str, Any], previous: dict[str, Any] | None, *,
                           carry_identity: bool = True) -> set[str]:
    """Keep the saved identity of a conversation client that still runs.

    A client is the same when its tmux server, pane and process ID are
    unchanged, and its start time too when both records have one. Its new
    evidence may be missing while it starts, compacts or lacks a hook receipt:
    then the saved identity stays in its pane. Evidence that contradicts the
    saved identity, or a pane where the identity cannot be kept, holds that
    conversation instead: it stays pending rather than counting as closed.
    Return the held conversation keys.
    """
    if not previous:
        return set()
    pending_sessions, carried = pending_layout(previous)
    known: dict[tuple, tuple[tuple[str, str], dict[str, Any]]] = {}
    for session in previous.get("sessions", []):
        server = _server(session)
        if server is None or session.get("name") in pending_sessions:
            continue
        for window in session.get("windows", []):
            if int(window["index"]) in carried.get(session["name"], set()):
                continue
            for pane in window.get("panes", []):
                found, value = identity(pane), record(pane)
                if found is not None and value is not None and isinstance(pane.get("id"), str):
                    known[(*server, pane["id"])] = (found, deepcopy(value))
    if not known:
        return set()
    live = {conversation_item(found) for _session, _window, pane in _panes(snapshot.get("sessions", []))
            if (found := identity(pane)) is not None}
    held: set[str] = set()
    for session in snapshot.get("sessions", []):
        server = _server(session)
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                value = record(pane)
                if server is None or value is None or identity(pane) is not None:
                    continue
                saved = known.get((*server, pane.get("id")))
                if saved is None:
                    continue  # A new pane: nothing was saved for this client.
                found, old = saved
                item = conversation_item(found)
                if item in live:
                    continue  # That conversation runs in another pane now.
                same_process = (str(value.get("pid")) == str(old.get("pid"))
                                and (value.get("start_ticks") is None or old.get("start_ticks") is None
                                     or str(value.get("start_ticks")) == str(old.get("start_ticks"))))
                if value.get("pid") is not None and not same_process:
                    continue  # Another client replaced it: the old one was closed.
                if (carry_identity and same_process and value.get("provider") == found[0]
                        and value.get("confidence") in NO_EVIDENCE.get(found[0], ())):
                    _set_record(pane, found[0], old)
                    live.add(item)
                else:
                    held.add(item)
    return held


def _labels(snapshot: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    labels.update({deck_item(row): f"Agent Deck {row['profile']}/{row['title']}"
                   for row in (snapshot.get("agent_deck") or {}).get("sessions", [])})
    for session in snapshot.get("sessions", []):
        labels[session_key(session["name"])] = f"tmux session {session['name']}"
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                if "agent_deck" in pane:
                    scope = pane["agent_deck"]
                    labels[dashboard_item(session["name"], scope)] = (
                        f"Agent Deck dashboard {scope['profile']}/{scope['group']} in {session['name']}")
                found = identity(pane)
                if found:
                    provider = "Codex" if found[0] == "codex" else "Claude"
                    labels[conversation_item(found)] = (
                        f"{provider} conversation in {session['name']}:{window['index']} ({pane.get('cwd', '')})")
    for item in (snapshot.get("vscode") or {}).get("windows", []):
        target = item.get("workspace_file") or next((folder.get("uri") for folder in item.get("folders", [])), None)
        labels[vscode_key(item)] = f"VS Code {target or item.get('kind', 'window')}"
    for window in (snapshot.get("file_manager") or {}).get("windows", []):
        locations = window.get("locations") or []
        labels[file_manager_key(window)] = f"Nemo {window.get('title') or (locations[0] if locations else 'window')}"
    for app_id in snapshot.get("social_apps") or {}:
        labels[social_key(app_id)] = f"desktop app {app_id}"
    return labels


def describe(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    """Return the pending items with a readable label, oldest first."""
    labels = _labels(snapshot)
    return sorted(({"key": item, "category": str(entry.get("category")), "since": str(entry.get("since")),
                    "label": labels.get(item, item)}
                   for item, entry in _entries(snapshot).items()),
                  key=lambda value: (value["since"], value["key"]))


def validate(snapshot: dict[str, Any]) -> None:
    """Every pending entry names exactly the saved data it keeps."""
    entries = snapshot.get("pending")
    if entries is None:
        return
    if not isinstance(entries, dict):
        raise ValueError("workspace state has an invalid pending index")
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])}
    positions: dict[str, list[tuple[str, int]]] = {}
    for session, window, pane in _panes(sessions.values()):
        for item in _pane_items(session["name"], pane):
            positions.setdefault(item, []).append((session["name"], int(window["index"])))
    keys: dict[str, set[str]] = {}

    def category_keys(category: str, make: Callable[[dict[str, Any]], str]) -> set[str]:
        if category not in keys:
            windows = (snapshot.get(category.replace("-", "_")) or {}).get("windows", [])
            keys[category] = {make(window) for window in windows}
        return keys[category]

    for item, entry in entries.items():
        category = category_of(item)
        if (category is None or not isinstance(entry, dict) or entry.get("category") != category
                or not isinstance(entry.get("since"), str) or not entry["since"]):
            raise ValueError("workspace state has an invalid pending item")
        prefix, _separator, rest = item.partition(":")
        if prefix == "session":
            found = rest in sessions
        elif prefix in PROVIDERS:
            found = positions.get(item) == [(entry.get("session"), entry.get("window"))]
        elif prefix == "agent-deck-dashboard":
            found = (entry.get("session"), entry.get("window")) in positions.get(item, ())
        elif prefix == "agent-deck":
            found = any(deck_item(row) == item and row["open"] and not row["archived"]
                        for row in (snapshot.get("agent_deck") or {}).get("sessions", []))
        elif prefix == "vscode":
            found = item in category_keys("vscode", vscode_key)
        elif prefix == "file-manager":
            found = item in category_keys("file-manager", file_manager_key)
        else:
            found = ((snapshot.get("social_apps") or {}).get(rest) or {}).get("mode") == "windowed"
        if not found:
            raise ValueError(f"pending item {item} does not name saved data")


def forget(snapshot: dict[str, Any], items: Iterable[str]) -> set[str]:
    """Remove pending items and the data they keep; return the changed categories.

    A forgotten conversation leaves its pane as a shell. A window that existed
    only to carry pending conversations goes away with the last of them.
    """
    items = list(dict.fromkeys(items))
    entries = dict(_entries(snapshot))
    unknown = [item for item in items if item not in entries]
    if unknown:
        raise KeyError(", ".join(unknown))
    pending_sessions, _windows = pending_layout(snapshot)
    changed: set[str] = set()
    for item in items:
        entry = entries.pop(item, None)
        if entry is None:
            continue  # Removed together with its pending session.
        changed.add(entry["category"])
        prefix, _separator, rest = item.partition(":")
        if prefix == "session":
            snapshot["sessions"] = [session for session in snapshot.get("sessions", [])
                                    if session.get("name") != rest]
            snapshot["terminals"] = [client for client in snapshot.get("terminals", [])
                                     if client.get("session") != rest]
            for other in [key for key, value in entries.items() if value.get("session") == rest]:
                entries.pop(other)
        elif prefix in {*PROVIDERS, "agent-deck-dashboard"}:
            name, index = entry.get("session"), entry.get("window")
            session = next(value for value in snapshot.get("sessions", []) if value.get("name") == name)
            window = next(value for value in session["windows"] if int(value["index"]) == index)
            window["panes"] = [
                {key: value for key, value in pane.items() if key != "agent_deck"}
                if prefix == "agent-deck-dashboard" and item in _pane_items(name, pane)
                else _shell_pane(pane) if item in _pane_items(name, pane)
                else pane for pane in window["panes"]
            ]
            still_carried = any(value.get("session") == name and value.get("window") == index
                                for value in entries.values())
            if name not in pending_sessions and not still_carried:
                session["windows"] = [value for value in session["windows"] if int(value["index"]) != index]
                if not session["windows"]:
                    # Only the conversations of a closed session were left.
                    snapshot["sessions"] = [value for value in snapshot["sessions"] if value is not session]
        elif prefix == "agent-deck":
            for row in snapshot["agent_deck"]["sessions"]:
                if deck_item(row) == item:
                    row["open"] = False
        elif prefix in {"vscode", "file-manager"}:
            field = prefix.replace("-", "_")
            make = vscode_key if prefix == "vscode" else file_manager_key
            snapshot[field]["windows"] = [window for window in snapshot[field].get("windows", [])
                                          if make(window) != item]
        else:
            snapshot["social_apps"][rest] = {"running": False, "mode": "stopped", "windows": []}
    if entries:
        snapshot["pending"] = entries
    else:
        snapshot.pop("pending", None)
    return changed


def restored_keys(category: str, snapshot: dict[str, Any], results: Iterable[Any]) -> set[str]:
    """Item keys whose restore verified a live identity, from provider results.

    VS Code and Nemo results are numbered by position in the saved window list,
    so callers pass only results of a restore that selected every window.
    """
    from .provider_results import EvidenceState
    verified = [result for result in results
                if getattr(getattr(result, "identity", None), "state", None) == EvidenceState.VERIFIED]
    if category in {"vscode", "file-manager"}:
        windows = (snapshot.get(category.replace("-", "_")) or {}).get("windows", [])
        make = vscode_key if category == "vscode" else file_manager_key
        keys = set()
        for result in verified:
            try:
                position = int(result.item_id) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= position < len(windows):
                keys.add(make(windows[position]))
        return keys
    if category == "social-apps":
        return {social_key(str(result.item_id).split(":", 1)[0]) for result in verified
                if category_of(social_key(str(result.item_id).split(":", 1)[0]))}
    return set()
