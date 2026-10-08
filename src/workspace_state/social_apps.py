"""Restore explicitly visible social-app windows, never background processes."""

from __future__ import annotations

import os
import re
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .desktop import (
    cancel_expected_window, capture_shell, expect_window, move_window_result,
    resolve_placement_target, placement_lock,
)
from .concurrency import completed_jobs
from .util import CommandError, launch_graphical_service
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount, ProviderRestoreError, placement_accepted, placement_pending, PlacementPending)
from .provider_results import waiting_only, placement_frame_matches as _placement_matches


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


class _StagingIncomplete(CommandError):
    """Yield the placement gate, then retry within the app's original budget."""


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


def capture_social_apps(shell: dict[str, Any]) -> dict[str, Any]:
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
            placements.append(placement)
        active = bool(windows) or app.id in running
        result[app.id] = {"running": active,
                          "mode": "windowed" if placements else "background" if active else "stopped",
                          "windows": placements}
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


def desktop_id(app: App) -> str:
    directories = [Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))]
    directories += [Path(value) for value in os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share:/var/lib/snapd/desktop").split(":") if value]
    for identifier in app.desktop_ids:
        if any((directory / "applications" / f"{identifier}.desktop").is_file() for directory in directories):
            return identifier
    raise CommandError(f"{app.label}: no installed desktop launcher found")


def _target(placement: dict[str, Any]) -> dict[str, Any]:
    return resolve_placement_target(placement, provider="social app", shell=capture_shell())


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
        if time.monotonic() >= deadline:
            break
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
        if window and (no_place or _placement_matches(window, target)):
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
                try:
                    pending = _place_social_window(app, window_id, target, active, deadline=deadline)
                except _StagingIncomplete as error:
                    # The short staging lease protects other providers from a
                    # slow client. It must not discard the remaining app budget
                    # or publish a waiting receipt without a final request.
                    detail = str(error)
                    pending = False
                last_move = time.monotonic()
        time.sleep(.2)
    if pending:
        raise PlacementPending(f"{app.label}: placement accepted; awaiting compositor verification — {detail}",
                               pending if isinstance(pending, str) else None)
    raise CommandError(f"{app.label}: restoration timed out — {detail}")


def _place_social_window(app: App, window_id: int, target: dict[str, Any], active: Any,
                         *, deadline: float) -> str | bool:
    """Serialize compositor mutations; candidate polling remains parallel."""
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not placement_lock.acquire(timeout=remaining):
        raise CommandError(f"{app.label}: placement deadline elapsed while waiting for another window")
    try:
        return _place_social_window_locked(app, window_id, target, active, deadline=deadline)
    finally:
        placement_lock.release()


def _place_social_window_locked(app: App, window_id: int, target: dict[str, Any], active: Any,
                                *, deadline: float) -> str | bool:
    # Chrome may have focused another workspace while this worker waited for
    # the gate. Resolve staging from the state observed under that gate.
    shell = capture_shell()
    if not shell.get("available"):
        raise CommandError(f"{app.label}: GNOME window state became unavailable")
    window = next((item for item in matching_windows(app, shell) if item["id"] == window_id), None)
    if window is None or _placement_matches(window, target):
        return False
    if time.monotonic() >= deadline:
        raise CommandError(f"{app.label}: placement deadline elapsed while waiting for another window")
    active = shell.get("active_workspace", active)
    moves = []
    if isinstance(active, int) and active != target["workspace"]:
        staging = dict(target, workspace=active)
        staging.pop("workspace_name", None)
        moves.append(staging)
    moves.append(target)
    pending = False
    for index, destination in enumerate(moves):
        if time.monotonic() >= deadline:
            raise CommandError(f"{app.label}: placement deadline elapsed before submitting its destination")
        result = move_window_result(window_id, destination)
        pending = placement_pending(result)
        if not placement_accepted(result):
            if not any(item["id"] == window_id for item in matching_windows(app, capture_shell())):
                return False
            raise CommandError(f"{app.label}: GNOME rejected placement of window {window_id}: {result}")
        if index + 1 < len(moves):
            # Moving to an inactive workspace before the client acknowledges
            # this resize leaves its old frame frozen there. Observe one stable
            # active-workspace placement before issuing the final handoff.
            stable_since = None
            window: dict[str, Any] = {}
            last_stage_move = time.monotonic()
            staging_deadline = min(deadline, last_stage_move + 5)
            while time.monotonic() < staging_deadline:
                shell = capture_shell()
                if not shell.get("available"):
                    raise CommandError(f"{app.label}: GNOME window state became unavailable")
                window = next((item for item in matching_windows(app, shell) if item["id"] == window_id), None)
                if window is None:
                    return False
                now = time.monotonic()
                if now >= staging_deadline:
                    # A slow state query must not authorize a late retry or
                    # final handoff after the original staging budget expired.
                    continue
                # A pending launch expectation can already have delivered the
                # final destination. Let the outer loop verify its stability.
                if _placement_matches(window, target):
                    return False
                if _placement_matches(window, destination):
                    if stable_since is None:
                        stable_since = now
                    if now - stable_since >= .4:
                        break
                else:
                    stable_since = None
                    # Startup configure events can replace our first move or
                    # retain the previous monitor's maximized work area.
                    # Reapply only this active-workspace stage, without
                    # extending the gate or handing off an unsettled frame.
                    if now - last_stage_move >= 1:
                        result = move_window_result(window_id, destination)
                        pending = placement_pending(result)
                        if not placement_accepted(result):
                            if not any(item["id"] == window_id for item in matching_windows(app, capture_shell())):
                                return False
                            raise CommandError(f"{app.label}: GNOME rejected staging of window {window_id}: {result}")
                        last_stage_move = time.monotonic()
                time.sleep(.1)
            else:
                # This token proves only staging, not the requested final
                # workspace. Never let the background observer promote it to
                # completed restoration. Release the gate for other windows.
                raise _StagingIncomplete(
                    f"{app.label}: staging did not settle before handoff; expected workspace "
                    f"{target.get('workspace_name', target['workspace'])} ({target['workspace']}), "
                    f"display {target['monitor']}, {target['state']}; observed workspace "
                    f"{window.get('workspace')}, display {window.get('monitor')}, "
                    f"{window.get('state')}, geometry {window.get('geometry')}"
                )

    return (result.get("token") or True) if pending else False


def restore_social_apps(records: dict[str, Any] | None, *, dry_run: bool = False,
                        no_place: bool = False, workspace: str | None = None,
                        reporter: Reporter | None = None, timeout: float = 30) -> int:
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
