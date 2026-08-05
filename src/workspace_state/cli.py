from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .browser import (
    BrowserUnavailable,
    browser_windows,
    capture_browser,
    connected_profiles,
    ensure_browser_profiles,
    restore_browser,
    runtime_dir,
)
from .capture import capture
from .desktop import capture_shell
from .restore import launch_terminal, missing_codex_ids, place_terminal, recreate_tmux
from .resurrect import annotate_state_file, preserve_last_state
from .storage import load, save, state_lock

CATEGORIES = ("terminals", "browsers")
BROWSER_KEY = "google_chrome"


def _browser_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read version-4 browser categories and legacy `chrome` snapshots."""
    browsers = snapshot.get("browsers")
    if isinstance(browsers, dict):
        value = browsers.get(BROWSER_KEY, {})
        return value if isinstance(value, dict) else {}
    value = snapshot.get("chrome", {})
    return value if isinstance(value, dict) else {}


def _set_browser_state(snapshot: dict[str, Any], chrome: dict[str, Any]) -> None:
    snapshot["version"] = 4
    snapshot["browsers"] = {BROWSER_KEY: chrome}
    snapshot.pop("chrome", None)


def _session_workspace(session: dict[str, Any], names: list[str]) -> str:
    return _placement_workspace(session.get("placement"), names)


def _terminal_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return version-2+ terminals or synthesize them from a version-1 snapshot."""
    if "terminals" in snapshot:
        return list(snapshot.get("terminals", []))
    return [
        {"session": session["name"], "placement": session.get("placement")}
        for session in snapshot.get("sessions", []) if session.get("attached")
    ]


def _placement_workspace(placement: dict[str, Any] | None, names: list[str]) -> str:
    saved_name = (placement or {}).get("workspace_name")
    if saved_name:
        return str(saved_name)
    index = int((placement or {}).get("workspace", -1))
    return names[index] if 0 <= index < len(names) else "Unassigned"


def _named_placement(
    placement: dict[str, Any] | None,
    names: list[str],
) -> dict[str, Any] | None:
    if placement is None:
        return None
    result = dict(placement)
    index = int(result.get("workspace", -1))
    if not result.get("workspace_name") and 0 <= index < len(names):
        result["workspace_name"] = names[index]
    return result


def _workspace_groups(snapshot: dict[str, Any]) -> dict[str, dict[str, Any]]:
    names = snapshot.get("desktop", {}).get("workspace_names", [])
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])}

    def empty_group() -> dict[str, Any]:
        return {
            "terminals": [], "session_names": set(), "tmux_windows": 0,
            "codex_ids": set(), "chrome_windows": [], "chrome_tabs": 0,
        }

    groups: dict[str, dict[str, Any]] = {name: empty_group() for name in names}
    groups["Unassigned"] = empty_group()

    for terminal in _terminal_records(snapshot):
        workspace = _placement_workspace(terminal.get("placement"), names)
        group = groups.setdefault(workspace, empty_group())
        group["terminals"].append(terminal)
        group["session_names"].add(terminal["session"])

    assigned_sessions = {terminal["session"] for terminal in _terminal_records(snapshot)}
    groups["Unassigned"]["session_names"].update(set(sessions) - assigned_sessions)
    for group in groups.values():
        group_sessions = [sessions[name] for name in group["session_names"] if name in sessions]
        group["tmux_windows"] = sum(len(session.get("windows", [])) for session in group_sessions)
        group["codex_ids"] = {
            pane["codex"]["session_id"]
            for session in group_sessions for window in session.get("windows", [])
            for pane in window.get("panes", [])
            if pane.get("codex") and pane["codex"].get("session_id")
        }
    for profile, window in browser_windows(_browser_state(snapshot)):
        workspace = str(window.get("workspace") or "Unassigned")
        group = groups.setdefault(workspace, empty_group())
        group["chrome_windows"].append((profile, window))
        group["chrome_tabs"] += len(window.get("tabs", []))
    return groups


def _restore_items(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return one launch item per saved terminal, plus detached tmux sessions."""
    names = snapshot.get("desktop", {}).get("workspace_names", [])
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])}
    terminals = _terminal_records(snapshot)
    items = []
    for terminal in terminals:
        session = sessions.get(terminal["session"])
        if session:
            items.append({
                **session,
                "placement": _named_placement(terminal.get("placement"), names),
                "launch_terminal": True,
            })
    represented = {terminal["session"] for terminal in terminals}
    items.extend(
        {
            **session,
            "placement": _named_placement(session.get("placement"), names),
            "launch_terminal": False,
        }
        for name, session in sessions.items() if name not in represented
    )
    return items


def _terminal_problems(snapshot: dict[str, Any]) -> list[str]:
    missing = [
        terminal for terminal in snapshot.get("terminals", [])
        if terminal.get("placement") is None
    ]
    unresolved = sum(
        1 for session in snapshot.get("sessions", []) for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if pane.get("codex") and not pane["codex"].get("session_id")
    )
    problems = []
    tmux_errors = snapshot.get("capture_errors", {}).get("tmux", [])
    if tmux_errors:
        problems.append(f"tmux capture failed ({'; '.join(tmux_errors)})")
    if not snapshot.get("desktop", {}).get("shell_companion"):
        problems.append("the GNOME companion is unavailable")
    if missing:
        problems.append(f"{len(missing)} Alacritty window(s) have no placement")
    if unresolved:
        problems.append(f"{unresolved} Codex session ID(s) are unresolved")
    session_names = {str(session.get("name")) for session in snapshot.get("sessions", [])}
    dangling = sorted({
        str(terminal.get("session")) for terminal in snapshot.get("terminals", [])
        if terminal.get("session") not in session_names
    })
    if dangling:
        problems.append("Alacritty clients reference missing tmux sessions: " + ", ".join(dangling))
    return problems


def _profile_names(chrome: dict[str, Any]) -> set[str]:
    return {str(profile.get("profile") or "Default") for profile in chrome.get("profiles", [])}


def _browser_problems(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> list[str]:
    chrome = _browser_state(snapshot)
    missing = [
        window for _profile, window in browser_windows(chrome)
        if window.get("workspace_index") is None or window.get("monitor") is None
    ]
    problems = []
    if not chrome.get("available"):
        problems.append("the Google Chrome companion is unavailable")
    if chrome.get("errors"):
        problems.append("a Google Chrome profile capture failed")
    names = [str(profile.get("profile") or "Default") for profile in chrome.get("profiles", [])]
    if len(names) != len(set(names)):
        problems.append("Google Chrome profile labels are not unique")
    if previous:
        missing_profiles = sorted(_profile_names(_browser_state(previous)) - set(names))
        if missing_profiles:
            problems.append("Google Chrome profiles were not captured: " + ", ".join(missing_profiles))
    if missing:
        problems.append(f"{len(missing)} Chrome window(s) have no placement")
    return problems


def _capture_all() -> dict[str, Any]:
    snapshot = capture()
    _set_browser_state(snapshot, capture_browser())
    return snapshot


def _counts(snapshot: dict[str, Any]) -> tuple[int, int, int, int, int]:
    chrome = _browser_state(snapshot)
    chrome_windows = browser_windows(chrome)
    tabs = sum(len(window.get("tabs", [])) for _profile, window in chrome_windows)
    codex = sum(
        1 for session in snapshot.get("sessions", []) for window in session.get("windows", [])
        for pane in window.get("panes", []) if (pane.get("codex") or {}).get("session_id")
    )
    return (
        len(snapshot.get("terminals", [])), len(snapshot.get("sessions", [])), codex,
        len(chrome_windows), tabs,
    )


def cmd_save(args: argparse.Namespace) -> int:
    with state_lock():
        try:
            previous = load()
        except FileNotFoundError:
            previous = None
        snapshot = _capture_all()
        problems = _terminal_problems(snapshot) + _browser_problems(snapshot, previous)
        if problems and not args.allow_partial:
            raise RuntimeError(
                "state not saved: " + "; ".join(problems)
                + ". Fix the integration or pass --allow-partial explicitly."
            )
        path = save(snapshot)
        _arm_autosave()
    terminals, sessions, codex, chrome_windows, tabs = _counts(snapshot)
    print(
        f"Saved {terminals} Alacritty windows, {sessions} tmux sessions, "
        f"{codex} Codex sessions, and {chrome_windows} Chrome windows "
        f"({tabs} tabs) to {path}"
    )
    if problems:
        print("Partial state: " + "; ".join(problems), file=sys.stderr)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    snapshot = load()
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return 0
    print(f"Saved workspace  {snapshot.get('created_at', '')}")
    print("\nWorkspace       Alacritty  tmux sessions  tmux windows  Codex sessions  Chrome  tabs")
    print("--------------- ---------  -------------  ------------  --------------  ------  ----")
    groups = _workspace_groups(snapshot)
    for workspace, group in groups.items():
        if (
            workspace == "Unassigned" and not group["terminals"]
            and not group["session_names"] and not group["chrome_windows"]
        ):
            continue
        print(
            f"{workspace:<15} {len(group['terminals']):>9}  "
            f"{len(group['session_names']):>13}  {group['tmux_windows']:>12}  "
            f"{len(group['codex_ids']):>14}  {len(group['chrome_windows']):>6}  "
            f"{group['chrome_tabs']:>4}"
        )
        if args.details and group["session_names"]:
            print(f"  tmux: {', '.join(sorted(group['session_names']))}")
        if args.details and group["chrome_windows"]:
            labels = [
                f"{profile.get('profile', 'Default')}/{window.get('id', 'window')}"
                for profile, window in group["chrome_windows"]
            ]
            print(f"  Chrome: {', '.join(labels)}")
    if not snapshot.get("desktop", {}).get("shell_companion", False):
        print("\nDesktop placement was not captured; save again after the GNOME companion is active.")
    return 0


def _select(sessions: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    if not shutil.which("fzf"):
        raise RuntimeError("--select requires fzf")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RuntimeError("--select requires an interactive terminal")
    labels = [
        f"{_session_workspace(session, names)}\t{session['name']}\t{index}"
        for index, session in enumerate(sessions)
    ]
    result = subprocess.run(
        [
            "fzf", "--multi", "--delimiter=\t", "--with-nth=1,2",
            "--prompt", "Restore terminals> ", "--header", "TAB selects; ENTER restores",
        ],
        input="\n".join(labels) + "\n", text=True, capture_output=True,
    )
    selected = {
        int(line.rsplit("\t", 1)[1])
        for line in result.stdout.splitlines() if "\t" in line
    }
    return [session for index, session in enumerate(sessions) if index in selected]


def _filtered_terminal_items(snapshot: dict[str, Any], args: argparse.Namespace) -> list[dict[str, Any]]:
    names = snapshot.get("desktop", {}).get("workspace_names", [])
    sessions = _restore_items(snapshot)
    if args.workspace:
        sessions = [
            session for session in sessions
            if _placement_workspace(session.get("placement"), names).casefold() == args.workspace.casefold()
        ]
    if args.session:
        wanted = set(args.session)
        sessions = [session for session in sessions if session["name"] in wanted]
    if args.select:
        sessions = _select(sessions, names)
    return sessions


def _live_terminal_clients() -> dict[str, list[dict[str, Any]]]:
    live = capture()
    errors = list(live.get("capture_errors", {}).get("tmux", []))
    if errors:
        no_server = all(
            any(marker in error.casefold() for marker in (
                "no server running", "failed to connect to server", "error connecting to",
            ))
            for error in errors
        )
        if not no_server:
            raise RuntimeError("cannot safely inspect live tmux clients: " + "; ".join(errors))
    clients: dict[str, list[dict[str, Any]]] = {}
    for terminal in live.get("terminals", []):
        clients.setdefault(str(terminal.get("session") or ""), []).append(terminal)
    return clients


def _restore_terminals(snapshot: dict[str, Any], args: argparse.Namespace) -> int:
    sessions = _filtered_terminal_items(snapshot, args)
    if not sessions:
        return 0
    live_clients = _live_terminal_clients()
    restored_names: dict[str, str] = {}
    for session in sessions:
        saved_name = session["name"]
        actual_name = restored_names.get(saved_name)
        if actual_name is None:
            actual_name, actions = recreate_tmux(
                session,
                dry_run=args.dry_run,
                repair_processes=getattr(args, "repair_processes", False),
                adopt_restored=getattr(args, "adopt_restored", False),
            )
            restored_names[saved_name] = actual_name
            for action in actions:
                print(action)
        if not session.get("launch_terminal", True):
            print(f"restored detached tmux session {actual_name}")
            continue
        live = live_clients.get(actual_name) or []
        client = live.pop(0) if live else None
        placement = session.get("placement")
        if client is not None:
            if placement and not args.no_place:
                result = place_terminal(client, placement, dry_run=args.dry_run)
                print(result.message)
                if not result.success:
                    raise RuntimeError(result.message)
            else:
                print(f"reuse existing Alacritty for {actual_name}")
            continue
        result = launch_terminal(
            {**session, "name": actual_name},
            place=not args.no_place,
            dry_run=args.dry_run,
        )
        print(result.message)
        if not result.success:
            raise RuntimeError(result.message)
    if getattr(args, "verify_codex", False) and not args.dry_run:
        unique_sessions = {
            session["name"]: session for session in sessions
        }
        deadline = time.monotonic() + max(0, args.wait)
        while True:
            missing = {
                session_id
                for saved_name, session in unique_sessions.items()
                for session_id in missing_codex_ids(
                    session, restored_names.get(saved_name, saved_name),
                )
            }
            if not missing:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"{len(missing)} Codex conversation(s) did not resume before startup timeout",
                )
            time.sleep(0.5)
    return len(sessions)


def _selected_browser_windows(snapshot: dict[str, Any], workspace: str | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (profile, window) for profile, window in browser_windows(_browser_state(snapshot))
        if workspace is None
        or str(window.get("workspace") or "").casefold() == workspace.casefold()
    ]


def _restore_browsers(
    snapshot: dict[str, Any],
    args: argparse.Namespace,
    *,
    start_browser: bool = False,
) -> int:
    chrome = _browser_state(snapshot)
    selected = _selected_browser_windows(snapshot, args.workspace)
    if not selected:
        return 0
    if not args.dry_run:
        if start_browser:
            for browser in ensure_browser_profiles(chrome):
                print(f"started {browser} companion")
        connected = set(connected_profiles())
        required = {str(profile.get("profile") or "Default") for profile, _window in selected}
        missing = sorted(required - connected)
        if missing:
            raise RuntimeError("Chrome companion is not connected for profile(s): " + ", ".join(missing))
    token_prefix = hashlib.sha256(
        str(snapshot.get("created_at") or "current").encode(),
    ).hexdigest()[:16]
    for profile, window in selected:
        profile_name = str(profile.get("profile") or "Default")
        label = str(window.get("id") or "window")
        item_key = hashlib.sha256(
            f"{token_prefix}\0{profile_name}\0{label}".encode(),
        ).hexdigest()
        item_marker = _startup_directory() / "browser-items" / f"{item_key}.done"
        if start_browser and not args.dry_run and item_marker.exists():
            print(f"reuse restored Chrome {profile_name}/{label}")
            continue
        one_window = {
            **chrome,
            "profiles": [{**profile, "windows": [window]}],
        }
        results = restore_browser(
            one_window,
            place=not args.no_place,
            dry_run=args.dry_run,
            restore_token_prefix=token_prefix if start_browser else None,
        )
        for result in results:
            print(result.message)
            if not result.success:
                raise RuntimeError(result.message)
        if start_browser and not args.dry_run:
            item_marker.parent.mkdir(parents=True, exist_ok=True)
            item_marker.parent.chmod(0o700)
            item_marker.write_text(f"{snapshot.get('created_at', '')}\n")
            item_marker.chmod(0o600)
    return len(selected)


def _targets(category: str | None) -> tuple[str, ...]:
    return (category,) if category else CATEGORIES


def _restore(snapshot: dict[str, Any], args: argparse.Namespace, *, startup: bool = False) -> dict[str, int]:
    targets = _targets(args.category)
    if args.select and "terminals" not in targets:
        raise ValueError("--select only applies to terminals")
    if args.session and "terminals" not in targets:
        raise ValueError("--session only applies to terminals")
    counts = {"terminals": 0, "browsers": 0}
    if "terminals" in targets:
        counts["terminals"] = _restore_terminals(snapshot, args)
    if "browsers" in targets and not args.session and not args.select:
        counts["browsers"] = _restore_browsers(snapshot, args, start_browser=startup)
    return counts


def cmd_restore(args: argparse.Namespace) -> int:
    counts = _restore(load(), args)
    if not any(counts.values()):
        print("No matching windows or sessions selected.", file=sys.stderr)
        return 1
    return 0


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "current-boot"


def _startup_directory() -> Path:
    root = runtime_dir()
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    directory = root / f"startup-{_boot_id()}"
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    return directory


def _startup_marker(category: str) -> Path:
    return _startup_directory() / f"{category}.done"


def _autosave_marker() -> Path:
    return _startup_directory() / "autosave.ready"


def _tmux_restore_marker() -> Path:
    return _startup_directory() / "tmux-restore.running"


def _tmux_restore_done_marker() -> Path:
    return _startup_directory() / "tmux-restore.done"


def _arm_autosave() -> None:
    marker = _autosave_marker()
    marker.write_text("ready\n")
    marker.chmod(0o600)


@contextmanager
def _startup_lock() -> Iterator[None]:
    path = _startup_directory() / "restore.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _wait_for_shell(timeout: float, capabilities: set[str] | None = None) -> None:
    required = capabilities or set()
    deadline = time.monotonic() + max(0, timeout)
    while True:
        shell = capture_shell()
        available = set(shell.get("capabilities", []))
        if shell.get("available") and required.issubset(available):
            return
        if time.monotonic() >= deadline:
            missing = sorted(required - available)
            detail = f" (missing: {', '.join(missing)})" if missing else ""
            raise RuntimeError(
                "the GNOME companion did not become ready for startup restore" + detail,
            )
        time.sleep(0.1)


def _startup_shell_capabilities(
    snapshot: dict[str, Any],
    pending: list[str],
    args: argparse.Namespace,
) -> set[str]:
    if args.no_place:
        return set()
    required: set[str] = set()
    if "terminals" in pending and any(
        item.get("launch_terminal", True) and item.get("placement")
        for item in _filtered_terminal_items(snapshot, args)
    ):
        required.update({"list_windows", "move_window"})
    if "browsers" in pending and _selected_browser_windows(snapshot, args.workspace):
        required.update({"place_next_window", "placement_status"})
    return required


def _wait_for_tmux_restore(timeout: float, *, await_start: bool = False) -> None:
    deadline = time.monotonic() + max(0, timeout)
    marker = _tmux_restore_marker()
    done = _tmux_restore_done_marker()
    saw_restore = marker.exists()
    while time.monotonic() < deadline:
        if done.exists():
            return
        if marker.exists():
            saw_restore = True
        elif saw_restore or not await_start:
            return
        time.sleep(0.1)
    if marker.exists():
        raise RuntimeError("tmux-resurrect is still running; startup restore was deferred")
    if await_start and not done.exists():
        raise RuntimeError("tmux-continuum did not finish its startup restore in time")


def cmd_startup(args: argparse.Namespace) -> int:
    if args.dry_run:
        return int(not any(_restore(load(), args, startup=True).values()))
    _wait_for_tmux_restore(args.wait, await_start=getattr(args, "await_tmux", False))
    with _startup_lock():
        targets = _targets(args.category)
        pending = [
            category for category in targets
            if args.force or not _startup_marker(category).exists()
        ]
        if not pending:
            print("Startup state is already restored for this boot.")
            return 0
        snapshot = load()
        required_shell = _startup_shell_capabilities(snapshot, pending, args)
        if required_shell:
            _wait_for_shell(args.wait, required_shell)
        for category in pending:
            category_args = argparse.Namespace(**vars(args))
            category_args.category = category
            counts = _restore(snapshot, category_args, startup=True)
            marker = _startup_marker(category)
            marker.write_text(f"{snapshot.get('created_at', '')}\n")
            marker.chmod(0o600)
            if category == "terminals":
                _arm_autosave()
            if counts[category]:
                print(f"Startup restored {counts[category]} {category} item(s).")
            else:
                print(f"Startup has no saved {category} items.")
    return 0


def _autosave_from_tmux() -> tuple[Path | None, list[str]]:
    with state_lock():
        try:
            previous = load()
        except FileNotFoundError:
            previous = {}
        snapshot = capture()
        problems = _terminal_problems(snapshot)
        if previous.get("sessions") and not snapshot.get("sessions"):
            problems.append("tmux capture unexpectedly contains no sessions")
        if problems:
            return None, problems

        captured_chrome = capture_browser()
        prior_chrome = _browser_state(previous)
        captured_by_name = {
            str(profile.get("profile") or "Default"): profile
            for profile in captured_chrome.get("profiles", [])
        }
        prior_by_name = {
            str(profile.get("profile") or "Default"): profile
            for profile in prior_chrome.get("profiles", [])
        }
        merged_profiles = []
        for name in sorted(set(captured_by_name) | set(prior_by_name)):
            captured_profile = captured_by_name.get(name)
            prior_profile = prior_by_name.get(name)
            valid_capture = captured_profile is not None and all(
                window.get("workspace_index") is not None and window.get("monitor") is not None
                for window in captured_profile.get("windows", [])
            )
            if (
                valid_capture and prior_profile
                and prior_profile.get("windows") and not captured_profile.get("windows")
            ):
                valid_capture = False
            if valid_capture:
                merged_profiles.append(captured_profile)
            elif prior_profile is not None:
                merged_profiles.append(prior_profile)
        chrome = {
            "available": bool(merged_profiles),
            "profiles": merged_profiles,
            "errors": list(captured_chrome.get("errors", [])),
        }
        candidate = dict(snapshot)
        _set_browser_state(candidate, chrome)
        return save(candidate), []


def cmd_tmux_save(args: argparse.Namespace) -> int:
    state_file = Path(args.state_file)
    try:
        recipe = load()
    except FileNotFoundError:
        recipe = None
    if not _autosave_marker().exists():
        try:
            protected = preserve_last_state(state_file)
        except FileNotFoundError:
            result = annotate_state_file(state_file, recipe)
            print(
                f"wsctl tmux hook: autosave deferred; created the first tmux state with "
                f"{result['annotated']} contracted Codex pane(s)"
            )
        else:
            print(f"wsctl tmux hook: autosave deferred; preserved {protected}")
        return 0
    try:
        result = annotate_state_file(state_file, recipe)
    except (OSError, RuntimeError, ValueError) as error:
        try:
            protected = preserve_last_state(state_file)
        except (OSError, RuntimeError, ValueError) as preserve_error:
            raise RuntimeError(
                f"tmux state annotation failed ({error}); previous state could not be preserved "
                f"({preserve_error})"
            ) from preserve_error
        print(
            f"wsctl tmux hook: annotation failed; preserved {protected}: {error}",
            file=sys.stderr,
        )
        return 0
    path, problems = _autosave_from_tmux()
    if problems:
        print("wsctl tmux hook: workspace state not updated: " + "; ".join(problems), file=sys.stderr)
    print(
        f"wsctl tmux hook: {result['annotated']} Codex pane(s) contracted, "
        f"{result['unresolved']} unresolved"
        + (f"; saved {path}" if path else "")
    )
    return 0


def cmd_tmux_begin(_args: argparse.Namespace) -> int:
    marker = _tmux_restore_marker()
    marker.write_text(f"{os.getpid()}\n")
    marker.chmod(0o600)
    return 0


def cmd_tmux_restore(args: argparse.Namespace) -> int:
    _tmux_restore_marker().unlink(missing_ok=True)
    done = _tmux_restore_done_marker()
    done.write_text("done\n")
    done.chmod(0o600)
    startup_args = argparse.Namespace(
        category=None, workspace=None, session=None, select=False,
        dry_run=False, no_place=False, force=False, wait=args.wait,
        repair_processes=False, adopt_restored=True, await_tmux=False,
        verify_codex=True,
    )
    return cmd_startup(startup_args)


def cmd_tmux_end(_args: argparse.Namespace) -> int:
    _tmux_restore_marker().unlink(missing_ok=True)
    done = _tmux_restore_done_marker()
    done.write_text("done\n")
    done.chmod(0o600)
    return 0


def cmd_tmux_contract(args: argparse.Namespace) -> int:
    result = annotate_state_file(Path(args.state_file), load(), recipe_only=True)
    print(
        f"wsctl tmux hook: {result['annotated']} Codex pane(s) contracted, "
        f"{result['unresolved']} unresolved",
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="wsctl",
        description="Save and restore the current GNOME workspace state",
    )
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)

    save_parser = sub.add_parser("save", help="replace the saved workspace state")
    save_parser.add_argument(
        "--allow-partial", action="store_true",
        help="save even when a companion, placement, or Codex ID is unavailable",
    )
    save_parser.set_defaults(func=cmd_save)

    show_parser = sub.add_parser("show", help="show the saved state grouped by workspace")
    show_parser.add_argument("--json", action="store_true")
    show_parser.add_argument("--details", action="store_true", help="list tmux and browser window names")
    show_parser.set_defaults(func=cmd_show)

    restore_parser = sub.add_parser("restore", help="restore all state or one category")
    restore_parser.add_argument("category", nargs="?", choices=CATEGORIES)
    restore_parser.add_argument("--workspace", help="restore one named workspace")
    restore_parser.add_argument("--session", action="append", help="restore one tmux session; repeatable")
    restore_parser.add_argument("--select", action="store_true", help="choose terminal sessions with fzf")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--no-place", action="store_true", help="skip GNOME window placement")
    restore_parser.set_defaults(
        func=cmd_restore, repair_processes=False, adopt_restored=False,
        verify_codex=False,
    )

    startup_parser = sub.add_parser("startup", help="restore once per boot and start missing applications")
    startup_parser.add_argument("category", nargs="?", choices=CATEGORIES)
    startup_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    startup_parser.add_argument("--session", action="append", help=argparse.SUPPRESS)
    startup_parser.add_argument("--select", action="store_true", help=argparse.SUPPRESS)
    startup_parser.add_argument("--dry-run", action="store_true")
    startup_parser.add_argument("--no-place", action="store_true", help="skip GNOME window placement")
    startup_parser.add_argument("--force", action="store_true", help="run again during the current boot")
    startup_parser.add_argument("--wait", type=float, default=15, help="seconds to wait for GNOME (default: 15)")
    startup_parser.add_argument("--await-tmux", action="store_true", help=argparse.SUPPRESS)
    startup_parser.set_defaults(
        func=cmd_startup, repair_processes=False, adopt_restored=False,
        verify_codex=True,
    )

    tmux_parser = sub.add_parser("tmux", help="tmux-resurrect/continuum hook interface")
    tmux_sub = tmux_parser.add_subparsers(dest="tmux_command", required=True)
    tmux_begin = tmux_sub.add_parser("begin", help="mark tmux-resurrect restore as running")
    tmux_begin.set_defaults(func=cmd_tmux_begin)
    tmux_save = tmux_sub.add_parser("save", help="annotate a resurrect state file and autosave terminals")
    tmux_save.add_argument("state_file", help="state-file path passed by tmux-resurrect")
    tmux_save.set_defaults(func=cmd_tmux_save)
    tmux_restore = tmux_sub.add_parser("restore", help="place the desktop after continuum restore")
    tmux_restore.add_argument("--wait", type=float, default=15)
    tmux_restore.set_defaults(func=cmd_tmux_restore)
    tmux_end = tmux_sub.add_parser("end", help="finish the continuum restore lifecycle")
    tmux_end.set_defaults(func=cmd_tmux_end)
    tmux_contract = tmux_sub.add_parser("contract", help="contract Codex panes using the saved recipe")
    tmux_contract.add_argument("state_file")
    tmux_contract.set_defaults(func=cmd_tmux_contract)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except (BrowserUnavailable, FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"wsctl: {error}", file=sys.stderr)
        return 2
