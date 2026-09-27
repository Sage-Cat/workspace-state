"""Restore explicitly visible social-app windows, never background processes."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .desktop import (
    cancel_expected_window, capture_shell, expect_window, move_window_result,
    remap_monitor, remap_workspace, serialized_placement,
)
from .concurrency import completed_jobs
from .util import CommandError, launch_graphical_service
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount, ProviderRestoreError, placement_matches, placement_accepted, placement_pending, PlacementPending)
from .provider_results import waiting_only


@dataclass(frozen=True)
class App:
    id: str
    label: str
    aliases: tuple[str, ...]
    desktop_ids: tuple[str, ...]
    executables: tuple[str, ...]


APPS = (
    App("slack", "Slack", ("slack", "slack_slack", "com.slack.slack"),
        ("slack_slack", "slack", "com.slack.Slack"), ("slack",)),
    App("discord", "Discord", ("discord", "discord_discord", "com.discordapp.discord"),
        ("discord_discord", "discord", "com.discordapp.Discord"), ("discord",)),
    App("telegram", "Telegram", ("telegramdesktop", "telegram-desktop", "telegram-desktop_telegram-desktop", "org.telegram.desktop"),
        ("telegram-desktop_telegram-desktop", "org.telegram.desktop", "telegramdesktop", "telegram-desktop"),
        ("telegram-desktop", "telegram")),
    App("viber", "Viber", ("viber", "viber_viber", "com.viber.viber"),
        ("viber_viber", "viber", "com.viber.Viber"), ("viber",)),
)
APP_BY_ID = {app.id: app for app in APPS}
Reporter = Callable[[str, str, int, int], None]
MAX_WINDOWS = 16


def matching_windows(app: App, shell: dict[str, Any]) -> list[dict[str, Any]]:
    matches = []
    for window in shell.get("windows", []):
        aliases = {str(value).lower() for value in window.get("app_ids", [])}
        aliases.update(str(window.get(key) or "").lower() for key in ("app_id", "wm_class"))
        if aliases.intersection(app.aliases):
            matches.append(window)
    return sorted(matches, key=lambda window: int(window.get("id", 0)))


def running_apps(proc_root: Path = Path("/proc")) -> set[str]:
    """Inspect exact executable names for this user, not arbitrary argv text."""
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
        result.update(app.id for app in APPS if executable in app.executables)
    return result


def capture_social_apps(shell: dict[str, Any]) -> dict[str, Any]:
    if not shell.get("available"):
        raise CommandError("Cannot capture social apps: GNOME window state is unavailable")
    names = {item["index"]: item["name"] for item in shell.get("workspaces", [])}
    running = running_apps()
    result = {}
    for app in APPS:
        windows = matching_windows(app, shell)
        displayed = [window for window in windows if window.get("state") != "minimized"]
        placements = []
        for window in displayed:
            placement = {key: window.get(key) for key in (
                "workspace", "monitor", "monitor_identity", "monitor_intent",
                "monitor_geometry", "geometry", "geometry_relative", "state",
            )}
            placement["workspace_name"] = names.get(window.get("workspace"))
            placements.append(placement)
        active = bool(windows) or app.id in running
        result[app.id] = {"running": active,
                          "mode": "windowed" if placements else "background" if active else "stopped",
                          "windows": placements}
    validate_social_apps(result)
    return result


def validate_social_apps(records: Any) -> None:
    if not isinstance(records, dict) or set(records) != set(APP_BY_ID):
        raise ValueError("Social app state must contain exactly Slack, Discord, Telegram and Viber")
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


def desktop_id(app: App) -> str:
    directories = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))]
    directories += [Path(value) for value in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share:/var/lib/snapd/desktop").split(":") if value]
    for identifier in app.desktop_ids:
        if any((directory / "applications" / f"{identifier}.desktop").is_file() for directory in directories):
            return identifier
    raise CommandError(f"{app.label}: no installed desktop launcher found")


def _target(placement: dict[str, Any]) -> dict[str, Any]:
    shell = capture_shell()
    names = [item.get("name") for item in shell.get("workspaces", [])]
    if names.count(placement["workspace_name"]) != 1:
        raise CommandError(f"Saved social app workspace is unavailable or ambiguous: {placement['workspace_name']}")
    if not shell.get("available") or not shell.get("monitors"):
        raise CommandError("GNOME display state is unavailable")
    return remap_monitor(remap_workspace(placement), require_identity=True)


def _restore_window(app: App, target: dict[str, Any], *, claimed: set[int],
                    deadline: float, no_place: bool, settle_seconds: float,
                    notify: Callable[[str], None],
                    on_selected: Callable[[int], None] | None = None) -> int:
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
    while time.monotonic() < deadline:
        shell = capture_shell()
        if not shell.get("available"):
            raise CommandError(f"{app.label}: GNOME window state became unavailable")
        candidates = [item for item in matching_windows(app, shell) if item["id"] not in claimed]
        window = next((item for item in candidates if item["id"] == window_id), None)
        if window is None:
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
                if on_selected:
                    on_selected(window_id)
                notify(f"{app.label}: verifying window {window_id}")
        if window and (no_place or placement_matches(window, target)):
            detail = f"window {window_id} has the saved placement but is still settling"
            now = time.monotonic()
            if stable_since is None:
                stable_since = now
            if now - stable_since >= settle_seconds:
                return window_id
        else:
            stable_since = None
            if window and time.monotonic() - last_move >= 1:
                detail = (
                    f"window {window_id}: expected workspace {target['workspace_name']} "
                    f"({target['workspace']}), display {target['monitor']}, {target['state']}, "
                    f"geometry {target['geometry']}; observed workspace {window.get('workspace')}, "
                    f"display {window.get('monitor')}, {window.get('state')}, "
                    f"geometry {window.get('geometry')}"
                )
                active = shell.get("active_workspace")
                pending = _place_social_window(app, window_id, target, active)
                last_move = time.monotonic()
        time.sleep(.2)
    if pending:
        raise PlacementPending(f"{app.label}: placement accepted; awaiting compositor verification — {detail}",
                               pending if isinstance(pending, str) else None)
    raise CommandError(f"{app.label}: restoration timed out — {detail}")


@serialized_placement
def _place_social_window(app: App, window_id: int, target: dict[str, Any], active: Any) -> bool:
    """Serialize compositor mutations; candidate polling remains parallel."""
    # Chrome may have focused another workspace while this worker waited for
    # the gate. Resolve staging from the state observed under that gate.
    active = capture_shell().get("active_workspace", active)
    moves = []
    if isinstance(active, int) and active != target["workspace"]:
        staging = dict(target, workspace=active)
        staging.pop("workspace_name", None)
        moves.append(staging)
    moves.append(target)
    pending = False
    for destination in moves:
        result = move_window_result(window_id, destination)
        pending = placement_pending(result)
        if not placement_accepted(result):
            if not any(item["id"] == window_id for item in matching_windows(app, capture_shell())):
                return
            raise CommandError(f"{app.label}: GNOME rejected placement of window {window_id}: {result}")

    return (result.get("token") or True) if pending else False


def restore_social_apps(records: dict[str, Any] | None, *, dry_run: bool = False,
                        no_place: bool = False, workspace: str | None = None,
                        reporter: Reporter | None = None, timeout: float = 30) -> int:
    progress_lock = threading.Lock()
    completed = 0
    evidence_results = []

    def report(state: str, message: str, current: int) -> None:
        with progress_lock:
            current = max(current, completed)
            print(message, flush=True)
            if reporter:
                reporter(state, message, current, len(APPS))
    if records is None:
        report("skipped", "No social app checkpoint; nothing will be launched", len(APPS))
        return 0
    validate_social_apps(records)
    def restore_app(app: App) -> int:
        record = records[app.id]
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
            report("running", f"{app.label}: checking {len(targets)} saved window(s)", completed)
            existing = matching_windows(app, capture_shell())
            launched = len(existing) < len(targets)
            if launched:
                launcher = desktop_id(app)
                if not no_place:
                    for target in targets[len(existing):]:
                        token = expect_window(app.aliases[0], target)
                        if token:
                            tokens.append(token)
                launch_graphical_service(["/usr/bin/gtk-launch", launcher], f"social-{app.id}")
                report("running", f"{app.label}: launched; waiting for its window", completed)
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
                    )
                    claimed.add(window_id)
                    evidence = ProviderItemResult("social-apps", f"{app.id}:{index + 1}",
                        PhaseEvidence(EvidenceState.VERIFIED, f"Observed app window {window_id}"),
                        PhaseEvidence(EvidenceState.SKIPPED, "App content recovery is owned by the application"),
                        PhaseEvidence(EvidenceState.SKIPPED if no_place else EvidenceState.VERIFIED),
                        reused=window_id in {window["id"] for window in existing})
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
    errors = {}
    jobs = {app.id: (lambda app=app: restore_app(app)) for app in APPS}
    for name, result, error in completed_jobs(jobs, serial=dry_run):
        with progress_lock:
            completed += 1
            current = completed
        if error:
            errors[name] = f"{APP_BY_ID[name].label}: {error}"
        else:
            restored += int(result or 0)
        report("running", f"{APP_BY_ID[name].label}: completed", current)
    if errors:
        message = "; ".join(errors[app.id] for app in APPS if app.id in errors)
        report("waiting" if waiting_only(evidence_results) else "failed", message, len(APPS))
        raise ProviderRestoreError(message, evidence_results)
    verb = "Would restore" if dry_run else "Restored"
    report("ready" if restored else "skipped", f"{verb} {restored} social app window(s); background/stopped apps were not launched", len(APPS))
    return ProviderCount(restored, evidence_results)
