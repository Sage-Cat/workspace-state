from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .browser import (
    BROWSER_PROTOCOL_VERSION,
    BROWSER_REQUIRED_CAPABILITIES,
    BrowserUnavailable,
    browser_companion_info,
    browser_windows,
    capture_browser,
    connected_profiles,
    ensure_browser_profiles,
    request_browser,
    restore_browser,
    runtime_dir,
    wait_for_browser_settle,
)
from .capture import capture
from .codex_resume import pending_start_ids, waiting_directory_ids
from .concurrency import completed_jobs
from .desktop import (
    DESKTOP_REQUIRED_CAPABILITIES,
    desktop_readiness,
    desktop_topology_signature,
    capture_shell,
)
from .restore import launch_terminal, missing_codex_ids, place_terminal, recreate_tmux
from .resurrect import annotate_state_file, preserve_last_state
from .storage import load, save, state_lock
from .social_apps import APPS as SOCIAL_APPS, capture_social_apps, restore_social_apps
from .file_manager import capture_file_manager, needs_storage as needs_file_manager_storage, restore_file_manager
from .vscode import UnsafeEditorState, capture_vscode, needs_storage as needs_vscode_storage, restore_vscode
from .login_status import fail_active, set_overall, status_path, update_stage
from .login_status import publish_provider_results
from .provider_results import (EvidenceState, PhaseEvidence, ProviderCount,
                               ProviderItemResult, ProviderRestoreError, waiting_only)
from .startup import StageMarker, read_stage_marker, write_stage_marker
from .shutdown_profiles import (
    SUPPORTED_ACTIONS,
    install_qemu_windows_profile,
    load_profiles,
    probe_profile,
    profile_fingerprint,
    restore_startup_profiles,
)

CATEGORIES = ("terminals", "browsers", "social-apps", "file-manager", "vscode", "virtual-machines")
# Compatibility for callers/tests that used the original file-manager helper.
needs_storage = needs_file_manager_storage
BROWSER_KEY = "google_chrome"
WORKSPACE_RESTORED_TARGET = "wsctl-workspace-restored.target"
TMUX_RESTORE_START_WAIT_SECONDS = 5.0
CODEX_STABLE_SECONDS = 3.0
CODEX_VERIFY_TIMEOUT_SECONDS = 15.0
CODEX_FINALIZE_TIMEOUT_SECONDS = 30.0
TMUX_CODEX_PROCESS_MAPPING = '\"wsctl-codex->wsctl-codex-resume *\"'


class RestoreJobsFailed(RuntimeError):
    """Per-category errors are already published; unrelated jobs may continue."""


@dataclass(frozen=True)
class TerminalRestoreOutcome:
    restored: int
    codex_ready: int
    codex_total: int
    codex_verified: bool
    codex_deferred: bool = False


def _browser_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Read version-4 browser categories and legacy `chrome` snapshots."""
    browsers = snapshot.get("browsers")
    if isinstance(browsers, dict):
        value = browsers.get(BROWSER_KEY, {})
        return value if isinstance(value, dict) else {}
    value = snapshot.get("chrome", {})
    return value if isinstance(value, dict) else {}


def _browser_restore_token_prefix(snapshot: dict[str, Any]) -> str:
    """Keep restore claims stable while unrelated checkpoint categories change."""
    recipe = json.dumps(
        _browser_state(snapshot), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(recipe).hexdigest()[:16]


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
            "file_manager_windows": 0,
            "vscode_windows": 0,
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
    for window in (snapshot.get("file_manager") or {}).get("windows", []):
        placement = window.get("placement") or {}
        workspace = str(placement.get("workspace_name") or "Unassigned")
        group = groups.setdefault(workspace, empty_group())
        group["file_manager_windows"] += 1
    for window in (snapshot.get("vscode") or {}).get("windows", []):
        placement = window.get("placement") or {}
        workspace = str(placement.get("workspace_name") or window.get("workspace") or "Unassigned")
        groups.setdefault(workspace, empty_group())["vscode_windows"] += 1
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
    # Category capture is read-only; only the coordinator merges it into the
    # canonical recipe while cmd_save retains its cross-process state lock.
    from .checkpoint import CaptureContext
    context = CaptureContext.begin()
    def social():
        return capture_social_apps(context.shell)
    def file_manager():
        return capture_file_manager(context.shell)
    def vscode():
        return capture_vscode(context.shell)
    values, errors = {}, {}
    for name, result, error in completed_jobs({
        "terminals": lambda: capture(shell=context.shell, names=list(context.names)),
        "browsers": lambda: capture_browser(shell=context.shell, names=list(context.names)),
        "social_apps": social, "file_manager": file_manager, "vscode": vscode,
    }):
        if error is not None:
            errors[name] = error
        else:
            values[name] = result
    for name in ("terminals", "browsers", "social_apps"):
        if name in errors:
            raise errors[name]
    snapshot = values["terminals"]
    _set_browser_state(snapshot, values["browsers"])
    snapshot["social_apps"] = values["social_apps"]
    if "file_manager" in errors:
        snapshot.setdefault("capture_errors", {}).setdefault("file_manager", []).append(str(errors["file_manager"]))
    else:
        snapshot["file_manager"] = values["file_manager"]
    if "vscode" in errors:
        if isinstance(errors["vscode"], UnsafeEditorState):
            raise errors["vscode"]
        snapshot.setdefault("capture_errors", {}).setdefault("vscode", []).append(str(errors["vscode"]))
    else:
        snapshot["vscode"] = values["vscode"]
    context.verify()
    snapshot["capture_context"] = context.evidence({
        name: {"state": "failed" if name in errors else "captured",
               "detail": str(errors[name]) if name in errors else "provider capture completed"}
        for name in ("terminals", "browsers", "social_apps", "file_manager", "vscode")
    })
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


def _fallback_monitor_problem(
    snapshot: dict[str, Any], previous: dict[str, Any] | None,
) -> str | None:
    """Reject explicit fallback output without treating absent legacy data as failure."""
    unknown = {"", "unknown", "none", "n/a"}

    def identity_values(monitor):
        identity = monitor.get("identity")
        identity = identity if isinstance(identity, dict) else monitor
        return [str(identity.get(key) or "").strip().lower()
                for key in ("edid_hash", "edid_checksum", "vendor", "product", "serial")]

    def connector(monitor):
        identity = monitor.get("identity")
        return str(monitor.get("connector") or
                   (identity.get("connector") if isinstance(identity, dict) else "") or "").strip().lower()

    def fallback(monitor):
        name = connector(monitor)
        if name == "unknown" or name == "none" or name.startswith("none-"):
            return True
        values = identity_values(monitor)
        return any(value in unknown - {""} for value in values) and all(value in unknown for value in values)

    prior_monitors = (previous or {}).get("desktop", {}).get("monitors", [])
    physical = any(
        isinstance(item, dict) and not fallback(item)
        and (any(value not in unknown for value in identity_values(item))
             or connector(item).startswith(("dp-", "hdmi-", "edp-", "dvi-", "vga-")))
        for item in prior_monitors
    )
    if physical and any(
        isinstance(item, dict) and fallback(item)
        for item in snapshot.get("desktop", {}).get("monitors", [])
    ):
        return "fallback or unknown display identity would replace the saved physical monitor layout"
    return None


def _retain_unrestored_recipes(
    snapshot: dict[str, Any], previous: dict[str, Any] | None,
) -> dict[str, str]:
    """An app absent after failed startup is not proof of an intentional close.

    The HUD is already in shutdown mode here. Per-login completion markers
    retain the startup result after its stages have been replaced in the HUD.
    """
    previous = previous or {}
    retained: dict[str, str] = {}
    recipes = {
        "browsers": _browser_state(previous),
        "social-apps": previous.get("social_apps"),
        "file-manager": previous.get("file_manager"),
        "vscode": previous.get("vscode"),
    }
    for category, recipe in recipes.items():
        if not isinstance(recipe, dict):
            continue
        if category == "browsers":
            has_items = bool(browser_windows(recipe))
        elif category == "social-apps":
            has_items = any(
                isinstance(item, dict) and (item.get("windows") or item.get("mode") in {"visible", "background"})
                for item in recipe.values()
            )
        else:
            has_items = bool(recipe.get("windows"))
        if not has_items:
            continue
        completion = read_stage_marker(_startup_marker(category), category)
        if completion is not None and completion.verified_for_login(_marker_context()):
            continue
        if category == "browsers":
            _set_browser_state(snapshot, copy.deepcopy(recipe))
        else:
            snapshot[category.replace("-", "_")] = copy.deepcopy(recipe)
        retained[category] = (
            f"retained the saved {category} recipe because restoration did not complete this login"
        )
    if retained:
        snapshot.setdefault("capture_errors", {}).setdefault("preserved_categories", []).extend(retained.values())
    return retained


def cmd_save(args: argparse.Namespace) -> int:
    shutdown_safe = bool(getattr(args, "shutdown_safe", False))
    if shutdown_safe and not _shutdown_allows_unresolved_codex():
        raise RuntimeError(
            "--shutdown-safe is valid only inside the active verified shutdown transaction"
        )
    with state_lock():
        try:
            previous = load()
        except FileNotFoundError:
            previous = None
        if shutdown_safe:
            update_stage("social-apps-save", "running", "Capturing visible windows and background/stopped state", current=0, total=4)
            update_stage("file-manager-save", "running", "Capturing default file manager windows and placement", current=0, total=1)
            update_stage("vscode-save", "running", "Capturing VS Code workspaces", current=0, total=1)
        try:
            snapshot = _capture_all()
        except (RuntimeError, OSError, ValueError) as error:
            if shutdown_safe:
                stage = "vscode-save" if isinstance(error, UnsafeEditorState) else "social-apps-save"
                update_stage(stage, "failed", str(error), error=str(error))
            raise
        monitor_problem = _fallback_monitor_problem(snapshot, previous)
        if monitor_problem:
            raise RuntimeError("state not saved: " + monitor_problem + ". The existing checkpoint was preserved.")
        retained = _retain_unrestored_recipes(snapshot, previous) if shutdown_safe else {}
        terminal_problems = _terminal_problems(snapshot)
        browser_problems = _browser_problems(snapshot, previous)
        file_manager_problems = list(snapshot.get("capture_errors", {}).get("file_manager", []))
        if file_manager_problems:
            file_manager_problems = ["file manager capture failed: " + item for item in file_manager_problems]
        vscode_problems = list(snapshot.get("capture_errors", {}).get("vscode", []))
        if vscode_problems:
            vscode_problems = ["VS Code capture failed: " + item for item in vscode_problems]
        vscode_unsafe = bool(snapshot.get("capture_errors", {}).get("vscode_unsafe"))
        if shutdown_safe and vscode_problems:
            update_stage("vscode-save", "failed", "; ".join(vscode_problems), error="; ".join(vscode_problems))
        if shutdown_safe:
            # A live Codex process can briefly lack a provable rollout UUID
            # while it starts or compacts. Saving that pane as a plain shell is
            # safer than discarding every other current workspace change. No
            # other terminal defect is safe to downgrade automatically.
            unsafe_terminal = [
                problem for problem in terminal_problems
                if not problem.endswith("Codex session ID(s) are unresolved")
            ]
            if unsafe_terminal:
                raise RuntimeError(
                    "state not saved: " + "; ".join(unsafe_terminal)
                    + ". The shutdown inhibitor remains active."
                )
            if browser_problems:
                previous_browser = _browser_state(previous or {})
                if not previous_browser or not previous_browser.get("available"):
                    raise RuntimeError(
                        "state not saved: " + "; ".join(browser_problems)
                        + ". No last-good browser checkpoint is available. "
                        "The shutdown inhibitor remains active."
                    )
                _set_browser_state(snapshot, previous_browser)
                browser_problems = [
                    "retained the last-good browser checkpoint because current "
                    "capture was incomplete: " + "; ".join(browser_problems)
                ]
            if file_manager_problems:
                previous_file_manager = (previous or {}).get("file_manager")
                if previous_file_manager:
                    snapshot["file_manager"] = previous_file_manager
                    file_manager_problems = [
                        "retained the last-good file manager checkpoint because current capture was incomplete: "
                        + "; ".join(file_manager_problems)
                    ]
            if vscode_problems:
                previous_vscode = (previous or {}).get("vscode")
                prior_windows = (previous_vscode or {}).get("windows", []) if isinstance(previous_vscode, dict) else []
                if vscode_unsafe:
                    raise RuntimeError("state not saved: " + "; ".join(vscode_problems) + ". VS Code has unsaved edits with native recovery disabled. The shutdown inhibitor remains active.")
                if prior_windows:
                    snapshot["vscode"] = previous_vscode
                    vscode_problems = ["retained the last-good VS Code checkpoint because current capture was incomplete: " + "; ".join(vscode_problems)]
                else:
                    raise RuntimeError("state not saved: " + "; ".join(vscode_problems) + ". No last-good VS Code checkpoint is available. The shutdown inhibitor remains active.")
        elif vscode_problems:
            previous_vscode = (previous or {}).get("vscode")
            if isinstance(previous_vscode, dict):
                snapshot["vscode"] = previous_vscode
                vscode_problems = ["retained the last-good VS Code checkpoint because current capture was incomplete: " + "; ".join(vscode_problems)]
        problems = terminal_problems + browser_problems + file_manager_problems + vscode_problems + list(retained.values())
        if problems and not args.allow_partial:
            raise RuntimeError(
                "state not saved: " + "; ".join(problems)
                + ". Fix the integration or pass --allow-partial explicitly."
            )
        from .checkpoint import verify_capture_context
        verify_capture_context(snapshot)
        path = save(snapshot)
        if shutdown_safe:
            records = snapshot.get("social_apps", {})
            for index, app in enumerate(SOCIAL_APPS, 1):
                record = records.get(app.id, {})
                update_stage(
                    "social-apps-save", "running",
                    f"{app.label}: saved {record.get('mode', 'unknown')}, {len(record.get('windows', []))} window(s)",
                    current=index, total=len(SOCIAL_APPS),
                )
            update_stage("social-apps-save", "degraded" if "social-apps" in retained else "ready",
                         retained.get("social-apps", "Social app visibility and placement saved"), current=4, total=4)
            if file_manager_problems:
                update_stage("file-manager-save", "degraded" if (previous or {}).get("file_manager") else "failed", "; ".join(file_manager_problems), error="; ".join(file_manager_problems))
            elif "file-manager" in retained:
                update_stage("file-manager-save", "degraded", retained["file-manager"])
            else:
                update_stage("file-manager-save", "ready", "Default file manager windows and placement saved", current=len((snapshot.get("file_manager") or {}).get("windows", [])), total=len((snapshot.get("file_manager") or {}).get("windows", [])))
            if vscode_problems:
                update_stage("vscode-save", "failed", "; ".join(vscode_problems), error="; ".join(vscode_problems))
            elif "vscode" in retained:
                update_stage("vscode-save", "degraded", retained["vscode"])
            else:
                update_stage("vscode-save", "ready", "VS Code workspaces saved", current=len((snapshot.get("vscode") or {}).get("windows", [])), total=len((snapshot.get("vscode") or {}).get("windows", [])))
        _arm_autosave()
    terminals, sessions, codex, chrome_windows, tabs = _counts(snapshot)
    print(
        f"Saved {terminals} Alacritty windows, {sessions} tmux sessions, "
        f"{codex} Codex sessions, and {chrome_windows} Chrome windows "
        f"({tabs} tabs) to {path}"
    )
    for app in SOCIAL_APPS:
        record = snapshot.get("social_apps", {}).get(app.id)
        if record:
            print(f"  {app.label}: {record['mode']}, {len(record['windows'])} saved window(s)")
    if snapshot.get("file_manager"):
        print(f"  Default file manager: {len(snapshot['file_manager'].get('windows', []))} saved window(s)")
    if snapshot.get("vscode"):
        print(f"  VS Code workspaces: {len(snapshot['vscode'].get('windows', []))} saved window(s)")
    if problems:
        print("Partial state: " + "; ".join(problems), file=sys.stderr)
    return 3 if shutdown_safe and problems else 0


def cmd_show(args: argparse.Namespace) -> int:
    snapshot = load()
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return 0
    print(f"Saved workspace  {snapshot.get('created_at', '')}")
    print("\nWorkspace       Alacritty  tmux sessions  tmux windows  Codex sessions  Chrome  tabs  Files  VS Code")
    print("--------------- ---------  -------------  ------------  --------------  ------  ----  -----  -------")
    groups = _workspace_groups(snapshot)
    for workspace, group in groups.items():
        if (
            workspace == "Unassigned" and not group["terminals"]
            and not group["session_names"] and not group["chrome_windows"]
            and not group["file_manager_windows"] and not group["vscode_windows"]
        ):
            continue
        print(
            f"{workspace:<15} {len(group['terminals']):>9}  "
            f"{len(group['session_names']):>13}  {group['tmux_windows']:>12}  "
            f"{len(group['codex_ids']):>14}  {len(group['chrome_windows']):>6}  "
            f"{group['chrome_tabs']:>4}  {group['file_manager_windows']:>6}  {group['vscode_windows']:>6}"
        )
        if args.details and group["session_names"]:
            print(f"  tmux: {', '.join(sorted(group['session_names']))}")
        if args.details and group["chrome_windows"]:
            labels = [
                f"{profile.get('profile', 'Default')}/{window.get('id', 'window')}"
                for profile, window in group["chrome_windows"]
            ]
            print(f"  Chrome: {', '.join(labels)}")
        if args.details and group["file_manager_windows"]:
            print(f"  Default file manager: {group['file_manager_windows']} window(s)")
        if args.details and group["vscode_windows"]:
            print(f"  VS Code: {group['vscode_windows']} workspace(s)")
    if not snapshot.get("desktop", {}).get("shell_companion", False):
        print("\nDesktop placement was not captured; save again after the GNOME companion is active.")
    for app in SOCIAL_APPS:
        record = snapshot.get("social_apps", {}).get(app.id)
        if record:
            locations = ", ".join(
                f"{item['workspace_name']} / {(item.get('monitor_identity') or {}).get('connector', item.get('monitor'))}"
                for item in record["windows"]
            )
            print(f"  {app.label}: {record['mode']}" + (f" — {locations}" if locations else " — will not launch"))
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


def _restore_terminals(
    snapshot: dict[str, Any],
    args: argparse.Namespace,
) -> TerminalRestoreOutcome:
    sessions = _filtered_terminal_items(snapshot, args)
    report_status = bool(getattr(args, "login_status", False))
    total_sessions = len(sessions)
    codex_ids = {
        str((pane.get("codex") or {}).get("session_id"))
        for session in sessions
        for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if (pane.get("codex") or {}).get("session_id")
    }
    if report_status:
        update_stage(
            "terminals", "running" if sessions else "skipped",
            "Restoring Alacritty and tmux sessions" if sessions else "No saved terminal sessions",
            current=0, total=total_sessions,
        )
        update_stage(
            "codex", "running" if codex_ids else "skipped",
            "Waiting for saved Codex conversations" if codex_ids else "No saved Codex conversations",
            current=0, total=len(codex_ids),
        )
    if not sessions:
        return TerminalRestoreOutcome(0, 0, 0, True)
    live_clients = _live_terminal_clients()
    restored_names: dict[str, str] = {}
    for session_index, session in enumerate(sessions, start=1):
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
            if report_status:
                update_stage(
                    "terminals", "running", f"Restored tmux session {actual_name}",
                    current=session_index, total=total_sessions,
                )
            continue
        live = live_clients.get(actual_name) or []
        client = live.pop(0) if live else None
        placement = session.get("placement")
        if report_status and placement and not args.no_place:
            update_stage(
                "terminals", "running", f"Verifying Alacritty workspace/display for {actual_name}",
                current=session_index - 1, total=total_sessions,
            )
        if client is not None:
            if placement and not args.no_place:
                result = place_terminal(client, placement, dry_run=args.dry_run)
                print(result.message)
                if not result.success:
                    raise RuntimeError(result.message)
            else:
                print(f"reuse existing Alacritty for {actual_name}")
            if report_status:
                update_stage(
                    "terminals", "running", f"Restored Alacritty for {actual_name}",
                    current=session_index, total=total_sessions,
                )
            continue
        result = launch_terminal(
            {**session, "name": actual_name},
            place=not args.no_place,
            dry_run=args.dry_run,
        )
        print(result.message)
        if not result.success:
            raise RuntimeError(result.message)
        if report_status:
            update_stage(
                "terminals", "running", f"Restored Alacritty for {actual_name}",
                current=session_index, total=total_sessions,
            )
    codex_ready = len(codex_ids)
    codex_verified = True
    codex_deferred = False
    if getattr(args, "verify_codex", False) and not args.dry_run:
        unique_sessions = {
            session["name"]: session for session in sessions
        }
        deadline = time.monotonic() + min(
            max(0, args.wait),
            # Restores initialize serially to avoid competing SQLite opens.
            # Keep one bounded allowance per conversation, capped by --wait.
            max(CODEX_VERIFY_TIMEOUT_SECONDS, 5.0 * len(codex_ids)),
        )
        stable_since: float | None = None
        missing = set(codex_ids)
        while True:
            missing = {
                session_id
                for saved_name, session in unique_sessions.items()
                for session_id in missing_codex_ids(
                    session, restored_names.get(saved_name, saved_name),
                )
            }
            now = time.monotonic()
            if missing and missing <= waiting_directory_ids():
                # Cloud mounts start only after terminal restoration finishes.
                # Let the finalizer mount them before verifying these wrappers.
                codex_verified = False
                codex_deferred = True
                codex_ready = len(codex_ids) - len(missing)
                if report_status:
                    update_stage(
                        "codex", "waiting",
                        f"{len(missing)} Codex conversation(s) waiting for saved directories; "
                        "verification deferred until cloud drives mount",
                        current=codex_ready, total=len(codex_ids),
                    )
                break
            if missing:
                stable_since = None
            elif stable_since is None:
                stable_since = now
            stable = (
                not missing
                and stable_since is not None
                and now - stable_since
                >= min(CODEX_STABLE_SECONDS, max(0, args.wait))
            )
            if report_status:
                update_stage(
                    "codex", "ready" if stable else "running",
                    (
                        f"Waiting for {len(missing)} Codex conversation(s)"
                        if missing
                        else (
                            "All saved Codex conversations resumed"
                            if stable
                            else "Verifying resumed Codex conversations remain live"
                        )
                    ),
                    current=len(codex_ids) - len(missing), total=len(codex_ids),
                )
            if stable:
                break
            if now >= deadline:
                codex_verified = False
                codex_ready = len(codex_ids) - len(missing)
                if report_status:
                    update_stage(
                        "codex", "degraded",
                        (
                            f"{len(missing)} Codex conversation(s) could not be verified; "
                            "check their tmux panes for startup errors or prompts"
                            if missing else
                            "Codex stability verification timed out; check the restored tmux panes"
                        ),
                        current=codex_ready, total=len(codex_ids),
                    )
                break
            time.sleep(0.5)
    return TerminalRestoreOutcome(
        len(sessions), codex_ready, len(codex_ids), codex_verified, codex_deferred,
    )


def _selected_browser_windows(snapshot: dict[str, Any], workspace: str | None) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (profile, window) for profile, window in browser_windows(_browser_state(snapshot))
        if workspace is None
        or str(window.get("workspace") or "").casefold() == workspace.casefold()
    ]


def _close_startup_browser_duplicates(
    startup_native_windows: dict[str, set[int]],
    restore_tokens: dict[str, list[str]],
) -> int:
    """Close only unclaimed windows from Chrome's just-restored native session."""
    duplicates: dict[str, dict[int, str]] = {}
    for profile_name, initial_ids in startup_native_windows.items():
        tokens = restore_tokens.get(profile_name, [])
        keepers: set[int] = set()
        for restore_token in tokens:
            status = request_browser(
                "restore_status",
                {"restore_token": restore_token},
                profile=profile_name,
            )
            if not isinstance(status, dict) or not status.get("exists"):
                raise BrowserUnavailable(
                    f"refusing duplicate cleanup because Chrome keeper {restore_token!r} is missing",
                )
            window_id = status.get("window_id")
            if not isinstance(window_id, int):
                raise BrowserUnavailable(
                    f"refusing duplicate cleanup because Chrome keeper {restore_token!r} has no window ID",
                )
            if status.get("urls_restored") is not True:
                raise BrowserUnavailable(
                    "refusing duplicate cleanup because Chrome keeper URLs are unverified",
                )
            keepers.add(window_id)
        if len(keepers) != len(tokens):
            raise BrowserUnavailable(
                f"refusing duplicate cleanup because profile {profile_name!r} "
                "does not have one distinct keeper per saved window",
            )
        live = request_browser("list_windows", {}, profile=profile_name)
        if not isinstance(live, list):
            raise BrowserUnavailable(
                f"Chrome returned an invalid window list for profile {profile_name!r}",
            )
        live_ids = {
            int(window["id"])
            for window in live
            if isinstance(window, dict) and isinstance(window.get("id"), int)
        }
        keeper_signatures = {
            window["full_signature"] for window in live
            if isinstance(window, dict) and window.get("id") in keepers
            and window.get("urls_loaded") is True
            and isinstance(window.get("full_signature"), str)
            and window["full_signature"]
        }
        # A native-session window can contain unrelated, newer user tabs.
        # Only exact, fully loaded duplicates of verified keepers are eligible.
        duplicates[profile_name] = {
            window["id"]: window["full_signature"] for window in live
            if isinstance(window, dict) and isinstance(window.get("id"), int)
            and window["id"] in (initial_ids & live_ids) - keepers
            and window.get("urls_loaded") is True
            and window.get("full_signature") in keeper_signatures
        }

    for profile_name, window_ids in duplicates.items():
        for window_id in sorted(window_ids):
            result = request_browser(
                "close_restored_window",
                {"window_id": window_id, "created": True,
                 "expected_full_signature": window_ids[window_id]},
                profile=profile_name,
            )
            if not isinstance(result, dict) or not result.get("closed"):
                raise BrowserUnavailable(
                    f"Chrome did not close duplicate window {window_id} in profile {profile_name!r}",
                )
    return sum(len(window_ids) for window_ids in duplicates.values())


def _restore_browsers(
    snapshot: dict[str, Any],
    args: argparse.Namespace,
    *,
    start_browser: bool = False,
) -> int:
    chrome = _browser_state(snapshot)
    selected = _selected_browser_windows(snapshot, args.workspace)
    report_status = bool(getattr(args, "login_status", False))
    total_windows = len(selected)
    if report_status:
        update_stage(
            "browsers", "running" if selected else "skipped",
            "Restoring Chrome workspaces" if selected else "No saved Chrome windows",
            current=0, total=total_windows,
        )
    if not selected:
        return 0
    startup_native_windows: dict[str, set[int]] = {}
    launched_browsers: list[str] = []
    connected_before_start: set[str] = set()
    if not args.dry_run:
        if start_browser:
            connected_before_start = set(connected_profiles())
            launched_browsers = ensure_browser_profiles(chrome)
            for browser in launched_browsers:
                print(f"started {browser} companion")
        connected = set(connected_profiles())
        required = {str(profile.get("profile") or "Default") for profile, _window in selected}
        missing = sorted(required - connected)
        if missing:
            raise RuntimeError("Chrome companion is not connected for profile(s): " + ", ".join(missing))
        for profile_name in sorted(required):
            info = browser_companion_info(profile_name)
            capabilities = set(info.get("capabilities", []))
            if (
                int(info.get("protocol_version") or 0) < BROWSER_PROTOCOL_VERSION
                or not BROWSER_REQUIRED_CAPABILITIES.issubset(capabilities)
            ):
                raise BrowserUnavailable(
                    f"Chrome companion for profile {profile_name!r} is outdated; "
                    "reload the Workspace State Companion before browser restore",
                )
        if start_browser:
            if report_status:
                update_stage(
                    "browsers", "running", "Waiting for Chrome's window and tab list to stabilize",
                    current=0, total=total_windows,
                )
            wait_for_browser_settle(required, timeout=getattr(args, "wait", 15))
            # Only a Chrome instance wsctl started is eligible for automatic
            # cleanup. Freeze its native-session window IDs before wsctl can
            # create anything, then remove unclaimed members of that exact set
            # only after every saved window has a distinct live keeper.
            if launched_browsers and args.workspace is None:
                for profile_name in sorted(required - connected_before_start):
                    windows = request_browser("list_windows", {}, profile=profile_name)
                    if not isinstance(windows, list):
                        raise BrowserUnavailable(
                            f"Chrome returned an invalid window list for profile {profile_name!r}",
                        )
                    startup_native_windows[profile_name] = {
                        int(window["id"])
                        for window in windows
                        if isinstance(window, dict) and isinstance(window.get("id"), int)
                    }
    token_prefix = _browser_restore_token_prefix(snapshot)
    completed = 0
    failures: list[str] = []
    evidence: list[ProviderItemResult] = []
    for profile, window in selected:
        profile_name = str(profile.get("profile") or "Default")
        label = str(window.get("id") or "window")
        evidence_before = len(evidence)
        try:
            item_key = hashlib.sha256(
                f"{token_prefix}\0{profile_name}\0{label}".encode(),
            ).hexdigest()
            item_marker = _startup_directory() / "browser-items" / f"{item_key}.done"
            restore_token = f"{token_prefix}:{profile_name}:{label}"
            if start_browser and not args.dry_run:
                status = request_browser(
                    "restore_status",
                    {"restore_token": restore_token, "window": window},
                    profile=profile_name,
                )
                if isinstance(status, dict) and status.get("exists"):
                    prior = read_stage_marker(item_marker, "browsers")
                    if prior is not None and prior.provider_results and status.get("urls_restored") is True:
                        from .provider_progress import evidence_from_dict
                        restored_evidence = [evidence_from_dict(item) for item in prior.provider_results]
                        evidence.extend(restored_evidence)
                        if not all(item.success for item in restored_evidence):
                            failures.append(f"{profile_name}/{label}: existing restore awaits verification")
                            continue
                        print(f"reuse restored Chrome {profile_name}/{label}")
                        completed += 1
                        if report_status:
                            update_stage(
                                "browsers", "running", f"Reused Chrome {profile_name}/{label}",
                                current=completed, total=total_windows,
                            )
                        continue
                    # A live token without the commit marker is an interrupted
                    # placement. Reuse and reposition it; never close a window
                    # that Chrome restored from its own previous session.
                item_marker.unlink(missing_ok=True)
            one_window = {
                **chrome,
                "profiles": [{**profile, "windows": [window]}],
            }
            results = restore_browser(
                one_window,
                place=not args.no_place,
                dry_run=args.dry_run,
                restore_token_prefix=token_prefix,
            )
            for result in results:
                print(result.message)
                if result.evidence is not None:
                    evidence.append(result.evidence)
            item_evidence = evidence[evidence_before:]
            unsuccessful = [result for result in results if not result.success]
            if unsuccessful:
                if start_browser and not args.dry_run and waiting_only(item_evidence):
                    _write_attempt_marker(item_marker, "browsers", snapshot, "waiting", item_evidence,
                                          unsuccessful[0].message)
                raise ProviderRestoreError("; ".join(result.message for result in unsuccessful), item_evidence)
            if start_browser and not args.dry_run:
                item_marker.parent.mkdir(parents=True, exist_ok=True)
                item_marker.parent.chmod(0o700)
                _write_attempt_marker(item_marker, "browsers", snapshot, "ready", item_evidence)
            completed += 1
            if report_status:
                update_stage(
                    "browsers", "running", f"Restored Chrome {profile_name}/{label}",
                    current=completed, total=total_windows,
                )
        except (BrowserUnavailable, RuntimeError) as error:
            failures.append(f"{profile_name}/{label}: {error}")
            if len(evidence) == evidence_before:
                evidence.append(ProviderItemResult("chrome", f"{profile_name}/{label}",
                    PhaseEvidence(EvidenceState.FAILED, str(error), True)))
            if report_status:
                update_stage(
                    "browsers", "running",
                    f"Chrome {profile_name}/{label} needs attention; continuing other windows: {error}",
                    current=completed, total=total_windows,
                )
    if failures:
        raise ProviderRestoreError(
            f"Restored {completed}/{total_windows} Chrome window(s); " + "; ".join(failures), evidence,
        )
    if startup_native_windows:
        restore_tokens: dict[str, list[str]] = {}
        for profile, window in selected:
            profile_name = str(profile.get("profile") or "Default")
            label = str(window.get("id") or "window")
            restore_tokens.setdefault(profile_name, []).append(
                f"{token_prefix}:{profile_name}:{label}",
            )
        closed = _close_startup_browser_duplicates(
            startup_native_windows,
            restore_tokens,
        )
        if closed:
            print(f"closed {closed} duplicate Chrome window(s)")
    return ProviderCount(len(selected), evidence)


def _targets(category: str | None) -> tuple[str, ...]:
    return (category,) if category else CATEGORIES


def _restore(snapshot: dict[str, Any], args: argparse.Namespace, *, startup: bool = False) -> dict[str, Any]:
    targets = _targets(args.category)
    if args.select and "terminals" not in targets:
        raise ValueError("--select only applies to terminals")
    if args.session and "terminals" not in targets:
        raise ValueError("--session only applies to terminals")
    counts = {
        **{category: 0 for category in CATEGORIES},
        "virtual_machines_total": 0,
        "virtual_machines_message": "",
        "codex_ready": 0,
        "codex_total": 0,
        "codex_verified": 1,
        "codex_deferred": 0,
    }
    if len(targets) > 1 and not args.session and not args.select:
        jobs = {}
        for category in targets:
            category_args = argparse.Namespace(**vars(args))
            category_args.category = category
            jobs[category] = partial(_restore, snapshot, category_args, startup=startup)
        errors = []
        for category, result, error in completed_jobs(jobs, serial=args.dry_run):
            if error is not None:
                errors.append(f"{category}: {error}")
                continue
            counts[category] = result[category]
            if category == "terminals":
                counts.update({key: result[key] for key in ("codex_ready", "codex_total", "codex_verified")})
                counts["codex_deferred"] = result.get("codex_deferred", 0)
            elif category == "virtual-machines":
                counts.update({key: result[key] for key in ("virtual_machines_total", "virtual_machines_message")})
        if errors:
            raise RestoreJobsFailed("; ".join(errors))
        return counts
    if "terminals" in targets:
        outcome = _restore_terminals(snapshot, args)
        counts["terminals"] = outcome.restored
        counts["codex_ready"] = outcome.codex_ready
        counts["codex_total"] = outcome.codex_total
        counts["codex_verified"] = int(outcome.codex_verified)
        counts["codex_deferred"] = int(outcome.codex_deferred)
    if "browsers" in targets and not args.session and not args.select:
        counts["browsers"] = _restore_browsers(snapshot, args, start_browser=startup)
    if "social-apps" in targets and not args.session and not args.select:
        def social_report(state: str, message: str, current: int, total: int) -> None:
            if getattr(args, "login_status", False):
                update_stage("social-apps", state, message, current=current, total=total)
        counts["social-apps"] = restore_social_apps(
            snapshot.get("social_apps"), dry_run=args.dry_run,
            no_place=args.no_place, workspace=args.workspace, reporter=social_report,
        )
    if "file-manager" in targets and not args.session and not args.select:
        def file_manager_report(state: str, message: str, current: int, total: int) -> None:
            if getattr(args, "login_status", False):
                update_stage("file-manager", state, message, current=current, total=total)
        counts["file-manager"] = restore_file_manager(
            snapshot.get("file_manager"), dry_run=args.dry_run,
            no_place=args.no_place, workspace=args.workspace,
            reporter=file_manager_report, timeout=30,
        )
    if "vscode" in targets and not args.session and not args.select:
        def vscode_report(state: str, message: str, current: int, total: int) -> None:
            if getattr(args, "login_status", False):
                update_stage("vscode", state, message, current=current, total=total)
        counts["vscode"] = restore_vscode(
            snapshot.get("vscode"), dry_run=args.dry_run,
            no_place=args.no_place, workspace=args.workspace,
            reporter=vscode_report, timeout=30,
        )
    if "virtual-machines" in targets and not args.session and not args.select:
        if getattr(args, "login_status", False):
            update_stage(
                "virtual-machines",
                "running",
                "Checking the committed Windows VM restore transaction",
            )
        outcome = restore_startup_profiles(dry_run=args.dry_run)
        counts["virtual-machines"] = outcome.restored
        counts["virtual_machines_total"] = outcome.total
        counts["virtual_machines_message"] = outcome.message
        print(outcome.message)
    return counts


def cmd_restore(args: argparse.Namespace) -> int:
    counts = _restore(load(), args)
    if args.category in {"social-apps", "file-manager", "vscode"}:
        return 0  # A deliberate background/stopped skip is successful reconciliation.
    if not any(counts[category] for category in CATEGORIES):
        print("No matching windows or sessions selected.", file=sys.stderr)
        return 1
    return 0


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "current-boot"


def _login_generation_file() -> str | None:
    try:
        generation = (runtime_dir() / "login-generation").read_text().strip()
    except OSError:
        return None
    if not generation or any(character not in "0123456789abcdef" for character in generation):
        return None
    return generation


def _startup_directory() -> Path:
    from .startup import startup_directory
    return startup_directory(runtime_dir(), _boot_id(), _login_generation_file())


def _startup_marker(category: str) -> Path:
    return _startup_directory() / f"{category}.done"




def _marker_context():
    """Read current authority without adopting a different worker's operation."""
    from . import operations
    try:
        document = json.loads(status_path().read_text())
        context = operations.OperationContext.from_dict(document.get("operation_context"))
        inherited = operations.current()
        if (not context.matches(document) or context.boot_id != operations.boot_id()
                or (inherited is not None and inherited != context)):
            return None
        return context
    except (OSError, TypeError, ValueError):
        return None

def _write_attempt_marker(path: Path, category: str, snapshot: dict[str, Any], state: str,
                          results=(), message: str = "") -> None:
    from . import operations
    context = operations.current()
    write_stage_marker(path, StageMarker(category, state, str(snapshot.get("created_at", "")),
                                        message, context.to_dict() if context else None,
                                        tuple(result.to_dict() for result in results)))


def _publish_category_outcome(category: str, snapshot: dict[str, Any], *,
                              count=0, error: BaseException | None = None) -> str:
    results = tuple(getattr(error if error is not None else count, "results", ()))
    waiting = waiting_only(results)
    state = "waiting" if waiting else "failed" if error is not None else "ready" if count else "skipped"
    if results and not waiting and not all(result.success for result in results):
        state = "failed"
    message = (str(error) if error is not None else
               "Waiting for compositor placement verification" if waiting else
               f"Verified {int(count)} {category} item(s)" if count else f"No saved {category} items")
    update_stage(category, state, message, error=message if state == "failed" else None)
    # Publish the attempt marker before exposing asynchronous work to the
    # observer, so a fast verified response cannot be overwritten by waiting.
    _write_attempt_marker(_startup_marker(category), category, snapshot, state, results, message)
    publish_provider_results(category, results,
                             error=message if error is not None and not waiting else None)
    if state in {"waiting", "failed"}:
        _autosave_marker().unlink(missing_ok=True)
    return state


def _publish_workspace_attempt_completion() -> None:
    """Start mounts after attempts, but leave aggregate verification pending."""
    from . import login_status
    def mutate(status):
        stage = next((item for item in status.get("stages", []) if item.get("id") == "workspace"), None)
        if stage is None:
            return
        states = {item.get("id"): item.get("state") for item in status.get("stages", [])}
        unfinished = any(states.get(category) not in {"ready", "skipped"} for category in CATEGORIES)
        if unfinished:
            stage["provider_completion_pending"] = True
            category_states = [states.get(category) for category in CATEGORIES]
            state = ("failed" if "failed" in category_states else
                     "degraded" if all(value in login_status.TERMINAL_STATES for value in category_states)
                     else "waiting")
            stage.update(state=state, message="Application restoration needs attention" if state != "waiting"
                         else "Waiting for application restore verification")
        else:
            stage.update(state="ready", message="Application workspace restoration verified", current=1, total=1)
        login_status._refresh_provider_placements(status)
    login_status._locked_update(mutate, mode="startup", allow_expired=True)

def _autosave_marker() -> Path:
    return _startup_directory() / "autosave.ready"


def _tmux_restore_marker() -> Path:
    return _startup_directory() / "tmux-restore.running"


def _tmux_restore_done_marker() -> Path:
    return _startup_directory() / "tmux-restore.done"


def _process_start_time(pid: int) -> str | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2:].split()
        return fields[19]
    except (OSError, IndexError, ValueError):
        return None


def _tmux_restore_running() -> bool:
    marker = _tmux_restore_marker()
    try:
        fields = marker.read_text().split()
        pid = int(fields[0])
    except (OSError, ValueError, IndexError):
        return False
    current_start = _process_start_time(pid)
    if current_start is not None and len(fields) > 1 and fields[1] == current_start:
        return True
    marker.unlink(missing_ok=True)
    return False


def _arm_autosave() -> None:
    marker = _autosave_marker()
    marker.write_text("ready\n")
    marker.chmod(0o600)


def _arm_autosave_if_startup_complete() -> None:
    context = _marker_context()
    try:
        if context is None or context.mode != "startup":
            raise ValueError("startup context is unavailable")
        context.check()
        if not all((marker := read_stage_marker(_startup_marker(category), category)) is not None
                   and marker.verified_for(context) for category in CATEGORIES):
            raise ValueError("category verification belongs to another attempt")
        status = json.loads(status_path().read_text())
        if not context.matches(status):
            raise ValueError("operation changed")
        codex = next((stage for stage in status.get("stages", [])
                      if isinstance(stage, dict) and stage.get("id") == "codex"), {})
        if codex.get("state") not in {"ready", "skipped"}:
            raise ValueError("Codex conversation identities are unverified")
    except (OSError, TypeError, ValueError, TimeoutError):
        _autosave_marker().unlink(missing_ok=True)
        return
    _arm_autosave()


def _publish_workspace_restored() -> None:
    # Storage-backed folders run after this target starts mounts. Requiring
    # their .done marker here would create a dependency cycle.
    if not all(
        _startup_marker(category).exists()
        or (category in {"file-manager", "vscode"} and (_startup_directory() / f"{category}.deferred").exists())
        for category in CATEGORIES
    ):
        return
    result = subprocess.run(
        [
            "/usr/bin/systemctl", "--user", "start", "--no-block",
            WORKSPACE_RESTORED_TARGET,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit status {result.returncode}"
        raise RuntimeError(f"could not publish completed workspace restore: {detail}")


@contextmanager
def _startup_lock() -> Iterator[None]:
    path = _startup_directory() / "restore.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def finish_deferred_codex() -> bool:
    """Recheck saved conversations after mounts without launching any process."""
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        status = {}
    stage = next((item for item in status.get("stages", [])
                  if isinstance(item, dict) and item.get("id") == "codex"), {}) if isinstance(status, dict) else {}
    if stage.get("state") == "failed":
        return False
    snapshot = load()
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])}
    expected = {
        str(pane["codex"]["session_id"])
        for session in sessions.values()
        for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if (pane.get("codex") or {}).get("session_id")
    }
    deadline = time.monotonic() + CODEX_FINALIZE_TIMEOUT_SECONDS
    may_wait = bool(expected & pending_start_ids())
    stable_since: float | None = None
    first_check = True
    while True:
        missing = {
            session_id
            for name, session in sessions.items()
            for session_id in missing_codex_ids(session, name)
        } & expected
        now = time.monotonic()
        if first_check and not missing:
            may_wait = True
        first_check = False
        if missing:
            stable_since = None
        elif stable_since is None:
            stable_since = now
        stable = not expected or (
            not missing and stable_since is not None
            and now - stable_since >= CODEX_STABLE_SECONDS
        )
        if stable or not may_wait or now >= deadline:
            break
        time.sleep(0.5)
    ready = len(expected) - len(missing)
    update_stage(
        "codex", "degraded" if not stable else "ready" if expected else "skipped",
        (
            f"{ready}/{len(expected)} Codex conversation(s) verified after cloud drives; "
            "check the remaining tmux panes for startup errors or prompts"
            if missing else
            "Codex stability verification timed out; check the restored tmux panes"
            if not stable else
            f"Resumed {len(expected)} Codex conversation(s)" if expected else
            "No saved Codex conversations"
        ),
        current=ready, total=len(expected),
    )
    if stable:
        _arm_autosave_if_startup_complete()
    return stable


def finish_deferred_file_manager() -> bool:
    """Restore a storage-backed file-manager recipe after cloud mounts start."""
    with _startup_lock():
        marker = _startup_marker("file-manager")
        deferred = _startup_directory() / "file-manager.deferred"
        if marker.exists():
            previous = read_stage_marker(marker)
            return previous is not None and previous.state != "failed"
        if not deferred.exists():
            return True
        # A corrupt/missing checkpoint must stay visible; finalization catches
        # this failure and still warms drives. Do not silently mark it ready.
        snapshot = load()
        update_stage("file-manager", "running", "Restoring file manager windows after cloud drives mounted")
        args = argparse.Namespace(
            category="file-manager", workspace=None, session=None, select=False,
            dry_run=False, no_place=False, login_status=True, wait=30,
            force=False, repair_processes=False, adopt_restored=False,
            verify_codex=False,
        )
        try:
            counts = _restore(snapshot, args, startup=True)
        except (BrowserUnavailable, FileNotFoundError, ValueError, RuntimeError) as error:
            message = str(error)
            state = _publish_category_outcome("file-manager", snapshot, error=error)
            deferred.unlink(missing_ok=True)
            _arm_autosave_if_startup_complete()
            return state != "failed"
        deferred.unlink(missing_ok=True)
        outcome = counts.get("file-manager", 0)
        state = _publish_category_outcome("file-manager", snapshot, count=outcome)
        if state in {"waiting", "failed"}:
            return state != "failed"
        count = int(outcome)
        update_stage(
            "file-manager", "ready" if count else "skipped",
            f"Restored {count} file manager window(s)" if count else "No saved file manager windows",
            current=count, total=count,
        )
        _arm_autosave_if_startup_complete()
        return True


def finish_deferred_vscode() -> bool:
    """Restore VS Code workspaces deferred until storage mounts are ready."""
    with _startup_lock():
        marker = _startup_marker("vscode")
        deferred = _startup_directory() / "vscode.deferred"
        if marker.exists():
            previous = read_stage_marker(marker)
            return previous is not None and previous.state != "failed"
        if not deferred.exists():
            return True
        snapshot = load()
        update_stage("vscode", "running", "Restoring VS Code workspaces after cloud drives mounted")
        args = argparse.Namespace(
            category="vscode", workspace=None, session=None, select=False,
            dry_run=False, no_place=False, login_status=True, wait=30,
            force=False, repair_processes=False, adopt_restored=False,
            verify_codex=False,
        )
        try:
            counts = _restore(snapshot, args, startup=True)
        except (BrowserUnavailable, FileNotFoundError, ValueError, RuntimeError) as error:
            message = str(error)
            state = _publish_category_outcome("vscode", snapshot, error=error)
            deferred.unlink(missing_ok=True)
            _arm_autosave_if_startup_complete()
            return state != "failed"
        deferred.unlink(missing_ok=True)
        outcome = counts.get("vscode", 0)
        state = _publish_category_outcome("vscode", snapshot, count=outcome)
        if state in {"waiting", "failed"}:
            return state != "failed"
        count = int(outcome)
        update_stage("vscode", "ready" if count else "skipped",
                     f"Restored {count} VS Code workspace(s)" if count else "No saved VS Code workspaces",
                     current=count, total=count)
        _arm_autosave_if_startup_complete()
        return True


def _wait_for_shell(
    timeout: float,
    capabilities: set[str] | None = None,
    workspace_names: set[str] | None = None,
    *,
    stable_for: float = 2.0,
) -> None:
    required = capabilities or set()
    expected_names = workspace_names or set()
    deadline = time.monotonic() + max(0.0, timeout)
    stable_since: float | None = None
    previous_signature: str | None = None
    reason = "GNOME displays and workspaces"
    while True:
        shell = capture_shell()
        ready, reason = desktop_readiness(shell, required)
        current_names = {
            str(item.get("name")) for item in shell.get("workspaces", [])
            if isinstance(item, dict) and item.get("name") is not None
        }
        missing_names = sorted(expected_names - current_names)
        if ready and missing_names:
            ready = False
            reason = "saved GNOME workspaces: " + ", ".join(missing_names)
        now = time.monotonic()
        if ready:
            signature = desktop_topology_signature(shell)
            if signature != previous_signature:
                previous_signature = signature
                stable_since = now
            elif stable_since is not None and now - stable_since >= stable_for:
                return
        else:
            stable_since = None
            previous_signature = None
        if now >= deadline:
            raise RuntimeError(
                "the desktop did not become ready for startup restore; waiting for " + reason,
            )
        time.sleep(0.1)


def _startup_shell_capabilities(
    snapshot: dict[str, Any],
    pending: list[str],
    args: argparse.Namespace,
) -> set[str]:
    required = set(DESKTOP_REQUIRED_CAPABILITIES)
    if args.no_place:
        return required
    if "terminals" in pending and any(
        item.get("launch_terminal", True) and item.get("placement")
        for item in _filtered_terminal_items(snapshot, args)
    ):
        required.update({"list_windows", "place_window"})
    if "browsers" in pending and _selected_browser_windows(snapshot, args.workspace):
        required.update({
            "list_windows", "place_window", "window_state",
            "expect_window", "expectation_status", "cancel_expectation",
            "monitor_intent", "monitor_recovery",
        })
    if "vscode" in pending and (snapshot.get("vscode") or {}).get("windows") and not args.no_place:
        required.update({"list_windows", "place_window"})
    return required


def _startup_workspace_names(
    snapshot: dict[str, Any],
    pending: list[str],
    args: argparse.Namespace,
) -> set[str]:
    names: set[str] = set()
    if "terminals" in pending:
        for item in _filtered_terminal_items(snapshot, args):
            placement = item.get("placement") or {}
            if placement.get("workspace_name"):
                names.add(str(placement["workspace_name"]))
    if "browsers" in pending:
        for _profile, window in _selected_browser_windows(snapshot, args.workspace):
            if window.get("workspace"):
                names.add(str(window["workspace"]))
    # Social, file-manager, and VS Code destinations are checked per category so an unavailable destination
    # fails only that category, not the remaining workspace/drive handoff.
    return names


def _wait_for_tmux_restore(timeout: float, *, await_start: bool = False) -> bool:
    deadline = time.monotonic() + max(0, timeout)
    start_deadline = min(
        deadline,
        time.monotonic() + TMUX_RESTORE_START_WAIT_SECONDS,
    )
    marker = _tmux_restore_marker()
    done = _tmux_restore_done_marker()
    saw_restore = _tmux_restore_running()
    while time.monotonic() < deadline:
        if done.exists():
            return True
        if _tmux_restore_running():
            saw_restore = True
        elif saw_restore:
            return False
        elif not await_start:
            return True
        elif time.monotonic() >= start_deadline:
            return False
        time.sleep(0.1)
    if _tmux_restore_running():
        raise RuntimeError("tmux-resurrect is still running; startup restore was deferred")
    # Continuum deliberately skips restore when another server exists or the
    # boot restore is disabled. After its startup window has elapsed, wsctl is
    # the safe fallback and reconstructs from the canonical recipe itself.
    if await_start and not done.exists():
        return False
    return True


def _saved_tmux_sessions_are_live(snapshot: dict[str, Any]) -> bool:
    """Recognize a tmux layout that was already restored before GNOME login."""
    expected = {
        str(session.get("name"))
        for session in snapshot.get("sessions", [])
        if isinstance(session, dict) and session.get("name")
    }
    if not expected:
        return False
    result = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return False
    live = {name for name in result.stdout.splitlines() if name}
    return expected <= live


def cmd_startup(args: argparse.Namespace) -> int:
    if args.dry_run:
        dry_counts = _restore(load(), args, startup=True)
        return int(not any(dry_counts[category] for category in CATEGORIES))
    if args.force:
        context = _marker_context()
        try:
            document = json.loads(status_path().read_text())
            if context is None or context.mode != "startup" or document.get("operation_state") != "running":
                raise ValueError("startup operation is not running")
            context.check()
        except (OSError, TypeError, ValueError, TimeoutError) as error:
            raise RuntimeError("Cannot force a completed, expired, or unowned startup; start a new login/operation before retrying") from error
    from .startup import startup_suspended
    if startup_suspended(runtime_dir(), _boot_id(), _login_generation_file()):
        raise RuntimeError("startup is suspended by this login's shutdown/recovery operation")
    set_overall("running", "Restoring saved workspace state")
    if not getattr(args, "owns_tmux_restore", False):
        update_stage("tmux", "running", "Waiting for tmux-resurrect")
        tmux_already_live = (
            getattr(args, "await_tmux", False)
            and _saved_tmux_sessions_are_live(load())
        )
        if tmux_already_live:
            restored_by_continuum = True
            # The original tmux-resurrect names are present, so automatic
            # window-name changes (for example `ssh` -> `zsh`) are not a
            # reason to clone those sessions during the fallback worker.
            args = argparse.Namespace(**vars(args))
            args.adopt_restored = True
        else:
            try:
                restored_by_continuum = _wait_for_tmux_restore(
                    args.wait, await_start=getattr(args, "await_tmux", False),
                )
            except RuntimeError as error:
                update_stage("tmux", "failed", str(error), error=str(error))
                raise
        if getattr(args, "await_tmux", False) and not restored_by_continuum:
            # The first Alacritty creates a single-shell `main` session while
            # Continuum gets its chance to run. Only this bounded fallback may
            # adopt and repair that provably pristine bootstrap session.
            args = argparse.Namespace(**vars(args))
            args.repair_processes = True
        update_stage(
            "tmux", "ready",
            "Saved tmux sessions are already running"
            if tmux_already_live else
            "tmux-resurrect completed"
            if restored_by_continuum else
            "Using canonical tmux fallback",
            current=1, total=1,
        )
    else:
        update_stage("tmux", "ready", "tmux-resurrect restored the saved layout", current=1, total=1)
    with _startup_lock():
        snapshot = load()
        targets = _targets(args.category)
        pending = [
            category for category in targets
            if args.force or not _startup_marker(category).exists()
        ]
        deferred_file_manager = (
            args.category is None
            and "file-manager" in pending
            and needs_storage(snapshot.get("file_manager"))
        )
        deferred_vscode = (
            args.category is None
            and "vscode" in pending
            and needs_vscode_storage(snapshot.get("vscode"))
        )
        if deferred_file_manager:
            pending.remove("file-manager")
            deferred = _startup_directory() / "file-manager.deferred"
            deferred.write_text(f"{snapshot.get('created_at', '')}\n")
            deferred.chmod(0o600)
            update_stage("file-manager", "waiting", "Waiting for cloud drives before restoring file manager windows")
        if deferred_vscode:
            pending.remove("vscode")
            deferred = _startup_directory() / "vscode.deferred"
            deferred.write_text(f"{snapshot.get('created_at', '')}\n")
            deferred.chmod(0o600)
            update_stage("vscode", "waiting", "Waiting for cloud drives before restoring VS Code workspaces")
        completed_targets = set(targets) - set(pending)
        completed_errors = []
        if deferred_file_manager:
            completed_targets.discard("file-manager")
        if deferred_vscode:
            completed_targets.discard("vscode")
        try:
            with status_path().open(encoding="utf-8") as stream:
                previous_status = json.load(stream)
        except (OSError, ValueError):
            previous_status = {}
        previous_stages = {
            stage.get("id"): stage for stage in previous_status.get("stages", [])
            if isinstance(stage, dict)
        } if isinstance(previous_status, dict) and previous_status.get("mode", "startup") == "startup" else {}
        for completed in completed_targets:
            previous_marker = read_stage_marker(_startup_marker(completed), completed)
            previous_result = previous_marker.message if previous_marker else "Restore marker is unavailable"
            if previous_marker is not None and previous_marker.state == "failed":
                completed_errors.append(f"{completed}: {previous_result}")
                update_stage(completed, "failed", previous_result, error=previous_result)
                if completed == "terminals":
                    update_stage("codex", "failed", "Terminal restoration did not complete", error=previous_result)
                continue
            previous_stage = previous_stages.get(completed, {})
            if previous_marker is not None and previous_marker.state == "waiting":
                if previous_stage.get("state") != "failed":
                    update_stage(completed, "waiting", "Prior restore is awaiting placement verification")
                continue
            if previous_marker is not None and previous_marker.verified_for(_marker_context()) and previous_stage.get("state") not in {"ready", "skipped", "degraded", "failed"}:
                update_stage(completed, previous_marker.state, "Prior restore evidence is verified")
                continue
            if previous_stage.get("state") == "failed":
                completed_errors.append(
                    f"{completed}: {previous_stage.get('error') or previous_stage.get('message') or 'prior restoration failed'}"
                )
            elif previous_stage.get("state") not in {"ready", "skipped", "degraded"}:
                update_stage(
                    completed, "degraded",
                    "Restore was already attempted this login; prior completion details are unavailable",
                )
            # A completion marker prevents relaunch; it does not establish new
            # item counts or supersede the prior terminal result. In particular,
            # the terminal-layout marker says nothing about Codex readiness.
        if not pending and not deferred_file_manager and not deferred_vscode:
            _publish_workspace_attempt_completion()
            _publish_workspace_restored()
            _arm_autosave_if_startup_complete()
            print("Startup state is already restored for this login.")
            if completed_errors:
                raise RestoreJobsFailed("; ".join(completed_errors))
            return 0
        update_stage("workspace", "running", "Validating GNOME placement capabilities")
        required_shell = _startup_shell_capabilities(snapshot, pending, args)
        try:
            _wait_for_shell(
                args.wait,
                required_shell,
                _startup_workspace_names(snapshot, pending, args),
            )
        except RuntimeError as error:
            update_stage("workspace", "failed", str(error), error=str(error))
            raise
        startup_errors: list[str] = list(completed_errors)
        jobs = {}
        for category in pending:
            category_args = argparse.Namespace(**vars(args))
            category_args.category = category
            category_args.login_status = True
            jobs[category] = partial(_restore, snapshot, category_args, startup=True)
        for category, counts, error in completed_jobs(jobs):
            if error is not None:
                state = _publish_category_outcome(category, snapshot, error=error)
                if state == "failed":
                    if category == "terminals":
                        update_stage("codex", "failed", "Terminal restoration did not complete", error=str(error))
                    startup_errors.append(f"{category}: {error}")
                # Both waiting and failed attempts prevent automatic relaunch.
                continue
            state = _publish_category_outcome(category, snapshot, count=counts[category])
            if state in {"waiting", "failed"}:
                if state == "failed":
                    startup_errors.append(f"{category}: provider evidence is incomplete")
                continue
            if counts[category]:
                print(f"Startup restored {counts[category]} {category} item(s).")
            else:
                print(f"Startup has no saved {category} items.")
            if category == "terminals":
                state = "ready" if counts[category] else "skipped"
                terminal_items = _filtered_terminal_items(snapshot, args)
                alacritty_total = sum(
                    bool(item.get("launch_terminal", True))
                    for item in terminal_items
                )
                tmux_total = len({str(item.get("name")) for item in terminal_items})
                update_stage(
                    "terminals", state,
                    (
                        f"Restored {alacritty_total} Alacritty window(s) and "
                        f"verified {tmux_total} tmux session(s)"
                    ) if counts[category] else "No saved terminal sessions",
                    current=counts[category], total=counts[category],
                )
                codex_total = counts.get("codex_total", 0)
                codex_ready = counts.get("codex_ready", codex_total)
                codex_verified = bool(counts.get("codex_verified", 1))
                codex_state = (
                    "skipped" if not codex_total else
                    "ready" if codex_verified else
                    "waiting" if counts.get("codex_deferred", 0) else
                    "degraded"
                )
                update_stage(
                    "codex", codex_state,
                    (
                        f"Resumed {codex_total} Codex conversation(s)"
                        if codex_state == "ready" else
                        (
                            f"{codex_ready}/{codex_total} Codex conversation(s) verified; "
                            "waiting for saved directories before final verification"
                        )
                        if codex_state == "waiting" else
                        (
                            f"{codex_ready}/{codex_total} Codex conversation(s) verified; "
                            "check the remaining tmux panes for startup errors or prompts"
                        )
                        if codex_state == "degraded" else
                        "No saved Codex conversations"
                    ),
                    current=codex_ready, total=codex_total,
                )
            elif category == "browsers":
                update_stage(
                    "browsers", "ready" if counts[category] else "skipped",
                    f"Restored {counts[category]} Chrome window(s)" if counts[category] else "No saved Chrome windows",
                    current=counts[category], total=counts[category],
                )
            elif category == "social-apps":
                pass  # Per-app messages and the final result were published by its reporter.
            elif category == "file-manager":
                update_stage(
                    "file-manager", "ready" if counts[category] else "skipped",
                    f"Restored {counts[category]} file manager window(s)" if counts[category] else "No saved file manager windows",
                    current=counts[category], total=counts[category],
                )
            elif category == "vscode":
                update_stage(
                    "vscode", "ready" if counts[category] else "skipped",
                    f"Restored {counts[category]} VS Code workspace(s)" if counts[category] else "No saved VS Code workspaces",
                    current=counts[category], total=counts[category],
                )
            else:
                total = int(counts.get("virtual_machines_total", 0))
                update_stage(
                    "virtual-machines",
                    "ready" if total else "skipped",
                    str(counts.get("virtual_machines_message") or "Windows VM restore completed"),
                    current=counts[category], total=total,
                )
        _publish_workspace_attempt_completion()
        set_overall("running", "Workspace restored; loading cloud systems")
        try:
            _publish_workspace_restored()
        except RuntimeError as error:
            update_stage("workspace", "failed", str(error), error=str(error))
            raise
        _arm_autosave_if_startup_complete()
        if startup_errors:
            raise RestoreJobsFailed("; ".join(startup_errors))
    return 0


def _shutdown_allows_unresolved_codex() -> bool:
    operation_id = os.environ.get("WSCTL_SHUTDOWN_OPERATION_ID", "")
    if (
        len(operation_id) != 32
        or any(character not in "0123456789abcdef" for character in operation_id)
    ):
        return False
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        isinstance(status, dict)
        and status.get("schema_version") == 1
        and status.get("mode") == "shutdown"
        and status.get("operation_id") == operation_id
        and status.get("shutdown_origin") == "preflight"
        and status.get("cancelled") is not True
        and status.get("overall_state") != "failed"
    )


def _autosave_from_tmux(*, allow_unresolved_codex: bool = False) -> tuple[Path | None, list[str]]:
    with state_lock():
        try:
            previous = load()
        except FileNotFoundError:
            previous = {}
        snapshot = capture()
        problems = _terminal_problems(snapshot)
        monitor_problem = _fallback_monitor_problem(snapshot, previous)
        if monitor_problem:
            problems.append(monitor_problem)
        if previous.get("sessions") and not snapshot.get("sessions"):
            problems.append("tmux capture unexpectedly contains no sessions")
        unsafe = [
            problem for problem in problems
            if not problem.endswith("Codex session ID(s) are unresolved")
        ]
        if problems and (not allow_unresolved_codex or unsafe):
            return None, problems

        prior_chrome = _browser_state(previous)
        candidate = dict(snapshot)
        # Continuum is a terminal autosave. Browser state is checkpointed by
        # explicit/full saves (including GNOME end-session), not by a periodic
        # hook which may run while Chrome's own startup restoration is partial.
        # Keeping the prior category also prevents temporary or diagnostic
        # Chrome windows from replacing the durable browser recipe.
        if prior_chrome:
            _set_browser_state(candidate, prior_chrome)
        # Terminal-only autosave must never erase the social visibility recipe.
        if "social_apps" in previous:
            candidate["social_apps"] = previous["social_apps"]
        if "file_manager" in previous:
            candidate["file_manager"] = previous["file_manager"]
        if "vscode" in previous:
            candidate["vscode"] = previous["vscode"]
        return save(candidate), problems


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
    try:
        allow_unresolved = _shutdown_allows_unresolved_codex()
        path, problems = _autosave_from_tmux(
            allow_unresolved_codex=allow_unresolved,
        )
    except Exception as error:
        try:
            protected = preserve_last_state(state_file)
        except (OSError, RuntimeError, ValueError) as preserve_error:
            raise RuntimeError(
                f"workspace autosave failed ({error}); previous tmux state could not be "
                f"preserved ({preserve_error})"
            ) from preserve_error
        print(
            f"wsctl tmux hook: workspace autosave failed; preserved {protected}: {error}",
            file=sys.stderr,
        )
        return 0
    unresolved_only = problems and all(
        problem.endswith("Codex session ID(s) are unresolved")
        for problem in problems
    )
    if problems and not (allow_unresolved and unresolved_only and path is not None):
        try:
            protected = preserve_last_state(state_file)
        except (OSError, RuntimeError, ValueError) as error:
            raise RuntimeError(
                "workspace autosave was rejected and the previous tmux state "
                f"could not be preserved: {error}"
            ) from error
        print(
            "wsctl tmux hook: workspace state not updated; preserved "
            f"{protected}: " + "; ".join(problems),
            file=sys.stderr,
        )
        return 0
    if problems:
        print(
            "wsctl tmux hook: saved a degraded shutdown checkpoint: "
            + "; ".join(problems),
            file=sys.stderr,
        )
    print(
        f"wsctl tmux hook: {result['annotated']} Codex pane(s) contracted, "
        f"{result['unresolved']} unresolved"
        + (f"; saved {path}" if path else "")
    )
    return 0


def cmd_tmux_begin(args: argparse.Namespace) -> int:
    owner_pid = int(args.owner_pid or os.getpid())
    start_time = _process_start_time(owner_pid)
    if start_time is None:
        raise RuntimeError(f"tmux restore owner process {owner_pid} is not running")
    _tmux_restore_done_marker().unlink(missing_ok=True)
    marker = _tmux_restore_marker()
    marker.write_text(f"{owner_pid} {start_time}\n")
    marker.chmod(0o600)
    return 0


def cmd_tmux_restore(args: argparse.Namespace) -> int:
    startup_args = argparse.Namespace(
        category=None, workspace=None, session=None, select=False,
        dry_run=False, no_place=False, force=False, wait=args.wait,
        repair_processes=False, adopt_restored=True, await_tmux=False,
        verify_codex=True, owns_tmux_restore=True,
    )
    result = cmd_startup(startup_args)
    done = _tmux_restore_done_marker()
    done.write_text("done\n")
    done.chmod(0o600)
    _tmux_restore_marker().unlink(missing_ok=True)
    return result


def cmd_tmux_end(_args: argparse.Namespace) -> int:
    # The wrapper always calls this cleanup, including when resurrect or its
    # post-hook failed. Only cmd_tmux_restore may publish the success marker.
    _tmux_restore_marker().unlink(missing_ok=True)
    return 0


def _tmux_config_path(config: str | None = None) -> Path:
    if config:
        path = Path(config).expanduser()
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        candidate = config_home / "tmux/tmux.conf"
        path = candidate if candidate.exists() or candidate.is_symlink() else Path.home() / ".tmux.conf"
    return path.resolve() if path.is_symlink() else path


def _configure_tmux_file(path: Path) -> bool:
    directive = f"set -g @resurrect-processes '{TMUX_CODEX_PROCESS_MAPPING}'"
    try:
        original = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        original = ""
        mode = 0o600
    lines = original.splitlines()
    matches = [
        index for index, line in enumerate(lines)
        if (
            not line.lstrip().startswith("#")
            and "@resurrect-processes" in line
            and "wsctl-codex" in line
        )
    ]
    if matches:
        first = matches[0]
        lines[first] = directive
        for index in reversed(matches[1:]):
            del lines[index]
    else:
        conflicting = [
            line for line in lines
            if (
                not line.lstrip().startswith("#")
                and "@resurrect-processes" in line
            )
        ]
        if conflicting:
            raise RuntimeError(
                "tmux already has a non-wsctl @resurrect-processes directive; "
                "refusing to overwrite it"
            )
        if lines and lines[-1]:
            lines.append("")
        lines.extend([
            "# Keep restored Codex panes alive as shells when a resume exits.",
            directive,
        ])
    updated = "\n".join(lines) + "\n"
    if updated == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.wsctl-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _unconfigure_tmux_file(path: Path) -> bool:
    directive = f"set -g @resurrect-processes '{TMUX_CODEX_PROCESS_MAPPING}'"
    try:
        original = path.read_text(encoding="utf-8")
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return False
    lines = original.splitlines()
    managed = [
        index for index, line in enumerate(lines)
        if line.strip() == directive
    ]
    if not managed:
        return False
    for index in reversed(managed):
        del lines[index]
        if (
            index > 0
            and lines[index - 1]
            == "# Keep restored Codex panes alive as shells when a resume exits."
        ):
            del lines[index - 1]
    while len(lines) >= 2 and not lines[-1] and not lines[-2]:
        lines.pop()
    updated = "\n".join(lines) + ("\n" if lines else "")
    temporary = path.with_name(f".{path.name}.wsctl-{os.getpid()}")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def cmd_tmux_configure(args: argparse.Namespace) -> int:
    path = _tmux_config_path(args.config)
    changed = _configure_tmux_file(path)
    subprocess.run(
        [
            "tmux", "set-option", "-g", "@resurrect-processes",
            TMUX_CODEX_PROCESS_MAPPING,
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print(f"tmux Codex restore mapping {'updated' if changed else 'already current'}: {path}")
    return 0


def cmd_tmux_unconfigure(args: argparse.Namespace) -> int:
    path = _tmux_config_path(args.config)
    changed = _unconfigure_tmux_file(path)
    current = subprocess.run(
        ["tmux", "show-option", "-gv", "@resurrect-processes"],
        check=False,
        capture_output=True,
        text=True,
    )
    if current.returncode == 0 and current.stdout.strip() == TMUX_CODEX_PROCESS_MAPPING:
        subprocess.run(
            ["tmux", "set-option", "-gu", "@resurrect-processes"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(f"tmux Codex restore mapping {'removed' if changed else 'not present'}: {path}")
    return 0


def cmd_tmux_contract(args: argparse.Namespace) -> int:
    result = annotate_state_file(Path(args.state_file), load(), recipe_only=True)
    print(
        f"wsctl tmux hook: {result['annotated']} Codex pane(s) contracted, "
        f"{result['unresolved']} unresolved",
    )
    return 0


def cmd_shutdown_profiles_list(args: argparse.Namespace) -> int:
    records: list[dict[str, Any]] = []
    for profile in load_profiles():
        record: dict[str, Any] = {
            "id": profile.identifier,
            "label": profile.label,
            "adapter": profile.adapter,
            "parallel": profile.parallel,
            "critical": profile.critical,
            "actions": sorted(profile.actions),
            "source": str(profile.source) if profile.source else None,
            "fingerprint": profile_fingerprint(profile),
            "enabled_for_action": args.action in profile.actions,
        }
        if args.probe and record["enabled_for_action"]:
            try:
                applicable, message = probe_profile(profile)
                record.update({"probe_ok": True, "applicable": applicable, "message": message})
            except RuntimeError as error:
                record.update({"probe_ok": False, "applicable": None, "message": str(error)})
        records.append(record)
    if args.json:
        print(json.dumps(records, indent=2, ensure_ascii=False))
        return 2 if any(record.get("probe_ok") is False for record in records) else 0
    if not records:
        print("No shutdown profiles are installed.")
        return 0
    for record in records:
        status = "enabled" if record["enabled_for_action"] else "disabled for action"
        if args.probe and record["enabled_for_action"]:
            status = (
                "active" if record.get("probe_ok") and record.get("applicable")
                else "inactive" if record.get("probe_ok")
                else "probe failed"
            )
        importance = "critical" if record["critical"] else "best-effort"
        print(
            f"{record['id']}: {record['label']} [{record['adapter']}, "
            f"{importance}, {status}]"
        )
        if args.probe and record.get("message"):
            print(f"  {record['message']}")
    return 2 if any(record.get("probe_ok") is False for record in records) else 0


def cmd_shutdown_profiles_install_qemu_windows(args: argparse.Namespace) -> int:
    path = install_qemu_windows_profile(
        Path(args.vm_directory),
        identifier=args.id,
        label=args.label,
        timeout_seconds=args.timeout,
        force=args.force,
    )
    print(f"Installed shutdown profile: {path}")
    return 0


def cmd_shutdown_profiles_test(args: argparse.Namespace) -> int:
    from .profile_test import run_profile_test
    from .shutdown_profiles import ShutdownProfilesCancelled
    try:
        run_profile_test(args.profile_id, restore_only=args.restore_only)
    except ShutdownProfilesCancelled:
        print("Isolated VM test cancelled; see its recovery report.", file=sys.stderr)
        return 130
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="wsctl",
        description="Save and restore the current GNOME workspace state",
    )
    result.add_argument("--version", action="version", version=__version__)
    sub = result.add_subparsers(dest="command", required=True)
    from .alerts import add_parser as add_alerts_parser
    add_alerts_parser(sub)

    save_parser = sub.add_parser("save", help="replace the saved workspace state")
    save_parser.add_argument(
        "--allow-partial", action="store_true",
        help="save even when a companion, placement, or Codex ID is unavailable",
    )
    save_parser.add_argument("--shutdown-safe", action="store_true", help=argparse.SUPPRESS)
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

    startup_parser = sub.add_parser("startup", help="restore once per login and start missing applications")
    startup_parser.add_argument("category", nargs="?", choices=CATEGORIES)
    startup_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    startup_parser.add_argument("--session", action="append", help=argparse.SUPPRESS)
    startup_parser.add_argument("--select", action="store_true", help=argparse.SUPPRESS)
    startup_parser.add_argument("--dry-run", action="store_true")
    startup_parser.add_argument("--no-place", action="store_true", help="skip GNOME window placement")
    startup_parser.add_argument("--force", action="store_true", help="run again during the current login")
    startup_parser.add_argument("--wait", type=float, default=15, help="seconds to wait for GNOME (default: 15)")
    startup_parser.add_argument("--await-tmux", action="store_true", help=argparse.SUPPRESS)
    startup_parser.set_defaults(
        func=cmd_startup, repair_processes=False, adopt_restored=False,
        verify_codex=True,
    )

    tmux_parser = sub.add_parser("tmux", help="tmux-resurrect/continuum hook interface")
    tmux_sub = tmux_parser.add_subparsers(dest="tmux_command", required=True)
    tmux_begin = tmux_sub.add_parser("begin", help="mark tmux-resurrect restore as running")
    tmux_begin.add_argument("owner_pid", nargs="?", type=int, help=argparse.SUPPRESS)
    tmux_begin.set_defaults(func=cmd_tmux_begin)
    tmux_save = tmux_sub.add_parser("save", help="annotate a resurrect state file and autosave terminals")
    tmux_save.add_argument("state_file", help="state-file path passed by tmux-resurrect")
    tmux_save.set_defaults(func=cmd_tmux_save)
    tmux_restore = tmux_sub.add_parser("restore", help="place the desktop after continuum restore")
    tmux_restore.add_argument("--wait", type=float, default=15)
    tmux_restore.set_defaults(func=cmd_tmux_restore)
    tmux_end = tmux_sub.add_parser("end", help="clear the continuum restore lifecycle marker")
    tmux_end.set_defaults(func=cmd_tmux_end)
    tmux_contract = tmux_sub.add_parser("contract", help="contract Codex panes using the saved recipe")
    tmux_contract.add_argument("state_file")
    tmux_contract.set_defaults(func=cmd_tmux_contract)
    tmux_configure = tmux_sub.add_parser(
        "configure", help="install the resilient tmux-resurrect Codex mapping"
    )
    tmux_configure.add_argument("--config", help=argparse.SUPPRESS)
    tmux_configure.set_defaults(func=cmd_tmux_configure)
    tmux_unconfigure = tmux_sub.add_parser(
        "unconfigure", help="remove the managed tmux-resurrect Codex mapping"
    )
    tmux_unconfigure.add_argument("--config", help=argparse.SUPPRESS)
    tmux_unconfigure.set_defaults(func=cmd_tmux_unconfigure)

    profiles_parser = sub.add_parser(
        "shutdown-profiles",
        help="inspect or install pre-shutdown jobs with verified rollback",
    )
    profiles_sub = profiles_parser.add_subparsers(
        dest="shutdown_profiles_command", required=True
    )
    profiles_list = profiles_sub.add_parser(
        "list", help="validate and list configured shutdown profiles"
    )
    profiles_list.add_argument(
        "--action", choices=sorted(SUPPORTED_ACTIONS), default="poweroff"
    )
    profiles_list.add_argument(
        "--probe", action="store_true", help="run each profile's read-only applicability probe"
    )
    profiles_list.add_argument("--json", action="store_true")
    profiles_list.set_defaults(func=cmd_shutdown_profiles_list)
    profiles_test = profiles_sub.add_parser(
        "test", help="hibernate and restore one Windows VM without ending the host session"
    )
    profiles_test.add_argument("profile_id")
    profiles_test.add_argument(
        "--restore-only", action="store_true",
        help="only recover the VM and viewer; do not run the guest hibernation cycle",
    )
    profiles_test.set_defaults(func=cmd_shutdown_profiles_test)
    profiles_install = profiles_sub.add_parser(
        "install-qemu-windows",
        help="install a Windows guest-hibernation profile for a protected QEMU VM",
    )
    profiles_install.add_argument("vm_directory")
    profiles_install.add_argument("--id", default="windows-word-vm")
    profiles_install.add_argument("--label", default="Windows VM hibernation")
    profiles_install.add_argument("--timeout", type=float, default=180)
    profiles_install.add_argument("--force", action="store_true")
    profiles_install.set_defaults(func=cmd_shutdown_profiles_install_qemu_windows)
    from .deployment import add_parser as add_deployment_parser
    from .provider_progress import cmd_placement_progress
    add_deployment_parser(sub)
    progress = sub.add_parser("placement-progress", help=argparse.SUPPRESS)
    progress.set_defaults(func=cmd_placement_progress)
    return result


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parser().parse_args(argv)
        from . import operations
        inherited = operations.current()
        if inherited is not None:
            operations.context_from_status(status_path(), inherited.mode, inherited.operation_id,
                                           allow_expired=args.command == "placement-progress")
        elif args.command == "startup" or getattr(args, "login_status", False):
            operations.context_from_status(status_path(), "startup")
        return int(args.func(args))
    except (BrowserUnavailable, OSError, ValueError, RuntimeError) as error:
        if args is not None and (
            getattr(args, "command", None) == "startup"
            or (
                getattr(args, "command", None) == "tmux"
                and getattr(args, "tmux_command", None) == "restore"
            )
        ):
            if isinstance(error, RestoreJobsFailed):
                set_overall("failed", str(error))
            else:
                fail_active(str(error))
        print(f"wsctl: {error}", file=sys.stderr)
        return 2
