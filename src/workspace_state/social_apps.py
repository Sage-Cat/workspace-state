"""Restore explicitly visible social-app windows, never background processes."""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .desktop import (
    cancel_expected_window, capture_shell, expect_window, move_window_result,
    resolve_placement_target, wait_for_scoped_handoff,
)
from .concurrency import completed_jobs
from .util import CommandError, launch_graphical_service, run, workspace_launch_matches
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount, ProviderRestoreError, placement_accepted, PlacementPending)
from .provider_results import waiting_only, placement_frame_matches as _placement_matches
from .provider_results import native_handoff_token


@dataclass(frozen=True)
class App:
    id: str
    label: str
    aliases: tuple[str, ...]
    desktop_ids: tuple[str, ...]
    executables: tuple[str, ...]


TELEGRAM_SNAP_LAUNCHER = "telegram-desktop_telegram-desktop"
SNAP_REFRESH_WAIT = 15
SNAP_LAUNCH_GRACE = 2
SNAP_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
SNAP_DESKTOP_ROOT = Path("/var/lib/snapd/desktop")


APPS = (
    App("slack", "Slack", ("slack", "slack_slack", "com.slack.slack"),
        ("slack_slack", "slack", "com.slack.Slack"), ("slack",)),
    App("discord", "Discord", ("discord", "discord_discord", "com.discordapp.discord"),
        ("discord_discord", "discord", "com.discordapp.Discord"), ("discord",)),
    App("telegram", "Telegram", ("telegramdesktop", "telegram-desktop", "telegram-desktop_telegram-desktop", "org.telegram.desktop"),
        (TELEGRAM_SNAP_LAUNCHER, "org.telegram.desktop", "telegramdesktop", "telegram-desktop"),
        ("telegram-desktop", "telegram")),
    App("viber", "Viber", ("viber", "viber_viber", "com.viber.viber"),
        ("viber_viber", "viber", "com.viber.Viber"), ("viber",)),
)
APP_BY_ID = {app.id: app for app in APPS}
Reporter = Callable[[str, str, int, int], None]
MAX_WINDOWS = 16
APP_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}")
DESKTOP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}")


def configured_apps() -> tuple[App, ...]:
    """Read local desktop identities without putting launch commands in recipes."""
    path = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "workspace-state/desktop-apps.toml"
    try:
        raw = tomllib.loads(path.read_text())
    except FileNotFoundError:
        return APPS
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise CommandError(f"Cannot read desktop app configuration {path}: {error}") from error
    if set(raw) != {"apps"} or not isinstance(raw["apps"], list):
        raise CommandError(f"Invalid desktop app configuration {path}: expected [[apps]] entries")
    apps = list(APPS)
    fields = {"id", "label", "aliases", "desktop_ids", "executables"}
    identities = {value.lower() for app in apps for value in (*app.aliases, *app.desktop_ids)}
    executables = {value.lower() for app in apps for value in app.executables}
    for index, value in enumerate(raw["apps"], 1):
        prefix = f"Invalid desktop app configuration {path}, entry {index}"
        if not isinstance(value, dict) or set(value) != fields:
            raise CommandError(f"{prefix}: expected {', '.join(sorted(fields))}")
        identifier, label = value["id"], value["label"]
        if not isinstance(identifier, str) or APP_ID.fullmatch(identifier) is None:
            raise CommandError(f"{prefix}: id must be a stable lowercase app identifier")
        if identifier in {app.id for app in apps}:
            raise CommandError(f"{prefix}: duplicate app id {identifier!r}")
        if not isinstance(label, str) or not label.strip() or len(label) > 80 or not label.isprintable():
            raise CommandError(f"{prefix}: label must be a nonempty printable string of at most 80 characters")
        lists = {}
        for name in ("aliases", "desktop_ids", "executables"):
            values = value[name]
            if not isinstance(values, list) or not values or any(
                not isinstance(item, str) or not item or item != item.strip()
                or len(item) > 128 or not item.isprintable() for item in values
            ):
                raise CommandError(f"{prefix}: {name} must be a nonempty list of nonempty strings")
            if name in {"desktop_ids", "executables"} and any(DESKTOP_ID.fullmatch(item) is None for item in values):
                raise CommandError(f"{prefix}: {name} must contain identifiers, not paths or commands")
            if name == "desktop_ids" and any(item.endswith(".desktop") for item in values):
                raise CommandError(f"{prefix}: desktop_ids must omit the .desktop suffix")
            normalized = [item.lower() for item in values]
            if len(normalized) != len(set(normalized)):
                raise CommandError(f"{prefix}: duplicate {name}")
            lists[name] = tuple(normalized if name != "desktop_ids" else values)
        app = App(identifier, label.strip(), **lists)
        app_identities = {item.lower() for item in (*app.aliases, *app.desktop_ids)}
        if identities & app_identities or executables.intersection(app.executables):
            raise CommandError(f"{prefix}: app identity overlaps another configured or built-in app")
        identities.update(app_identities)
        executables.update(app.executables)
        apps.append(app)
    return tuple(apps)



def matching_windows(app: App, shell: dict[str, Any]) -> list[dict[str, Any]]:
    matches = []
    for window in shell.get("windows", []):
        aliases = {str(value).lower() for value in window.get("app_ids", [])}
        aliases.update(str(window.get(key) or "").lower() for key in ("app_id", "wm_class"))
        if aliases.intersection(app.aliases):
            matches.append(window)
    return sorted(matches, key=lambda window: int(window.get("id", 0)))


def running_apps(proc_root: Path = Path("/proc"), *, apps: tuple[App, ...] | None = None) -> set[str]:
    """Inspect exact executable names for this user, not arbitrary argv text."""
    apps = configured_apps() if apps is None else apps
    result = set()
    for entry in proc_root.iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            executable = (entry / "exe").resolve(strict=True).name.lower()
        except OSError:
            continue
        result.update(app.id for app in apps if executable in app.executables)
    return result


def capture_social_apps(shell: dict[str, Any], *, include_native_ids: bool = False) -> dict[str, Any]:
    if not shell.get("available"):
        raise CommandError("Cannot capture social apps: GNOME window state is unavailable")
    apps = configured_apps()
    names = {item["index"]: item["name"] for item in shell.get("workspaces", [])}
    running = running_apps(apps=apps)
    result = {}
    for app in apps:
        windows = matching_windows(app, shell)
        displayed = [window for window in windows if window.get("state") != "minimized"]
        placements = []
        for window in displayed:
            placement = {key: window.get(key) for key in (
                "workspace", "monitor", "monitor_identity", "monitor_intent",
                "monitor_geometry", "geometry", "geometry_relative", "state",
            )}
            placement["workspace_name"] = names.get(window.get("workspace"))
            if app.id == "steam":
                if window.get("title") == "Steam":
                    placement["steam_main"] = True
                if isinstance(window.get("title"), str) and window["title"]:
                    # Steam's windows share one app id; the title tells a chat
                    # or friends window from any other window of the same frame.
                    placement["steam_title"] = window["title"]
            if include_native_ids:
                placement["_native_id"] = window["id"]
            placements.append(placement)
        active = bool(windows) or app.id in running
        result[app.id] = {"running": active,
                          "mode": "windowed" if placements else "background" if active else "stopped",
                          "windows": placements}
        if include_native_ids:
            result[app.id]["_native_window_count"] = len(windows)
    validate_social_apps(result)
    return result


def validate_social_apps(records: Any) -> None:
    if not isinstance(records, dict) or not set(APP_BY_ID).issubset(records) or any(
        not isinstance(identifier, str) or APP_ID.fullmatch(identifier) is None for identifier in records
    ):
        raise ValueError("App state must contain the built-in app records and valid extra app identifiers")
    for app_id, record in records.items():
        if not isinstance(record, dict) or not isinstance(record.get("running"), bool):
            raise ValueError(f"Invalid social app state: {app_id}")
        mode, windows = record.get("mode"), record.get("windows")
        if mode not in {"windowed", "background", "stopped"} or not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
            raise ValueError(f"Invalid social app mode/windows: {app_id}")
        if (mode == "windowed") != bool(windows) or record["running"] != (mode != "stopped"):
            raise ValueError(f"Inconsistent social app visibility: {app_id}")
        for placement in windows:
            if not isinstance(placement, dict) or not isinstance(placement.get("workspace_name"), str) or not placement["workspace_name"]:
                raise ValueError(f"Social app window lacks a workspace name: {app_id}")
            if not isinstance(placement.get("workspace"), int) or placement["workspace"] < 0:
                raise ValueError(f"Invalid social app workspace: {app_id}")
            identity = placement.get("monitor_intent") or placement.get("monitor_identity")
            if not isinstance(identity, dict) or not any(identity.get(key) for key in ("edid_hash", "serial", "connector")):
                raise ValueError(f"Social app window lacks a display identity: {app_id}")
            if placement.get("state") not in {"normal", "maximized", "fullscreen"}:
                raise ValueError(f"Invalid displayed social app window state: {app_id}")
            for key in ("geometry", "geometry_relative"):
                geometry = placement.get(key)
                if not isinstance(geometry, dict) or any(type(geometry.get(field)) is not int for field in ("x", "y", "width", "height")) or geometry["width"] <= 0 or geometry["height"] <= 0:
                    raise ValueError(f"Invalid social app window geometry: {app_id}")


def _desktop_source(identifier: str) -> Path | None:
    directories = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))]
    directories += [Path(value) for value in os.environ.get(
        "XDG_DATA_DIRS", f"/usr/local/share:/usr/share:{SNAP_DESKTOP_ROOT}").split(":") if value]
    candidates = (directory / "applications" / f"{identifier}.desktop" for directory in directories)
    return next((entry for entry in candidates if entry.is_file()), None)


def desktop_id(app: App) -> str:
    for identifier in app.desktop_ids:
        if _desktop_source(identifier) is not None:
            return identifier
    raise CommandError(f"{app.label}: no installed desktop launcher found")


def _target(placement: dict[str, Any]) -> dict[str, Any]:
    return resolve_placement_target(placement, provider="social app", shell=capture_shell())


def _snap_name(identifier: str) -> str | None:
    """The snap that owns a launcher installed by snapd, else None.

    snapd exports `<snap>_<app>.desktop` under its own desktop root only; a
    local or distribution launcher of the same name is not a snap launch.
    """
    name, separator, _app = identifier.partition("_")
    if (not separator or SNAP_NAME.fullmatch(name) is None
            or _desktop_source(identifier) != SNAP_DESKTOP_ROOT / "applications" / f"{identifier}.desktop"):
        return None
    return name


def _snap_refresh_active(snap: str, timeout: float) -> bool | None:
    """True while snapd has an unfinished change for this snap; None when unknown."""
    try:
        response = json.loads(run(
            ["/usr/bin/snap", "debug", "api", "--disable-auth", "--fail",
             f"/v2/changes?select=in-progress&for={snap}"], timeout=timeout,
        ))
    except (CommandError, OSError, ValueError):
        return None
    if (not isinstance(response, dict) or response.get("type") != "sync"
            or type(response.get("status-code")) is not int or response["status-code"] != 200):
        return None
    changes = response.get("result")
    if not isinstance(changes, list) or any(
        not isinstance(change, dict) or not isinstance(change.get("kind"), str)
        or type(change.get("ready")) is not bool for change in changes
    ):
        return None
    return any(not change["ready"] and change["kind"] in {"auto-refresh", "refresh-snap", "multi-update"}
               for change in changes)


def _wait_for_snap_refresh(snap: str, label: str, deadline: float, notify: Callable[[str], None],
                           already_started: Callable[[], bool]) -> bool:
    """Wait for a confirmed refresh and detect an independent start of the app."""
    waiting = False
    while (remaining := deadline - time.monotonic()) > 0:
        active = _snap_refresh_active(snap, min(1, remaining))
        if already_started():
            return True
        if active is True and not waiting:
            notify(f"{label}: waiting for the in-progress snap refresh")
            waiting = True
        if active is False or not waiting:
            return False
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    if not waiting:
        return False
    if already_started():
        return True
    raise CommandError(f"{label}: snap refresh did not finish within the launch wait budget")


def _refresh_interrupted_launch(snap: str, label: str, deadline: float, notify: Callable[[str], None],
                                window_present: Callable[[], bool]) -> bool:
    """After a launch that shows nothing, wait out a refresh; True means launch once more."""
    grace = min(deadline, time.monotonic() + SNAP_LAUNCH_GRACE)
    while time.monotonic() < grace:
        if window_present():
            return False
        time.sleep(.2)
    if window_present() or _snap_refresh_active(snap, max(.1, min(1, deadline - time.monotonic()))) is not True:
        return False
    notify(f"{label}: waiting for the in-progress snap refresh")
    end = min(deadline, time.monotonic() + SNAP_REFRESH_WAIT)
    while _snap_refresh_active(snap, max(.1, min(1, end - time.monotonic()))) is True:
        if time.monotonic() >= end:
            raise CommandError(f"{label}: snap refresh did not finish within the launch wait budget")
        time.sleep(min(1, max(0, end - time.monotonic())))
    return not window_present()


def _is_saved_steam_window(window: dict[str, Any], target: dict[str, Any]) -> bool:
    """A recorded title must still name the window; older recipes have none."""
    title = target.get("steam_title")
    return not (isinstance(title, str) and title) or window.get("title") == title


def _matches_steam_frame(window: dict[str, Any], target: dict[str, Any]) -> bool:
    # Older checkpoints store placement alone. Width and height can identify
    # Steam's normal windows before they reach the saved workspace or display.
    if not _is_saved_steam_window(window, target):
        return False
    if target.get("steam_main") is True:
        return window.get("title") == "Steam"
    if (target.get("_legacy_main_candidate") and window.get("title") == "Steam"
            and window.get("state") == "normal"):
        return True
    if window.get("state") != target["state"]:
        return False
    if target["state"] != "normal":
        return True
    geometry = window.get("geometry") or {}
    return all(type(geometry.get(key)) is int and abs(geometry[key] - target["geometry"][key]) <= 3
               for key in ("width", "height"))


def _restore_window(app: App, target: dict[str, Any], *, claimed: set[int],
                    deadline: float, no_place: bool, settle_seconds: float,
                    notify: Callable[[str], None],
                    on_selected: Callable[[int], None] | None = None,
                    scoped: bool = False, reusable: frozenset[int] = frozenset(),
                    launch_token: str | None = None) -> int:
    """Follow a launcher's window replacement without extending its deadline.

    Discord's updater is a NORMAL window too. Its ID must not stay pinned after
    it disappears, and a newly launched splash must not immediately count as a
    restored main window. Claim only the final, continuously verified window.
    """
    window_id = None
    pending = False
    stable_since = None
    last_move = float("-inf")
    detail = "no matching window appeared"
    steam = app.id == "steam"
    candidate_since = 0.0
    while time.monotonic() < deadline:
        shell = capture_shell()
        if not shell.get("available"):
            raise CommandError(f"{app.label}: GNOME window state became unavailable")
        if time.monotonic() >= deadline:
            break
        candidates = [item for item in matching_windows(app, shell) if item["id"] not in claimed and
                      (not scoped or (item["id"] in reusable and item["workspace"] == target["workspace"]
                       and not item.get("sticky"))
                       or (launch_token and workspace_launch_matches(item["pid"], launch_token)))]
        if steam and (not pending or all(item["id"] != window_id for item in candidates)):
            matches = [item for item in candidates if _matches_steam_frame(item, target)
                       or (item["id"] == window_id and _is_saved_steam_window(item, target)
                           and _placement_matches(item, target))]
            if candidates and not matches:
                detail = "no Steam window matches the saved frame"
            elif len(matches) > 1:
                detail = "more than one Steam window matches the saved frame"
            candidates = matches if len(matches) == 1 else []
        elif scoped and not steam and len(candidates) > 1:
            raise CommandError(f"{app.label}: more than one window matches the scoped launch")
        window = next((item for item in candidates if item["id"] == window_id), None)
        if window is None:
            pending = False
            stable_since = None
            last_move = float("-inf")
            if window_id is not None:
                detail = f"window {window_id} disappeared; waiting for a replacement"
                notify(f"{app.label}: {detail}")
                window_id = None
            candidates.sort(key=lambda item: (
                item.get("workspace") != target["workspace"],
                item.get("monitor") != target["monitor"],
            ))
            if candidates:
                window = candidates[0]
                window_id = window["id"]
                candidate_since = time.monotonic()
                if on_selected and not steam:
                    on_selected(window_id)
                notify(f"{app.label}: verifying window {window_id}")
        if window and steam:
            if time.monotonic() - candidate_since < settle_seconds:
                time.sleep(.2)
                continue
            if on_selected:
                on_selected(window_id)
        if window and (no_place or _placement_matches(window, target)):
            detail = f"window {window_id} has the saved placement but is still settling"
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            if now - stable_since >= settle_seconds or (steam and last_move == float("-inf")):
                return window_id
        else:
            stable_since = None
            # One receipt owns this window's final placement, including a
            # lock delay. Retrying it can lose that receipt when a later read
            # crosses the caller deadline. Only a replacement window needs a
            # new submission; continue observing this one until we hand off.
            if window and not pending and time.monotonic() - last_move >= 1:
                detail = (
                    f"window {window_id}: expected workspace {target['workspace_name']} "
                    f"({target['workspace']}), display {target['monitor']}, {target['state']}, "
                    f"geometry {target['geometry']}; observed workspace {window.get('workspace')}, "
                    f"display {window.get('monitor')}, {window.get('state')}, "
                    f"geometry {window.get('geometry')}"
                )
                active = shell.get("active_workspace")
                pending = _place_social_window(app, window_id, target, active, deadline=deadline,
                                               **({"require_local": window_id in reusable, "scoped": True} if scoped else {}))
                last_move = time.monotonic()
        time.sleep(.2)
    if pending:
        raise PlacementPending(f"{app.label}: placement accepted; awaiting compositor verification — {detail}",
                               pending if isinstance(pending, str) else None)
    raise CommandError(f"{app.label}: restoration timed out — {detail}")


def _place_social_window(app: App, window_id: int, target: dict[str, Any], active: Any,
                         *, deadline: float, require_local: bool = False, scoped: bool = False) -> str | bool:
    # The native owner serializes configure work after this exact submission.
    shell = capture_shell()
    if not shell.get("available"):
        raise CommandError(f"{app.label}: GNOME window state became unavailable")
    window = next((item for item in matching_windows(app, shell) if item["id"] == window_id), None)
    if (window is None or _placement_matches(window, target)
            or (app.id == "steam" and not _matches_steam_frame(window, target))):
        return False
    if require_local and (window.get("workspace") != target["workspace"] or window.get("sticky")):
        raise CommandError(f"{app.label}: the window moved before scoped placement")
    if time.monotonic() >= deadline:
        raise CommandError(f"{app.label}: placement deadline elapsed before submission")
    if "staged_placement_handoff" not in shell.get("capabilities", []):
        raise CommandError("GNOME staged placement handoff capability is unavailable")
    result = move_window_result(window_id, dict(target, stage_before_handoff=True))
    if not placement_accepted(result):
        if not any(item["id"] == window_id for item in matching_windows(app, capture_shell())):
            return False
        reason = result.get("message")
        message = f"{app.label}: GNOME rejected the final saved placement"
        raise CommandError(f"{message} ({reason})" if reason else message)
    if result.get("status") == "verified":
        return False
    token = native_handoff_token(result)
    if not token:
        raise CommandError(f"{app.label}: GNOME did not retain the final workspace handoff")
    if scoped and wait_for_scoped_handoff(token, deadline):
        return False
    return token


def restore_social_apps(records: dict[str, Any] | None, *, dry_run: bool = False,
                        no_place: bool = False, workspace: str | None = None,
                        reporter: Reporter | None = None, timeout: float = 30,
                        workspace_switch: bool = False, excluded_windows: frozenset[int] = frozenset()) -> int:
    apps = configured_apps()
    app_by_id = {app.id: app for app in apps}
    progress_lock = threading.Lock()
    completed = 0
    total = len(apps)
    evidence_results = []
    errors = {}

    def report(state: str, message: str, current: int) -> None:
        with progress_lock:
            current = max(current, completed)
            print(message, flush=True)
            if reporter:
                reporter(state, message, current, total)
    if records is None:
        report("skipped", "No social app checkpoint; nothing will be launched", len(apps))
        return 0
    validate_social_apps(records)
    missing = sorted(set(records) - set(app_by_id))
    total += len(missing)
    for identifier in missing:
        message = f"{identifier}: saved desktop app is not configured"
        errors[identifier] = message
        evidence_results.append(ProviderItemResult("social-apps", identifier,
            PhaseEvidence(EvidenceState.FAILED, message), PhaseEvidence(EvidenceState.SKIPPED),
            PhaseEvidence(EvidenceState.SKIPPED), attention=(message,)))
        completed += 1
        report("running", message + "; not launching", completed)
    def restore_app(app: App) -> int:
        record = records.get(app.id)
        if record is None:
            report("running", f"{app.label}: no saved state; not launching", completed)
            return 0
        placements = [item for item in record["windows"] if not workspace or item["workspace_name"] == workspace]
        if record["mode"] != "windowed" or not placements:
            report("running", f"{app.label}: {record['mode']}; not launching" if not workspace else f"{app.label}: no saved windows selected; not launching", completed)
            return 0
        if dry_run:
            report("running", f"{app.label}: would restore {len(placements)} saved window(s)", completed)
            return len(placements)
        tokens = []
        try:
            targets = [dict(placement) if no_place else _target(placement) for placement in placements]
            if app.id == "steam" and sum(item["state"] == "maximized" for item in record["windows"]) == 1:
                for target in targets:
                    if target["state"] == "maximized" and "steam_main" not in target:
                        target["_legacy_main_candidate"] = True
            report("running", f"{app.label}: checking {len(targets)} saved window(s)", completed)
            existing = matching_windows(app, capture_shell())
            reusable = frozenset(window["id"] for window in existing
                                 if window["workspace"] == targets[0]["workspace"]
                                 and window["id"] not in excluded_windows and not window.get("sticky"))
            if workspace_switch and not reusable and existing:
                raise CommandError(f"{app.label}: no independent launch is available while another window is open")
            launched = (len(reusable) if workspace_switch else len(existing)) < len(targets)
            launch_token = None
            deadline = None
            snap_started = None
            snap = None
            if launched:
                launcher = desktop_id(app)
                snap = _snap_name(launcher)
                if snap is not None:
                    started = time.monotonic()
                    deadline = started + timeout
                    baseline = 0 if workspace_switch else len(existing)
                    was_running = app.id in running_apps(apps=(app,))

                    def snap_started() -> bool:
                        nonlocal existing
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return False
                        shell = capture_shell(timeout=min(1, remaining))
                        if not shell.get("available"):
                            raise CommandError(f"{app.label}: GNOME window state became unavailable")
                        if workspace_switch:
                            return False  # A scoped launch owns its own window.
                        existing = matching_windows(app, shell)
                        return len(existing) > baseline or (not was_running and app.id in running_apps(apps=(app,)))

                    launched = not _wait_for_snap_refresh(
                        snap, app.label, min(deadline, started + SNAP_REFRESH_WAIT),
                        lambda message: report("running", message, completed), snap_started,
                    )
                    if launched and time.monotonic() >= deadline:
                        raise CommandError(f"{app.label}: restoration deadline elapsed before launch")
            if launched:
                # An app-wide expectation would move a sibling window before
                # the restore loop could identify the saved one.
                if not no_place and not workspace_switch and app.id != "steam" and len(record["windows"]) == 1:
                    token = expect_window(app.aliases[0], targets[0])
                    if token:
                        tokens.append(token)
                    if snap_started is not None:
                        launched = not snap_started()
            if launched:
                launch_token = uuid.uuid4().hex if workspace_switch else None
                if deadline is not None and time.monotonic() >= deadline:
                    raise CommandError(f"{app.label}: restoration deadline elapsed before launch")
                launch_options = {"workspace_launch": launch_token} if launch_token else {}
                launch_graphical_service(["/usr/bin/gtk-launch", launcher], f"social-{app.id}", **launch_options)
                report("running", f"{app.label}: launched; waiting for its window", completed)
                if snap_started is not None and _refresh_interrupted_launch(
                        snap, app.label, deadline, lambda message: report("running", message, completed),
                        snap_started):
                    # snapd began refreshing between the probe and the launch;
                    # the launch is retried once, after the change finished.
                    launch_graphical_service(["/usr/bin/gtk-launch", launcher], f"social-{app.id}",
                                             **launch_options)
                    report("running", f"{app.label}: launched again after the snap refresh", completed)
            if deadline is None:
                deadline = time.monotonic() + timeout
            claimed: set[int] = set()
            item_errors = []
            for index, target in enumerate(targets):
                selected_ids = []
                try:
                    window_id = _restore_window(
                        app, target, claimed=claimed, deadline=deadline,
                        no_place=no_place, settle_seconds=3 if launched else 1,
                        notify=lambda message: report("running", message, completed),
                        on_selected=selected_ids.append,
                        scoped=workspace_switch, reusable=reusable, launch_token=launch_token,
                    )
                    claimed.add(window_id)
                    evidence = ProviderItemResult("social-apps", f"{app.id}:{index + 1}",
                        PhaseEvidence(EvidenceState.VERIFIED, f"Observed app window {window_id}"),
                        PhaseEvidence(EvidenceState.SKIPPED, "App content recovery is owned by the application"),
                        PhaseEvidence(EvidenceState.SKIPPED if no_place else EvidenceState.VERIFIED),
                        reused=window_id in {window["id"] for window in existing}, native_window_id=window_id)
                except (CommandError, OSError, ValueError) as error:
                    claimed.update(selected_ids)
                    item_errors.append(f"window {index + 1}: {error}")
                    evidence = ProviderItemResult("social-apps", f"{app.id}:{index + 1}",
                        identity=PhaseEvidence(EvidenceState.VERIFIED if selected_ids else EvidenceState.FAILED,
                                               "Exact app window selected" if selected_ids else str(error)),
                        content=PhaseEvidence(EvidenceState.SKIPPED),
                        placement=PhaseEvidence(EvidenceState.WAITING if isinstance(error, PlacementPending) else EvidenceState.FAILED,
                                                str(error), True, getattr(error, "request_id", None)))
                with progress_lock:
                    evidence_results.append(evidence)
            if item_errors:
                raise ProviderRestoreError("; ".join(item_errors))
            report("running", f"{app.label}: restored {len(targets)} window(s)" + (" (placement disabled)" if no_place else " on their saved workspace/display"), completed)
            return len(targets)
        except (CommandError, OSError, ValueError) as error:
            if not isinstance(error, ProviderRestoreError):
                with progress_lock:
                    evidence_results.append(ProviderItemResult("social-apps", app.id,
                        PhaseEvidence(EvidenceState.UNKNOWN), PhaseEvidence(EvidenceState.SKIPPED),
                        PhaseEvidence(EvidenceState.FAILED, str(error), True), attention=(str(error),)))
            report("running", f"{app.label}: failed — {error}", completed)
            raise
        finally:
            for token in tokens:
                cancel_expected_window(token)

    restored = 0
    jobs = {app.id: (lambda app=app: restore_app(app)) for app in apps}
    for name, result, error in completed_jobs(jobs, serial=dry_run):
        with progress_lock:
            completed += 1
            current = completed
        if error:
            errors[name] = f"{app_by_id[name].label}: {error}"
        else:
            restored += int(result or 0)
        report("running", f"{app_by_id[name].label}: completed", current)
    if errors:
        message = "; ".join(errors[identifier] for identifier in (*app_by_id, *missing) if identifier in errors)
        report("waiting" if waiting_only(evidence_results) else "failed", message, total)
        raise ProviderRestoreError(message, evidence_results)
    verb = "Would restore" if dry_run else "Restored"
    report("ready" if restored else "skipped", f"{verb} {restored} social app window(s); background/stopped apps were not launched", total)
    return ProviderCount(restored, evidence_results)
