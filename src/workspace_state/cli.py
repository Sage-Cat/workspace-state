from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

from . import __version__
from .browser import browser_windows, capture_browser, connected_profiles, restore_browser
from .capture import capture
from .desktop import capture_shell, workspace_names
from .restore import launch_terminal, recreate_tmux
from .storage import list_all, load, save


def _session_workspace(session: dict[str, Any], names: list[str]) -> str:
    return _placement_workspace(session.get("placement"), names)


def _terminal_records(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Return version-2 terminals or synthesize them from a version-1 snapshot."""
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

    groups: dict[str, dict[str, Any]] = {
        name: empty_group() for name in names
    }
    groups["Unassigned"] = empty_group()

    for terminal in _terminal_records(snapshot):
        workspace = _placement_workspace(terminal.get("placement"), names)
        group = groups.setdefault(workspace, empty_group())
        group["terminals"].append(terminal)
        group["session_names"].add(terminal["session"])

    assigned_sessions = {
        terminal["session"] for terminal in _terminal_records(snapshot)
    }
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
    for profile, window in browser_windows(snapshot.get("chrome", {})):
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
            })
    represented = {terminal["session"] for terminal in terminals}
    items.extend(
        {**session, "placement": _named_placement(session.get("placement"), names)}
        for name, session in sessions.items() if name not in represented
    )
    return items


def cmd_save(args: argparse.Namespace) -> int:
    snapshot = capture(args.name)
    missing_placements = [
        terminal for terminal in snapshot.get("terminals", [])
        if terminal.get("placement") is None
    ]
    unresolved_codex = sum(
        1 for session in snapshot["sessions"] for window in session["windows"]
        for pane in window["panes"]
        if pane.get("codex") and not pane["codex"].get("session_id")
    )
    problems = []
    if not snapshot["desktop"]["shell_companion"]:
        problems.append("the GNOME companion is unavailable")
    if missing_placements:
        problems.append(f"{len(missing_placements)} Alacritty window(s) have no placement")
    if unresolved_codex:
        problems.append(f"{unresolved_codex} Codex session ID(s) are unresolved")
    if problems and not args.allow_partial:
        raise RuntimeError(
            "snapshot not saved: " + "; ".join(problems)
            + ". Fix the integration or pass --allow-partial explicitly."
        )

    path = save(snapshot)
    codex_count = sum(
        1 for session in snapshot["sessions"] for window in session["windows"]
        for pane in window["panes"] if (pane.get("codex") or {}).get("session_id")
    )
    print(
        f"Saved {len(snapshot.get('terminals', []))} Alacritty windows, "
        f"{len(snapshot['sessions'])} tmux sessions, and {codex_count} Codex sessions to {path}"
    )
    if problems:
        print("Partial snapshot: " + "; ".join(problems), file=sys.stderr)
    return 0


def _browser_only_snapshot(name: str) -> dict[str, Any]:
    shell = capture_shell()
    names = workspace_names()
    return {
        "version": 3,
        "name": name,
        "created_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "archived": False,
        "desktop": {
            "workspace_names": names,
            "shell_companion": shell.get("available", False),
            "monitors": shell.get("monitors", []),
        },
        "terminals": [],
        "sessions": [],
        "chrome": capture_browser(shell=shell, names=names),
    }


def cmd_snapshot(args: argparse.Namespace) -> int:
    name = args.name or args.target
    if args.target == "browser":
        snapshot = _browser_only_snapshot(name)
    else:
        snapshot = capture(name)
        snapshot["version"] = 3
        snapshot["chrome"] = capture_browser()

    missing_terminals = [
        terminal for terminal in snapshot.get("terminals", [])
        if terminal.get("placement") is None
    ]
    chrome = snapshot.get("chrome", {})
    missing_browser = [
        window for _profile, window in browser_windows(chrome)
        if window.get("workspace_index") is None or window.get("monitor") is None
    ]
    unresolved_codex = sum(
        1 for session in snapshot.get("sessions", []) for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if pane.get("codex") and not pane["codex"].get("session_id")
    )
    problems = []
    if not snapshot.get("desktop", {}).get("shell_companion"):
        problems.append("the GNOME companion is unavailable")
    if not chrome.get("available"):
        problems.append("the Chrome companion is unavailable")
    if missing_terminals:
        problems.append(f"{len(missing_terminals)} Alacritty window(s) have no placement")
    if missing_browser:
        problems.append(f"{len(missing_browser)} Chrome window(s) have no placement")
    if unresolved_codex:
        problems.append(f"{unresolved_codex} Codex session ID(s) are unresolved")
    if problems and not args.allow_partial:
        raise RuntimeError(
            "snapshot not saved: " + "; ".join(problems)
            + ". Fix the integration or pass --allow-partial explicitly."
        )
    path = save(snapshot)
    window_count = len(browser_windows(chrome))
    tab_count = sum(
        len(window.get("tabs", [])) for _profile, window in browser_windows(chrome)
    )
    print(
        f"Saved {window_count} Chrome windows ({tab_count} tabs), "
        f"{len(snapshot.get('terminals', []))} Alacritty windows, and "
        f"{len(snapshot.get('sessions', []))} tmux sessions to {path}"
    )
    if problems:
        print("Partial snapshot: " + "; ".join(problems), file=sys.stderr)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    snapshots = list_all(args.all)
    if args.json:
        print(json.dumps(snapshots, indent=2, ensure_ascii=False))
        return 0
    if not snapshots:
        print("No snapshots.")
        return 0
    for item in snapshots:
        state = "archived" if item.get("archived") else "active"
        chrome_count = len(browser_windows(item.get("chrome", {})))
        print(
            f"{item['name']:<24} {item.get('created_at', '-'):<26} "
            f"{len(item.get('sessions', []))} sessions  {chrome_count} Chrome windows  {state}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    snapshot = load(args.name)
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return 0
    print(f"{snapshot['name']}  {snapshot.get('created_at', '')}")
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


def cmd_restore(args: argparse.Namespace) -> int:
    target = "all"
    snapshot_name = args.name
    if args.name in {"browser", "all", "terminals"}:
        target = args.name
        snapshot_name = args.snapshot_name or args.name
    elif args.snapshot_name:
        raise ValueError("a second snapshot name requires the browser, terminals, or all restore target")
    snapshot = load(snapshot_name)
    names = snapshot.get("desktop", {}).get("workspace_names", [])
    sessions = [] if target == "browser" else _restore_items(snapshot)
    if sessions and args.workspace:
        sessions = [
            session for session in sessions
            if _placement_workspace(session.get("placement"), names).casefold() == args.workspace.casefold()
        ]
    if sessions and args.session:
        wanted = set(args.session)
        sessions = [s for s in sessions if s["name"] in wanted]
    if args.select:
        if target == "browser":
            raise ValueError("--select only applies to terminal sessions")
        sessions = _select(sessions, names)

    restore_chrome = target != "terminals" and not args.session and not args.select
    chrome = snapshot.get("chrome", {}) if restore_chrome else {}
    selected_browser_windows = [
        (profile, window) for profile, window in browser_windows(chrome)
        if args.workspace is None
        or str(window.get("workspace") or "").casefold() == args.workspace.casefold()
    ]
    if selected_browser_windows and not args.dry_run:
        connected = set(connected_profiles())
        required = {str(profile.get("profile") or "Default") for profile, _window in selected_browser_windows}
        missing = sorted(required - connected)
        if missing:
            raise RuntimeError("Chrome companion is not connected for profile(s): " + ", ".join(missing))

    if not sessions and not selected_browser_windows:
        print("No matching windows or sessions selected.", file=sys.stderr)
        return 1
    restored_names: dict[str, str] = {}
    for session in sessions:
        saved_name = session["name"]
        actual_name = restored_names.get(saved_name)
        if actual_name is None:
            actual_name, actions = recreate_tmux(session, dry_run=args.dry_run)
            restored_names[saved_name] = actual_name
            for action in actions:
                print(action)
        print(launch_terminal(
            {**session, "name": actual_name},
            place=not args.no_place,
            dry_run=args.dry_run,
        ))
    for action in restore_browser(
        chrome,
        workspace=args.workspace,
        place=not args.no_place,
        dry_run=args.dry_run,
    ):
        print(action)
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    snapshot = load(args.name)
    snapshot["archived"] = not args.undo
    save(snapshot)
    print(f"{'Unarchived' if args.undo else 'Archived'} snapshot {args.name}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="wsctl", description="Save and restore GNOME workspace recipes")
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)
    save_parser = sub.add_parser("save", help="capture the current state")
    save_parser.add_argument("name")
    save_parser.add_argument(
        "--allow-partial", action="store_true",
        help="save even when placement or Codex IDs cannot be captured",
    )
    save_parser.set_defaults(func=cmd_save)
    snapshot_parser = sub.add_parser("snapshot", help="capture Chrome alone or the whole workspace")
    snapshot_parser.add_argument("target", choices=("browser", "all"))
    snapshot_parser.add_argument("name", nargs="?", help="snapshot name (defaults to the target name)")
    snapshot_parser.add_argument(
        "--allow-partial", action="store_true",
        help="save even when a companion, placement, or Codex ID is unavailable",
    )
    snapshot_parser.set_defaults(func=cmd_snapshot)
    list_parser = sub.add_parser("list", help="list snapshots")
    list_parser.add_argument("--all", action="store_true", help="include archived snapshots")
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(func=cmd_list)
    show_parser = sub.add_parser("show", help="show one snapshot grouped by workspace")
    show_parser.add_argument("name")
    show_parser.add_argument("--json", action="store_true")
    show_parser.add_argument("--details", action="store_true", help="list tmux session names under each workspace")
    show_parser.set_defaults(func=cmd_show)
    restore_parser = sub.add_parser("restore", help="selectively restore a snapshot")
    restore_parser.add_argument("name", help="snapshot name, or browser/all/terminals target")
    restore_parser.add_argument("snapshot_name", nargs="?", help="snapshot name after an explicit target")
    restore_parser.add_argument("--workspace", help="restore one named workspace")
    restore_parser.add_argument("--session", action="append", help="restore one tmux session; repeatable")
    restore_parser.add_argument("--select", action="store_true", help="choose sessions with fzf")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--no-place", action="store_true", help="do not place restored windows")
    restore_parser.set_defaults(func=cmd_restore)
    archive_parser = sub.add_parser("archive", help="hide a snapshot without deleting it")
    archive_parser.add_argument("name")
    archive_parser.add_argument("--undo", action="store_true", help="unarchive it")
    archive_parser.set_defaults(func=cmd_archive)
    return result


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser().parse_args(argv)
        return int(args.func(args))
    except (FileNotFoundError, ValueError, RuntimeError) as error:
        print(f"wsctl: {error}", file=sys.stderr)
        return 2
