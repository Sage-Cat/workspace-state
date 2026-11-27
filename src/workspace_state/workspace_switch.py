"""Durable, explicitly requested transitions for static named GNOME workspaces."""
from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import re
import shlex
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from . import desktop, storage
from .util import CommandError, atomic_json, data_home

CLOSE_WAIT_SECONDS = 5.0


def _directory() -> Path:
    return data_home() / "workspaces"


def _path(name: str) -> Path:
    key = hashlib.sha256(name.casefold().encode()).hexdigest()
    return _directory() / f"{key}.json"


def _load(path: Path) -> dict:
    """Read one archive file; ValueError means it is not a usable archive."""
    try:
        entry = json.loads(path.read_text())
        if (entry["version"] != 1 or not isinstance(entry["name"], str)
                or path != _path(entry["name"]) or entry["phase"] not in {"off", "closing", "opening", "on"}
                or not isinstance(entry["snapshot"], dict)
                or not isinstance(entry["epoch"], str) or not isinstance(entry["token"], str)
                or not isinstance(entry["originals"], dict)):
            raise ValueError("invalid workspace archive")
    except (OSError, TypeError, KeyError) as error:
        raise ValueError("unreadable workspace archive") from error
    return entry


def _archives() -> dict[str, dict]:
    """Usable archives by folded name. An unusable file is skipped: it cannot
    name its workspace, so `_unreadable` finds it by the name's own path."""
    result = {}
    try:
        paths = sorted(_directory().iterdir())
    except FileNotFoundError:
        return result
    for path in paths:
        if path.suffix != ".json":
            continue
        try:
            entry = _load(path)
        except ValueError:
            continue
        result[entry["name"].casefold()] = entry
    return result


def _unreadable(name: str) -> bool:
    path = _path(name)
    if not path.exists():
        return False
    try:
        _load(path)
    except ValueError:
        return True
    return False


def _unreadable_error(name: str) -> CommandError:
    return CommandError(f"The switch archive of workspace {name} is unreadable and its restoration is paused; "
                        f"to discard it run: wsctl workspace forget --yes -- {shlex.quote(name)}")


# The desktop menu cuts the whole stderr line at 240 characters, and the
# command adds "wsctl: " in front of the message.
MESSAGE_LIMIT = 240 - len("wsctl: ")


def _text(value, limit: int = 40) -> str:
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(value)).strip()[:limit]


def _summary(problems: list[str]) -> str:
    """All problems, folded to the length the desktop menu can show."""
    problems = list(dict.fromkeys(problems))
    total = len(problems)
    best = 0
    for count in range(total, 0, -1):
        suffix = f" +{total - count} more" if count < total else ""
        if len("; ".join(problems[:count])) + len(suffix) <= MESSAGE_LIMIT:
            best = count
            break
    suffix = f" +{total - best} more" if 0 < best < total else (f" +{total - 1} more" if best == 0 and total > 1 else "")
    if best:
        return "; ".join(problems[:best]) + suffix
    return problems[0][:MESSAGE_LIMIT - len(suffix) - 1] + "…" + suffix


class SwitchBlocked(CommandError):
    """Every reason a workspace cannot be switched off yet."""

    def __init__(self, problems: list[str]):
        self.problems = list(problems)
        super().__init__(_summary(self.problems))


@contextmanager
def _switch_lock():
    directory = _directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    # Ensure first-use directory entries survive a power interruption after
    # the archive is committed and before all windows have closed.
    for parent in (directory.parent, directory.parent.parent):
        parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    fd = os.open(directory / "switch.lock", os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise storage.StateBusy("another workspace switch is running") from error
        yield
    finally:
        os.close(fd)


def _busy() -> bool:
    try:
        with (_directory() / "switch.lock").open("rb") as stream:
            fcntl.flock(stream, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False
    except FileNotFoundError:
        return False
    except BlockingIOError:
        return True


def _slots(shell: dict) -> dict[str, dict]:
    if (not shell.get("available") or "close_window" not in shell.get("capabilities", ())
            or not shell.get("enable_epoch") or shell.get("dynamic_workspaces") is not False):
        return {}
    named = [item for item in shell["workspaces"] if item.get("named") is True and item.get("name")]
    duplicates = Counter(item["name"].casefold() for item in shell["workspaces"])
    return {item["name"].casefold(): item for item in named if duplicates[item["name"].casefold()] == 1}


def _slot(shell: dict, name: str) -> dict:
    slot = _slots(shell).get(name.casefold())
    if not slot:
        raise CommandError("Workspace switching requires a unique, explicitly named static GNOME workspace")
    if shell.get("screen_unavailable") is not False:
        raise CommandError("Unlock GNOME before switching a workspace")
    if type(slot.get("blocking_windows")) is not int:
        raise CommandError("GNOME did not report all windows on the workspace")
    return slot


def _windows(shell: dict, slot: dict) -> dict[int, dict]:
    return {item["id"]: item for item in shell["windows"] if item.get("workspace") == slot["index"] and item.get("sticky") is not True}


CLOSE_BLOCKERS = {
    "unsupported_window": "is not a regular window",
    "sticky": "is shown on all workspaces",
    "transient": "is a dialog of another window",
    "unsupported_client": "is an X11 client that cannot be archived or closed safely",
    "not_closable": "cannot be closed",
    "invalid_pid": "has no valid process ID",
}


def _describe(window: dict) -> str:
    title = _text(window.get("title") or window.get("app_id") or window.get("id"))
    return f"{'minimized ' if window.get('state') == 'minimized' else ''}window \"{title}\""


def _window_problem(window: dict) -> str | None:
    if window.get("sticky") is not False:
        return "has an unknown all-workspaces state"
    if "close_blocker" not in window:
        return "was not classified by GNOME"
    if window["close_blocker"]:
        return CLOSE_BLOCKERS.get(window["close_blocker"], f"cannot be closed ({_text(window['close_blocker'])})")
    if type(window.get("pid")) is not int or window["pid"] <= 0:
        return "has no valid process ID"
    return None


def _inventory_problems(shell: dict, slot: dict) -> tuple[dict[int, dict], list[str]]:
    windows = _windows(shell, slot)
    name = slot["name"]
    problems = []
    if slot["blocking_windows"]:
        problems.append(f"{name}: {slot['blocking_windows']} window(s) GNOME cannot list, such as dialogs")
    for window in windows.values():
        if reason := _window_problem(window):
            problems.append(f"{name}: {_describe(window)} {reason}")
    return windows, problems


def _inventory(shell: dict, slot: dict) -> dict[int, dict]:
    windows, problems = _inventory_problems(shell, slot)
    if problems:
        raise SwitchBlocked(problems)
    return windows


def _witness(windows: dict[int, dict]) -> dict:
    return {str(wid): {key: window.get(key) for key in ("pid", "title", "workspace", "monitor", "geometry", "state")}
            for wid, window in windows.items()}


def _native_ids(snapshot: dict) -> list[int]:
    return [item["_native_id"] for item in _records(snapshot)]


def _records(snapshot: dict):
    from .cli import _browser_state, _terminal_records
    from .browser import browser_windows
    yield from _terminal_records(snapshot)
    yield from (window for _, window in browser_windows(_browser_state(snapshot)))
    for key in ("file_manager", "vscode"):
        yield from (snapshot.get(key) or {}).get("windows", [])
    for record in (snapshot.get("social_apps") or {}).values():
        yield from record.get("windows", [])


def _project(snapshot: dict, select, *, include_detached: bool) -> dict:
    """Project per-window recipes and session ownership without renumbering GNOME."""
    from . import agent_deck_restore, pending
    from .cli import _browser_state, _terminal_records, _set_browser_state

    result = copy.deepcopy(snapshot)
    terminal_rows = _terminal_records(result)
    selected_terminals = [item for item in terminal_rows if select(item, item.get("placement"))]
    attached = {item["session"] for item in terminal_rows}
    selected_sessions = {item["session"] for item in selected_terminals}
    hidden_sessions = set()
    result["terminals"] = selected_terminals
    sessions = result.get("sessions", [])
    result["sessions"] = []
    for session in sessions:
        name = session["name"]
        if (name in selected_sessions or
                (name not in attached and include_detached and select(session, session.get("placement")))):
            result["sessions"].append(session)
        else:
            hidden_sessions.add(name)
    if "agent_deck" in result:
        visible = agent_deck_restore.scopes(result["sessions"])
        hidden = agent_deck_restore.scopes([item for item in sessions if item["name"] in hidden_sessions])
        result["agent_deck"]["sessions"] = [
            row for row in result["agent_deck"]["sessions"]
            if any(agent_deck_restore.scope_contains(scope, row) for scope in visible)
            or (include_detached and not any(agent_deck_restore.scope_contains(scope, row) for scope in hidden))
        ]
    chrome = _browser_state(result)
    if chrome:
        changed = False
        profiles = []
        for profile in chrome.get("profiles", []):
            old = profile.get("windows", [])
            windows = [item for item in old if select(item, {
                "workspace": item.get("workspace_index"), "workspace_name": item.get("workspace"),
            })]
            changed |= len(old) != len(windows)
            if windows:
                profiles.append({**profile, "windows": windows})
        if changed:
            chrome = {key: value for key, value in chrome.items() if key != "latest_observation"}
            _set_browser_state(result, {**chrome, "profiles": profiles})
    for key in ("file_manager", "vscode"):
        if key in result:
            result[key]["windows"] = [item for item in result[key]["windows"]
                                     if select(item, item.get("placement"))]
    for item in (result.get("social_apps") or {}).values():
        old = item["windows"]
        item["windows"] = [window for window in old if select(window, window)]
        if old and not item["windows"]:
            item.update(running=False, mode="stopped")
    if "pending" in result:
        live = pending.live_keys(result, pending.CATEGORIES)
        entries = result["pending"]
        result["pending"] = {key: val for key, val in entries.items() if key in live}
    return result


def _destination(snapshot: dict, placement: dict | None) -> str:
    from .cli import _placement_workspace
    if not placement or (not placement.get("workspace_name") and type(placement.get("workspace")) is not int):
        return ""
    return _placement_workspace(placement, snapshot.get("desktop", {}).get("workspace_names", [])).casefold()


def suppress(snapshot: dict) -> dict:
    """Keep switched-off recipes out of every ordinary restore entry point."""
    entries = _archives()
    parked = {key for key, entry in entries.items() if entry["phase"] != "on"}
    # An unreadable archive cannot say what it parked: hide only the windows
    # saved on its own workspace and leave every other workspace restorable.
    names = (snapshot.get("desktop") or {}).get("workspace_names") or ()
    paused = {name.casefold() for name in names if isinstance(name, str) and name and _unreadable(name)}
    hidden = parked | paused
    if not hidden:
        return snapshot
    terminal_names = {
        session["name"] for entry in entries.values() if entry["phase"] != "on"
        for session in entry["snapshot"].get("sessions", [])
    }

    def keep(item, placement):
        if _destination(snapshot, placement) in hidden:
            return False
        # A detached capture made after Off has no placement at all. Keep a
        # genuinely visible client of a shared session in the active recipe.
        return placement is not None or (item.get("session") or item.get("name")) not in terminal_names

    result = _project(snapshot, keep, include_detached=True)
    if "agent_deck" in result:
        from .agent_deck_restore import scope_contains, scopes
        from .pending import deck_item
        disabled_rows = {deck_item(row) for entry in entries.values() if entry["phase"] != "on"
                         for row in (entry["snapshot"].get("agent_deck") or {}).get("sessions", [])}
        active_scopes = scopes(result["sessions"])
        result["agent_deck"]["sessions"] = [row for row in result["agent_deck"]["sessions"]
            if deck_item(row) not in disabled_rows or any(scope_contains(scope, row) for scope in active_scopes)]
        if "pending" in result:
            allowed = {deck_item(row) for row in result["agent_deck"]["sessions"]}
            result["pending"] = {key: value for key, value in result["pending"].items()
                                 if not key.startswith("agent-deck:") or key in allowed}
    # A split browser recipe is not authoritative for full-profile native
    # reconciliation or for cleaning up Chrome's own startup windows.
    result["_workspace_switch_exclusions"] = True
    from .cli import _browser_state
    # Chrome can replay its last closed window on a cold start. Suppress that
    # even after Off completed; only incomplete switches forbid ordinary live reuse.
    cold_profiles, blocked_profiles = set(), set()
    for key in parked:
        entry = entries[key]
        profiles = {profile["profile"] for profile in _browser_state(entry["snapshot"]).get("profiles", [])
                    if profile.get("windows")}
        cold_profiles.update(profiles)
        if entry["phase"] != "off":
            blocked_profiles.update(profiles)
    # A profile that keeps an enabled workspace's windows restarts with Chrome's
    # own session, which holds exactly those windows. Only a profile without
    # any enabled window (Chrome would replay its last closed one) or with an
    # unfinished switch still starts without a startup window.
    enabled = {profile["profile"] for profile in _browser_state(result).get("profiles", [])
               if profile.get("windows")}
    result["_workspace_switch_cold_profiles"] = cold_profiles - (enabled - blocked_profiles)
    result["_workspace_switch_blocked_profiles"] = blocked_profiles
    result["_workspace_switch_blocked_vscode_scopes"] = {
        window["user_data_dir"] for entry in entries.values() if entry["phase"] != "on"
        for window in (entry["snapshot"].get("vscode") or {}).get("windows", [])
    }
    return result


def _snapshot(cli, shell: dict, slot: dict, windows: dict[int, dict], *, for_close: bool = True) -> dict:
    from .browser import _chrome_shell_windows
    from .checkpoint import CaptureContext
    from .file_manager import matching_windows as nemo_windows
    from .vscode import matching_windows as code_windows

    target = {"windows": list(windows.values())}
    categories = {"terminals"}
    if _chrome_shell_windows([], target):
        categories.add("browsers")
    if nemo_windows(target):
        categories.add("file-manager")
    if code_windows(target):
        categories.add("vscode")
    if any(cli.matching_social_windows(app, target) for app in cli.configured_apps()):
        categories.add("social-apps")
    context = CaptureContext.begin(shell=shell)
    captured = cli._capture_all(only=categories, browsers="browsers" in categories,
                                context=context, include_native_ids=True, native_window_ids=frozenset(windows))
    relevant = _project(captured, lambda item, _: item.get("_native_id") in windows, include_detached=False)
    # Terminal details outside this workspace cannot authorize closing a
    # client in it. Errors in a visible provider conservatively stop Off.
    problems = [problem for category, issues in cli._category_problems(
        relevant, None, browsers="browsers" in categories,
    ).items() if category in categories and (category != "terminals" or relevant["terminals"])
        for problem in issues]
    if problems:
        raise SwitchBlocked([f"{slot['name']}: {problem}" for problem in problems])
    if for_close:
        captured_ids = Counter(_native_ids(relevant))
        if captured_ids != Counter(windows.keys()):
            problems = [f"{slot['name']}: {_describe(window)} "
                        + ("has no provider that can archive it" if not captured_ids[wid]
                           else "was captured more than once")
                        for wid, window in windows.items() if captured_ids[wid] != 1]
            raise SwitchBlocked(problems or [f"{slot['name']}: a window was captured ambiguously"])
        _verify_close_recipes(cli, captured, relevant)
    for key in ("capture_context", "capture_errors", "capture_warnings", "pending", "category_provenance"):
        relevant.pop(key, None)
    # Terminal records already hold GNOME's stable window ID in their
    # placement. The explicit _native_id is uniformly used for retry selection.
    storage.validate(relevant)
    return relevant


CHROME_REOPENABLE = ("https://", "http://", "file://", "about:blank")
# chrome:// pages that act or crash the browser when opened. Everything else
# under chrome:// is an ordinary page that a new tab opens by its URL.
CHROME_ACTION_PAGES = {
    "crash", "kill", "hang", "gpucrash", "gpuhang", "quit", "restart", "shorthang", "memory-exhaust",
    "inducebrowsercrashforrealz", "inducebrowserheapcorruption", "heapcorruptioncrash", "gpuclean",
}
CHROME_MARKER = "chrome://version/#wsctl-workspace-"


def _chrome_reopenable(url: str) -> bool:
    """True when opening this URL in a new tab recreates the page."""
    if url.startswith(CHROME_REOPENABLE):
        return True
    if url.startswith(CHROME_MARKER):
        return False
    parsed = urlsplit(url)
    return parsed.scheme == "chrome" and bool(parsed.hostname) and parsed.hostname.lower() not in CHROME_ACTION_PAGES


def _url_kind(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}:" if parsed.scheme and parsed.scheme != "chrome" else (
        f"chrome://{_text(parsed.hostname or '', 20)}" if parsed.scheme else "an invalid URL")


def _vscode_label(window: dict) -> str:
    uri = window.get("workspace_file") or (window["folders"][0]["uri"] if window.get("folders") else "")
    name = _text(Path(urlsplit(uri).path).name) if uri else "empty window"
    return f"{_text((window.get('placement') or {}).get('workspace_name'))}: Code {name or 'window'}"


def _vscode_problems(window: dict) -> list[str]:
    label = _vscode_label(window)
    if not window.get("_supports_workspace_switch"):
        return [f"{label} needs its companion reloaded (run 'Developer: Reload Window')"]
    problems = []
    blockers = window.get("_switch_blockers")
    if blockers is None:
        # A companion without the switch policy blocks on any non-text editor.
        if window.get("dirty_count"):
            problems.append(f"{label} has unsaved edits")
        if window.get("_unsupported_editors") != 0:
            problems.append(f"{label} has non-file editors; reload the window to update its companion")
    else:
        names = {"unsaved": "unsaved tab", "untitled": "untitled tab with content", "terminal": "terminal",
                 "unverified": "state not verified"}
        problems += [f"{label} {names[item['kind']]} {_text(item['label'])}".rstrip()
                     + (f" ({_text(item['reason'])})" if item.get("reason") else "") for item in blockers]
    if window["kind"] in {"empty", "untitled"} and (window["kind"] == "untitled" or window.get("editor_uris")):
        problems.append(f"{label} must be saved as a workspace file first")
    return problems


def _verify_close_recipes(cli, captured: dict, scoped: dict) -> None:
    from .browser import browser_companion_info, browser_windows
    from .browser_reconciliation import window_signature
    problems = []
    for profile in cli._browser_state(scoped).get("profiles", []):
        info = browser_companion_info(profile["profile"], timeout=2)
        if "workspace_switch" not in info.get("capabilities", ()):
            problems.append(f"Chrome profile {_text(profile['profile'])} needs its companion reloaded")
    for profile, window in browser_windows(cli._browser_state(scoped)):
        tabs = window.get("tabs") or []
        label = f"{_text(window.get('workspace'))}: Chrome {_text(profile['profile'])}"
        if window.get("incognito") or window.get("type") != "normal":
            problems.append(f"{label} private or app window cannot be recreated")
        elif not tabs:
            problems.append(f"{label} window has no tabs to recreate")
        else:
            bad = sorted({_url_kind(tab["url"]) if isinstance(tab.get("url"), str) else "an invalid URL"
                          for tab in tabs if not isinstance(tab.get("url"), str) or not _chrome_reopenable(tab["url"])})
            if bad:
                problems.append(f"{label} window has tabs that cannot be reopened by URL: {', '.join(bad)}")
            else:
                window_signature(window)
    if (scoped.get("file_manager") or {}).get("windows"):
        from .file_manager import _bridge, _default_is_nemo
        if not _default_is_nemo() or "workspace_launch_marker" not in _bridge("GetState").get("capabilities", ()):
            problems.append("Nemo integration needs a reload to switch this workspace")
    for window in (scoped.get("vscode") or {}).get("windows", []):
        problems += _vscode_problems(window)
    for app, item in (scoped.get("social_apps") or {}).items():
        if item["windows"] and captured["social_apps"][app].get("_native_window_count") != 1:
            problems.append(f"{_text(app)} has several windows and cannot restore one independently")
    if problems:
        raise SwitchBlocked(problems)
    if scoped.get("sessions"):
        _verify_tmux_retention(scoped)


def _verify_tmux_retention(snapshot: dict) -> None:
    from . import tmux_server
    from .restore import tmux_runtime_identity
    from .util import run
    command = ["tmux", "-L", "default"] if "agent_deck" in snapshot else ["tmux"]
    # Explicit socket scope is also used by the provider when Agent Deck owns
    # the canonical workspace server.
    with tmux_server.workspace_socket() if "agent_deck" in snapshot else nullcontext():
        if run([*command, "show-options", "-s", "-v", "exit-unattached"]).strip() != "off":
            raise CommandError("tmux is configured to exit when its last client detaches")
        for session in snapshot["sessions"]:
            name = session["name"]
            identity = session.get("tmux_identity")
            if not identity or tmux_runtime_identity(name) != identity:
                raise CommandError("The tmux session changed during capture")
            if run([*command, "show-options", "-Av", "-t", "=" + name + ":", "destroy-unattached"]).strip() != "off":
                raise CommandError("tmux is configured to destroy a session when its client detaches")


def _refresh(shell: dict, slot: dict, *, same_capture: bool = True) -> tuple[dict, dict]:
    current = desktop.capture_shell()
    target = _slot(current, slot["name"])
    if (current["enable_epoch"] != shell["enable_epoch"] or target["index"] != slot["index"]
            or (same_capture and desktop.desktop_topology_signature(current)
                != desktop.desktop_topology_signature(shell))):
        raise CommandError("GNOME workspace or display topology changed during the operation")
    return current, target


def _write(entry: dict, phase: str, detail: str = "") -> None:
    entry.update(phase=phase, detail=detail)
    atomic_json(_path(entry["name"]), entry)


def _closing_windows(shell: dict, slot: dict, entry: dict) -> dict[int, dict]:
    windows = _inventory(shell, slot)
    witness = _witness(windows)
    if (windows and shell["enable_epoch"] != entry["epoch"]) or any(
        entry["originals"].get(wid) != record for wid, record in witness.items()
    ):
        raise CommandError("Workspace windows changed; the archived copy was preserved")
    # A target window moved elsewhere must not be mistaken for a closed one.
    if shell["enable_epoch"] == entry["epoch"] and any(
        str(item["id"]) in entry["originals"] and item["id"] not in windows
        for item in shell["windows"]
    ):
        raise CommandError("An original window moved elsewhere; the archived copy was preserved")
    return windows


def _browser_windows(cli, entry: dict) -> dict:
    from .browser import browser_windows
    return {window["_native_id"]: (profile["profile"], window,
            f"{entry['token']}:{profile['profile']}:{window['id']}")
            for profile, window in browser_windows(cli._browser_state(entry["snapshot"]))}


def _cancel_browser_markers(browser_windows: dict, still_open) -> None:
    from . import browser
    for wid in still_open:
        if wid in browser_windows:
            profile, _saved, token = browser_windows[wid]
            browser.cancel_workspace_window(profile, token)


def _finish_committed_on(cli, entry: dict) -> None:
    if not entry.get("chrome_cleanup_pending"):
        return
    try:
        marked = _browser_windows(cli, entry)
        _cancel_browser_markers(marked, marked)
    except (OSError, RuntimeError, ValueError) as error:
        detail = f"Workspace is enabled; retry to remove its temporary Chrome tabs: {error}"
        _write(entry, "on", detail)
        raise SwitchPartial(detail) from error
    entry.pop("chrome_cleanup_pending", None)
    _write(entry, "on")


def _recipe_problems(cli, shell: dict, slot: dict, windows: dict[int, dict]) -> list[str]:
    """Best-effort extra reasons from the provider capture; never blocks by itself."""
    if not windows:
        return []
    try:
        _snapshot(cli, shell, slot, windows)
    except SwitchBlocked as error:
        return error.problems
    except CommandError as error:
        return [str(error)]
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        pass
    return []


def _off(cli, shell: dict, slot: dict, previous: dict | None) -> None:
    if previous and previous["phase"] == "opening":
        raise CommandError("Finish turning on this workspace before switching it off again")
    browser_windows = {}
    if previous and previous["phase"] in {"closing", "off"}:
        entry = previous
        remaining = _closing_windows(shell, slot, entry)
        if entry["phase"] == "off":
            if remaining:
                raise CommandError("New windows appeared on this switched-off workspace")
            return
        if remaining:
            _cancel_browser_markers(_browser_windows(cli, entry), remaining)
            # The first attempt may have closed a subset. Check before retrying
            # that each still-live recipe equals its archived original.
            fresh = _snapshot(cli, shell, slot, remaining)
            archived = _project(entry["snapshot"], lambda item, _: item.get("_native_id") in remaining,
                                include_detached=False)
            if _recipe(fresh) != _recipe(archived):
                raise SwitchPartial("A live window's contents changed; turn the workspace on before archiving again")
            browser_windows = _browser_windows(cli, entry)
            for window in _records(fresh):
                wid = window["_native_id"]
                if wid in browser_windows:
                    profile, saved, token = browser_windows[wid]
                    browser_windows[wid] = profile, {**saved, "_browser_window_id": window["_browser_window_id"]}, token
    else:
        windows, problems = _inventory_problems(shell, slot)
        if problems:
            # Also report what blocks the windows GNOME does accept.
            eligible = {wid: window for wid, window in windows.items() if not _window_problem(window)}
            problems += _recipe_problems(cli, shell, slot, eligible)
            raise SwitchBlocked(problems)
        snapshot = (_snapshot(cli, shell, slot, windows) if windows else
                    {"version": 2, "name": "current", "desktop": {"workspace_names": desktop.workspace_names(shell=shell)},
                     "sessions": [], "terminals": []})
        entry = {"version": 1, "name": slot["name"], "epoch": shell["enable_epoch"],
                 "originals": _witness(windows), "token": uuid.uuid4().hex, "snapshot": snapshot}
        current, target = _refresh(shell, slot)
        if _witness(_inventory(current, target)) != entry["originals"]:
            raise CommandError("Workspace windows changed during capture; no windows were closed")
        _write(entry, "closing", "Waiting for the workspace windows to close")
        browser_windows = _browser_windows(cli, entry)

    from . import browser
    prepared = set()
    try:
        current, target = _refresh(shell, slot)
        remaining = _closing_windows(current, target, entry)
        for wid in remaining:
            current, target = _refresh(shell, slot)
            candidates = _closing_windows(current, target, entry)
            if wid not in candidates:
                continue
            if wid in browser_windows:
                profile, saved, token = browser_windows[wid]
                # Track cleanup before the native call in case its response
                # is interrupted after the marker was added.
                prepared.add(wid)
                browser.prepare_workspace_window(profile, saved, token)
                current, target = _refresh(shell, slot)
                candidates = _closing_windows(current, target, entry)
                if wid not in candidates:
                    continue
            else:
                # Recheck even if the compositor title stayed the same. A
                # background tab, editor or pane can change independently.
                fresh = _snapshot(cli, current, target, {wid: candidates[wid]})
                saved = _project(entry["snapshot"], lambda item, _: item.get("_native_id") == wid,
                                 include_detached=False)
                if _recipe(fresh) != _recipe(saved):
                    raise CommandError("A window's contents changed after capture; its archived copy was preserved")
                current, target = _refresh(shell, slot)
                candidates = _closing_windows(current, target, entry)
                if wid not in candidates:
                    continue
                from . import file_manager, vscode
                for provider, field in ((file_manager, "file_manager"), (vscode, "vscode")):
                    for record in (fresh.get(field) or {}).get("windows", []):
                        provider.verify_switch_close(record)
            desktop.close_window(wid, workspace=slot["index"], name=slot["name"],
                                 pid=candidates[wid]["pid"], epoch=entry["epoch"])
        deadline = time.monotonic() + CLOSE_WAIT_SECONDS
        while True:
            current, target = _refresh(shell, slot)
            if not _closing_windows(current, target, entry):
                _write(entry, "off")
                return
            if time.monotonic() >= deadline:
                raise CommandError("A window is still open; finish any save prompt and retry")
            time.sleep(0.1)
    except (OSError, RuntimeError, ValueError) as error:
        detail = str(error)
        try:
            current, _ = _refresh(shell, slot, same_capture=False)
            _cancel_browser_markers(browser_windows, (item["id"] for item in current["windows"]
                                                     if item["id"] in prepared))
        except (OSError, RuntimeError, ValueError):
            detail += ". Chrome marker cleanup will resume on retry"
        _write(entry, "closing", detail)
        raise SwitchPartial(detail) from error


def _window_recipes(snapshot: dict):
    """Yield complete provider content identity and the saved native destination."""
    from .browser import browser_window_placement, browser_windows
    from .browser_reconciliation import window_signature
    from .cli import _browser_state
    from .vscode import _identity as code_identity

    for item in snapshot["terminals"]:
        yield item, ("terminal", item["session"]), item["placement"]
    for profile, item in browser_windows(_browser_state(snapshot)):
        active = tuple(index for index, tab in enumerate(item["tabs"]) if tab.get("active"))
        yield item, ("chrome", profile["profile"], window_signature(item), active), browser_window_placement(item)
    for item in (snapshot.get("file_manager") or {}).get("windows", []):
        yield item, ("nemo", tuple(item["locations"]), item["active_tab"]), item["placement"]
    for item in (snapshot.get("vscode") or {}).get("windows", []):
        key = ("vscode", code_identity(item), tuple(sorted(item["editor_uris"])), item["dirty_count"],
               item.get("_unsupported_editors"),
               tuple(sorted((entry["kind"], entry["label"]) for entry in item.get("_switch_blockers") or ())))
        yield item, key, item["placement"]
    for app, record in (snapshot.get("social_apps") or {}).items():
        for item in record["windows"]:
            yield item, ("desktop app", app), item


def _recipe(snapshot: dict) -> dict:
    """Compare native content and tmux layout during Off, including hidden tabs."""
    from .restore import _session_fingerprint

    def session_state(item):
        return (item.get("tmux_identity"), _session_fingerprint(item), [
            [(pane.get("id"), pane.get("command")) for pane in window["panes"]]
            for window in item["windows"]
        ])

    return {
        "windows": {item["_native_id"]: key for item, key, _ in _window_recipes(snapshot)},
        "tmux": {item["name"]: session_state(item) for item in snapshot["sessions"]},
    }


def _verified_windows(saved: dict, live: dict, shell: dict, surviving: set[int],
                      receipts: set[int]) -> set[int]:
    """Assign every recipe to a distinct window using content and compositor placement."""
    from .provider_results import placement_frame_matches
    observed = {}
    rows = {window["id"]: window for window in shell["windows"]}
    for item, key, _ in _window_recipes(live):
        wid = item["_native_id"]
        if wid in observed or wid not in rows:
            raise CommandError("A native window was captured ambiguously after restoration")
        observed[wid] = key
    if not surviving.issubset(observed):
        raise CommandError("A surviving original window could no longer be verified")

    candidates = []
    for _, content, placement in _window_recipes(saved):
        target = desktop.remap_monitor(desktop.remap_workspace(placement, shell=shell), shell=shell)
        choices = [wid for wid, key in observed.items() if wid not in surviving and key == content
                   and wid in receipts and placement_frame_matches(rows[wid], target)]
        candidates.append(choices)
    assigned = {}

    def assign(index, seen):
        for wid in candidates[index]:
            if wid in seen:
                continue
            seen.add(wid)
            if wid not in assigned or assign(assigned[wid], seen):
                assigned[wid] = index
                return True
        return False

    if not all(assign(index, set()) for index in range(len(candidates))):
        raise CommandError("Could not verify each restored window's contents and saved physical placement")
    return set(assigned) | surviving


def _commit_enabled(cli, entry: dict, shell: dict, snapshot: dict) -> None:
    """Publish the verified enabled recipe before releasing its restore fence."""
    try:
        current = storage.load()
    except FileNotFoundError:
        current = {"version": 2, "name": "current", "sessions": [], "terminals": []}
    name = entry["name"].casefold()
    active = _project(current, lambda _, placement: _destination(current, placement) != name,
                      include_detached=True)
    restored = copy.deepcopy(snapshot)
    for item in _records(restored):
        for key in ("_native_id", "_browser_window_id", "_unsupported_editors", "_supports_workspace_switch",
                    "_switch_blockers", "_vscode_instance", "_nemo_native_id", "_nemo_pid"):
            item.pop(key, None)
    owned = {item["name"] for item in restored["sessions"]}
    shared = {item["session"] for item in active["terminals"]}
    active["sessions"] = [item for item in active["sessions"] if item["name"] not in owned or item["name"] in shared]
    restored["sessions"] = [item for item in restored["sessions"] if item["name"] not in shared]
    for key in ("sessions", "terminals"):
        active[key].extend(restored[key])
    # Preserve the active checkpoint's index-to-name mapping for legacy
    # placements. A newly created checkpoint can use the currently observed one.
    active.setdefault("desktop", restored.get("desktop") or {
        "workspace_names": desktop.workspace_names(shell=shell),
    })
    for key in ("file_manager", "vscode"):
        if key in restored:
            if key in active:
                active[key]["windows"].extend(restored[key]["windows"])
            else:
                active[key] = restored[key]
    if "social_apps" in restored:
        apps = active.setdefault("social_apps", {})
        for app, record in restored["social_apps"].items():
            record.pop("_native_window_count", None)
            if app not in apps:
                apps[app] = record
            elif record["windows"]:
                apps[app]["windows"].extend(record["windows"])
                apps[app].update(running=True, mode="windowed")
    if "agent_deck" in restored:
        from .agent_deck_restore import binding
        recorded = active.setdefault("agent_deck", {"version": 1, "sessions": []})["sessions"]
        bindings = {binding(row) for row in recorded}
        recorded.extend(row for row in restored["agent_deck"]["sessions"] if binding(row) not in bindings)
    chrome = cli._browser_state(active)
    browser = cli._browser_state(restored)
    if browser.get("profiles"):
        by_profile = {profile["profile"]: profile for profile in chrome.get("profiles", [])}
        for profile in browser["profiles"]:
            if profile["profile"] not in by_profile:
                by_profile[profile["profile"]] = profile
            else:
                existing = by_profile[profile["profile"]]["windows"]
                used = {window["id"] for window in existing}
                for window in profile["windows"]:
                    if window["id"] in used:
                        window["id"] = "workspace-" + uuid.uuid4().hex
                    existing.append(window)
                    used.add(window["id"])
        cli._set_browser_state(active, {"available": True, "errors": [], "profiles": list(by_profile.values())})
    if active.get("pending"):
        from . import pending
        live = pending.live_keys(active, pending.CATEGORIES)
        active["pending"] = {key: val for key, val in active["pending"].items() if key in live}
    # Aggregate capture attestations are no longer evidence for a merged recipe.
    for key in ("capture_context", "category_provenance"):
        active.pop(key, None)
    storage.save(active)


def _on(cli, shell: dict, slot: dict, entry: dict | None) -> None:
    if entry is None or entry["phase"] == "on":
        return
    from .browser import browser_windows, _workspace_marker
    snapshot = copy.deepcopy(entry["snapshot"])
    _write(entry, "opening", "Waiting for the saved workspace to reopen")
    try:
        current, target = _refresh(shell, slot)
        windows = _windows(current, target)
        originals = set()
        if current["enable_epoch"] == entry["epoch"]:
            for window in current["windows"]:
                wid = window["id"]
                if str(wid) not in entry["originals"]:
                    continue
                if wid not in windows or window["pid"] != entry["originals"][str(wid)]["pid"]:
                    raise CommandError("An original window moved elsewhere; move it back before retrying")
                originals.add(wid)
        # Keep an original window and its unsaved changes after an interrupted
        # Off. Reopen only the windows observed to be absent.
        if originals:
            _cancel_browser_markers(_browser_windows(cli, entry), originals)
            snapshot = _project(snapshot, lambda item, _: item.get("_native_id") not in originals,
                                include_detached=False)
        expected = len(list(_records(snapshot)))
        restored_ids = set()
        browser_ids = set()
        if expected:
            args = argparse.Namespace(category=None, workspace=slot["name"], session=None, select=False,
                                      dry_run=False, no_place=False, verify_codex=True, wait=45,
                                      workspace_switch=True, switch_token=entry["token"],
                                      excluded_windows=frozenset(originals))
            counts = cli._restore(snapshot, args)
            evidence = [item for name in cli.CATEGORIES for item in getattr(counts[name], "results", ())]
            if any(not item.success for item in evidence) or not counts["codex_verified"]:
                raise CommandError("A provider could not verify all restored windows or conversations")
            if sum(int(counts[name]) for name in cli.CATEGORIES) != expected or len(evidence) != expected:
                raise CommandError("A restore provider did not verify every archived window")
            restored_ids = {item.native_window_id for item in evidence if item.native_window_id is not None}
            for profile, window in browser_windows(cli._browser_state(snapshot)):
                name = profile["profile"]
                token = f"{entry['token']}:{name}:{window['id']}"
                status = cli.request_browser("restore_status", {"restore_token": token, "marker": _workspace_marker(token), "window": window},
                                             profile=name, timeout=2)
                if not isinstance(status, dict) or status.get("exists") is not True or type(status.get("window_id")) is not int:
                    raise CommandError("Chrome's exact restored window could no longer be verified")
                browser_ids.add((name, status["window_id"]))
        if expected or originals:
            final, target = _refresh(shell, slot)
            observed = _snapshot(cli, final, target, _windows(final, target), for_close=False)
            final, target = _refresh(shell, slot)
            final_windows = _windows(final, target)
            if any(wid not in final_windows or final_windows[wid]["pid"] != entry["originals"][str(wid)]["pid"]
                   for wid in originals):
                raise CommandError("An original window changed or moved during restoration")
            # A recipe seen during capture counts only if the same window still
            # belongs to this workspace after the provider returns.
            observed = _project(observed, lambda item, _: item.get("_native_id") in final_windows,
                                include_detached=False)
            for profile, window in browser_windows(cli._browser_state(observed)):
                if (profile["profile"], window.get("_browser_window_id")) in browser_ids:
                    restored_ids.add(window["_native_id"])
            verified = _verified_windows(snapshot, observed, final, originals, restored_ids)
            fresh = _project(observed, lambda item, _: item.get("_native_id") in verified, include_detached=False)
            _commit_enabled(cli, entry, final, fresh)
        if browser_ids:
            entry["chrome_cleanup_pending"] = True
        _write(entry, "on")
    except (OSError, RuntimeError, ValueError) as error:
        _write(entry, "opening", str(error))
        raise SwitchPartial(str(error)) from error
    _finish_committed_on(cli, entry)


class SwitchPartial(CommandError):
    """A durable transition needs another observation or user action."""


def cmd_workspace(args: argparse.Namespace) -> int:
    from . import cli
    if args.workspace_command == "list":
        shell = desktop.capture_shell()
        entries = _archives()
        rows = []
        for key, slot in _slots(shell).items():
            if _unreadable(slot["name"]):
                rows.append({"name": slot["name"], "state": "partial", "target": "off",
                             "detail": str(_unreadable_error(slot["name"]))})
                continue
            saved = entries.get(key, {})
            phase = saved.get("phase", "on")
            detail = saved.get("detail", "")
            target = "on" if phase == "opening" else "off"
            if phase == "on" and saved.get("chrome_cleanup_pending"):
                phase, target = "opening", "on"
                detail = detail or "Retry to remove temporary Chrome tabs from the enabled workspace"
            if phase == "off" and (_windows(shell, slot) or slot.get("blocking_windows")):
                phase, detail = "closing", "Windows are present on this switched-off workspace"
                target = "on"
            state = phase if phase in {"on", "off"} else "partial"
            rows.append({"name": slot["name"], "state": state, "detail": detail,
                         **({"target": target} if state == "partial" else {})})
        if args.json:
            print(json.dumps({"workspaces": rows, "busy": _busy()}, ensure_ascii=False))
        else:
            for row in rows:
                print(f"{row['name']}\t{row['state']}\t{row['detail']}")
        return 0

    try:
        with (_switch_lock(), storage.capture_lock(timeout=3), cli._startup_restore_idle(),
              storage.state_lock(wait=False)):
            if cli._graphical_session_ending():
                raise CommandError("The graphical session is ending")
            if args.workspace_command == "forget":
                return _forget(args.name, confirmed=args.yes)
            shell = desktop.capture_shell()
            slot = _slot(shell, args.name)
            if _unreadable(slot["name"]):
                raise _unreadable_error(slot["name"])
            previous = _archives().get(slot["name"].casefold())
            if previous and previous["phase"] != "on" and previous["name"] != slot["name"]:
                raise CommandError("Restore the exact GNOME workspace name before retrying")
            if previous and previous["phase"] == "on":
                _finish_committed_on(cli, previous)
            transition = _off if args.workspace_command == "off" else _on
            transition(cli, shell, slot, previous)
    except SwitchPartial as error:
        print(f"wsctl: {error}", file=sys.stderr)
        return 3
    return 0


def _forget(name: str, *, confirmed: bool) -> int:
    """Delete one workspace's switch archive; the archive is not parsed."""
    path = _path(name)
    if not path.exists():
        raise CommandError(f"Workspace {name} has no switch archive")
    try:
        entry = _load(path)
        saved = sum(1 for _ in _records(entry["snapshot"]))
        what = (f"its {entry['phase']} archive with {saved} saved window(s)"
                if entry["phase"] != "on" else "its archive of an enabled workspace")
    except (ValueError, KeyError, TypeError, ImportError):
        what = "its unreadable archive"
    if not confirmed:
        raise CommandError(f"This discards {what} for workspace {name}: On cannot reopen those windows. Items "
                           "the checkpoint still lists as pending ('wsctl show') return at the next login "
                           "unless dropped with 'wsctl forget KEY'. Repeat with --yes to confirm")
    path.unlink()
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    print(f"Forgot the switch archive of workspace {name}")
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser("workspace", help="switch static named GNOME workspaces on or off")
    commands = parser.add_subparsers(dest="workspace_command", required=True)
    listing = commands.add_parser("list", help="list switchable workspaces")
    listing.add_argument("--json", action="store_true")
    for name in ("on", "off"):
        command = commands.add_parser(name, help=f"switch a named workspace {name}")
        command.add_argument("name")
    forget = commands.add_parser("forget", help="discard one workspace's switch archive")
    forget.add_argument("name")
    forget.add_argument("--yes", action="store_true", help="confirm that the archived windows are discarded")
    parser.set_defaults(func=cmd_workspace)
