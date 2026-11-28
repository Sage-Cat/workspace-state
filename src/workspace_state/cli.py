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
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime
from functools import partial
from pathlib import Path
from collections.abc import Collection
from typing import Any, Iterator

from . import __version__
from .browser import (
    BROWSER_PROTOCOL_VERSION,
    BROWSER_REQUIRED_CAPABILITIES,
    BrowserHostStalled,
    BrowserTransportUnavailable,
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
from .restore import (launch_terminal, missing_codex_ids, missing_conversation_ids,
                      dashboard_state, dashboard_tabs, place_terminal, recreate_tmux)
from .util import CommandError, atomic_json
from .conversations import identity as conversation_identity, record as conversation_record, key as conversation_key
from .storage import load, load_history, save, state_lock
from . import providers
from .social_apps import configured_apps, matching_windows as matching_social_windows
from .vscode import UnsafeEditorState
from .login_status import fail_active, set_overall, status_path, update_stage
from .login_status import TERMINAL_STATES, publish_provider_results, record_restored_snapshot
from .provider_results import (EvidenceState, PhaseEvidence, ProviderCount, RECIPE_AGE_ITEM, ADVISORY_PREFIX,
                               ProviderItemResult, ProviderRestoreError, blocking_attention, has_advisory,
                               waiting_only)
from .stage_state import deferred as stage_deferred, not_failed, placement_pending, stage_state
from .browser_reconciliation import carry_ambiguous_placements, stale_recipe_message
from .snapshot_view import (
    BROWSER_KEY, _browser_state, _counts, _named_placement, _placement_workspace,
    _session_workspace, _terminal_records, _workspace_groups,
)

CATEGORIES = ("terminals", "browsers", "social-apps", "file-manager", "vscode")
# Snapshot keys of the categories that a capture reports evidence for.
CAPTURE_KEYS = ("terminals", "browsers", *(provider.snapshot_key for provider in providers.PROVIDERS))
WORKSPACE_RESTORED_TARGET = "wsctl-workspace-restored.target"
CODEX_STABLE_SECONDS = 3.0
CODEX_VERIFY_TIMEOUT_SECONDS = 15.0
# One verification deadline per login, shared by every restore and finalizer
# that verifies conversations; it is measured with the system-wide monotonic clock.
CONVERSATION_DEADLINE_SECONDS = 180.0
DECK_RETRY_FILE = "agent-deck-resume.json"
# Mappings that older wsctl releases wrote into tmux configurations; only
# `tmux unconfigure` still recognizes them, to remove them on uninstall.
TMUX_CODEX_PROCESS_MAPPING = '"wsctl-codex->wsctl-codex-resume *" "wsctl-claude->wsctl-claude-resume *"'
TMUX_LEGACY_PROCESS_MAPPING = '"wsctl-codex->wsctl-codex-resume *"'


@dataclass(frozen=True)
class TerminalRestoreOutcome:
    restored: int
    codex_ready: int
    codex_total: int
    codex_verified: bool
    codex_deferred: bool = False
    results: tuple[ProviderItemResult, ...] = ()
    legacy_retained: int = 0
    # Login leaves Agent Deck conversations stopped instead of starting them.
    codex_inactive: bool = False
    codex_stopped: int = 0
    codex_note: str = ""
    codex_alert: str = ""


def _browser_restore_token_prefix(snapshot: dict[str, Any]) -> str:
    """Hash restore intent, never capture evidence or recovery observations.

    In particular, a newer observation of a retained recipe must not abandon
    its live Chrome claims. Runtime IDs and page titles are also observations;
    Chrome assigns new IDs after restarting and pages update titles freely.
    """
    def fields(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
        return {name: value[name] for name in names if name in value}

    profiles = []
    for profile in _browser_state(snapshot).get("profiles", []):
        intent = fields(profile, ("profile", "profile_directory", "app_id"))
        windows = []
        for window in profile.get("windows", []):
            saved = fields(window, (
                "id", "type", "state", "focused", "incognito", "app_id",
                "bounds", "geometry", "workspace", "workspace_index", "monitor", "placement",
            ))
            saved["tabs"] = [
                fields(tab, ("url", "pinned", "active", "group"))
                for tab in window.get("tabs", [])
            ]
            saved["groups"] = [
                fields(group, ("id", "title", "color", "collapsed"))
                for group in window.get("groups", [])
            ]
            windows.append(saved)
        intent["windows"] = windows
        profiles.append(intent)
    recipe = json.dumps(
        {"profiles": profiles}, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(recipe).hexdigest()[:16]


def _set_browser_state(snapshot: dict[str, Any], chrome: dict[str, Any]) -> None:
    snapshot["version"] = 4
    snapshot["browsers"] = {BROWSER_KEY: chrome}
    snapshot.pop("chrome", None)


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


UNRESOLVED_PROBLEM = "conversation session ID(s) are unresolved"
AUTO_CATEGORIES = ("terminals", "social-apps", "file-manager", "vscode")


def _only_unresolved(problems: list[str]) -> bool:
    """True when every terminal problem is a conversation awaiting its identity."""
    return bool(problems) and all(problem.endswith(UNRESOLVED_PROBLEM) for problem in problems)


def _terminal_problems(snapshot: dict[str, Any]) -> list[str]:
    failed = snapshot.get("capture_errors", {}).get("terminals")
    if failed:
        return ["terminal capture failed: " + "; ".join(map(str, failed))]
    missing = [
        terminal for terminal in snapshot.get("terminals", [])
        if terminal.get("placement") is None
    ]
    unresolved = sum(
        1 for session in snapshot.get("sessions", []) for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if conversation_record(pane) and not conversation_identity(pane)
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
        problems.append(f"{unresolved} {UNRESOLVED_PROBLEM}")
    session_names = {str(session.get("name")) for session in snapshot.get("sessions", [])}
    dangling = sorted({
        str(terminal.get("session")) for terminal in snapshot.get("terminals", [])
        if terminal.get("session") not in session_names
    })
    if dangling:
        problems.append("Alacritty clients reference missing tmux sessions: " + ", ".join(dangling))
    return problems


def _unidentified_codex_panes(sessions) -> list[str]:
    """Keep uncaptured identities visible even though no resume can be issued."""
    result = {}
    for session in sessions:
        for window in session.get("windows", []):
            for pane in window.get("panes", []):
                if conversation_record(pane) and not conversation_identity(pane):
                    position = (session["name"], window["index"], pane["index"])
                    label = pane.get("label") or pane.get("title") or "unnamed"
                    result[position] = f"{position[0]}:{position[1]}.{position[2]} ({label})"
    return list(result.values())


def _unidentified_codex_message(panes: list[str]) -> str:
    return (f"{len(panes)} pane(s) had no saved conversation UUID; automatic resume could not identify them: "
            + "; ".join(panes) + ". Resume their exact UUIDs manually; no identity was guessed.")


def _profile_names(chrome: dict[str, Any]) -> set[str]:
    return {str(profile.get("profile") or "Default") for profile in chrome.get("profiles", [])}


def _browser_problems(
    snapshot: dict[str, Any],
    previous: dict[str, Any] | None = None,
) -> list[str]:
    failed = snapshot.get("capture_errors", {}).get("browsers")
    if failed:
        return ["Google Chrome capture failed: " + "; ".join(map(str, failed))]
    chrome = _browser_state(snapshot)
    missing = [
        window for _profile, window in browser_windows(chrome)
        if (window.get("workspace_index") is None or window.get("monitor") is None)
        and window.get("placement_ambiguous") is not True
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


def _failed_terminal_capture() -> dict[str, Any]:
    """Return the base of a capture whose terminal category failed."""
    from .capture import snapshot_time
    return {"version": 2, "name": "current", "created_at": snapshot_time(),
            "desktop": {}, "terminals": [], "sessions": []}


def _report_carried_dashboards(tabs: list[str]) -> None:
    if tabs:
        print("Agent Deck dashboard scope kept from the previous checkpoint for tab(s) that run no "
              "dashboard now: " + ", ".join(tabs), file=sys.stderr)


def _capture_all(*, browsers: bool = True, browsers_by_title: bool = False,
                 only: Collection[str] | None = None, context=None,
                 include_native_ids: bool = False, native_window_ids: frozenset[int] | None = None,
                 previous_browsers: dict[str, Any] | None = None) -> dict[str, Any]:
    """Capture each category independently.

    Category capture is read-only; only the coordinator merges it into the
    canonical recipe. A category that fails is recorded in `capture_errors`
    and never stops the others; the caller decides whether that category keeps
    its last good state. `only` names the categories to capture; terminals are
    always captured, because the snapshot is built on them. With
    `browsers_by_title`, Chrome windows are identified by title alone.
    """
    from .checkpoint import CaptureContext
    from .browser_reconciliation import digest
    from . import agent_deck
    context = context or CaptureContext.begin()
    exact_ids = {"include_native_ids": True} if include_native_ids else {}
    scoped_ids = {"native_window_ids": native_window_ids} if native_window_ids is not None else {}

    def capture_terminals():
        arguments = {"shell": context.shell, "names": list(context.names)}
        if not agent_deck.enabled():
            snapshot = capture(**arguments)
        else:
            routes: set[str] = set()
            inventory = agent_deck.inventory(visible_routes=routes)
            snapshot = capture(**arguments, deck_routes=routes)
            snapshot["agent_deck"] = inventory
        if include_native_ids:
            for terminal in snapshot["terminals"]:
                terminal["_native_id"] = (terminal.get("placement") or {}).get("id")
        return snapshot

    jobs = {
        "terminals": capture_terminals,
        **{provider.snapshot_key: partial(provider.capture, context.shell, exact_ids, scoped_ids)
           for provider in providers.PROVIDERS},
    }
    if browsers:
        jobs["browsers"] = lambda: capture_browser(shell=context.shell, names=list(context.names),
                                                   **({"by_title": True} if browsers_by_title else {}), **exact_ids)
    if only is not None:
        jobs = {name: job for name, job in jobs.items()
                if name == "terminals" or name.replace("_", "-") in only}
    values, errors = {}, {}
    for name, result, error in completed_jobs(jobs):
        if error is not None:
            errors[name] = error
        else:
            values[name] = result
    snapshot = values["terminals"] if "terminals" in values else _failed_terminal_capture()
    for name in CAPTURE_KEYS:
        if name in errors:
            snapshot.setdefault("capture_errors", {}).setdefault(name, []).append(str(errors[name]))
    if isinstance(errors.get("vscode"), UnsafeEditorState):
        snapshot["capture_errors"]["vscode_unsafe"] = True
    if "browsers" in values:
        carry_ambiguous_placements(values["browsers"], previous_browsers)
        _set_browser_state(snapshot, values["browsers"])
    for name in (provider.snapshot_key for provider in providers.PROVIDERS):
        if name in values:
            snapshot[name] = values[name]
    context.verify()
    # A category that was not captured has no evidence here, so this context
    # can never vouch for data carried forward from an earlier capture.
    evidence = {
        name: {"state": "failed" if name in errors else "captured",
               "detail": str(errors[name]) if name in errors else "provider capture completed"}
        for name in CAPTURE_KEYS if name in jobs
    }
    if "browsers" in values:
        evidence["browsers"]["content_digest"] = digest(values["browsers"])
    snapshot["capture_context"] = context.evidence(evidence, created_at=snapshot["created_at"])
    return snapshot


def _category_problems(snapshot: dict[str, Any], previous: dict[str, Any] | None, *,
                       browsers: bool = True) -> dict[str, list[str]]:
    """Return each capture category's problems; an empty list means complete."""
    errors = snapshot.get("capture_errors", {})

    def failed(key: str, label: str) -> list[str]:
        return [f"{label} capture failed: {item}" for item in errors.get(key, [])]

    return {
        "terminals": _terminal_problems(snapshot),
        "browsers": _browser_problems(snapshot, previous) if browsers else [],
        **{provider.name: failed(provider.snapshot_key, provider.label) for provider in providers.PROVIDERS},
    }


def _retain_last_good(snapshot: dict[str, Any], previous: dict[str, Any] | None,
                      category: str, failures: list[str]) -> str | None:
    """Keep the previous data of one incomplete category; None when there is none.

    Terminals keep their whole recipe: sessions, clients and the desktop they
    were placed on.
    """
    previous = previous or {}
    if category == "terminals":
        if "sessions" not in previous:
            return None
        for key in ("sessions", "terminals", "desktop", "agent_deck"):
            if key in previous:
                snapshot[key] = copy.deepcopy(previous[key])
            else:
                snapshot.pop(key, None)
    elif category == "browsers":
        prior = _browser_state(previous)
        if not prior:
            return None
        _set_browser_state(snapshot, copy.deepcopy(prior))
    else:
        prior = previous.get(category.replace("-", "_"))
        if not isinstance(prior, dict) or not prior:
            return None
        snapshot[category.replace("-", "_")] = copy.deepcopy(prior)
    return (f"retained the last-good {category} checkpoint because current capture was incomplete: "
            + "; ".join(failures))


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


def _checkpoint_login() -> dict[str, str] | None:
    """Bind explicit acceptance to the live login, independently of HUD success."""
    from . import operations
    generation = _login_generation_file()
    login_key = _login_key_file(generation)
    if generation is None or login_key is None:
        return None
    boot = _boot_id()
    if boot == "current-boot":
        return None
    inherited = operations.current()
    if inherited is not None and (inherited.boot_id != boot or inherited.login_generation != generation):
        return None
    return {"boot_id": boot, "login_generation": generation, "login_key": login_key}


def _change_digest(snapshot: dict[str, Any], category: str) -> str:
    """Digest a category without the Chrome observations pages and focus change freely."""
    from .checkpoint import category_data, category_digest
    if category != "browsers":
        return category_digest(snapshot, category)
    chrome = copy.deepcopy(category_data(snapshot, category))
    for profile in chrome.get("profiles") or []:
        for window in (profile.get("windows") or []) if isinstance(profile, dict) else []:
            if isinstance(window, dict):
                window.pop("focused", None)
                for tab in window.get("tabs") or []:
                    if isinstance(tab, dict):
                        tab.pop("title", None)
                        tab.pop("active", None)
    return hashlib.sha256(json.dumps(chrome, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _checkpoint_changed(snapshot: dict[str, Any], previous: dict[str, Any]) -> bool:
    """Compare what a checkpoint restores and its category states, not capture times."""
    from .checkpoint import CAPTURE_CATEGORIES
    old = previous.get("category_provenance")
    old = old if isinstance(old, dict) else {}
    new = snapshot.get("category_provenance") or {}
    if snapshot.get("pending") != previous.get("pending"):
        return True
    return any(
        _change_digest(snapshot, category) != _change_digest(previous, category)
        or (new.get(category) or {}).get("state") != (old.get(category) or {}).get("state")
        or (new.get(category) or {}).get("retained_placements")
        != (old.get(category) or {}).get("retained_placements")
        for category in CAPTURE_CATEGORIES
    )


def _save_auto(args: argparse.Namespace) -> int:
    """Publish a periodic checkpoint of every category.

    A category that fails keeps its last good state and never stops the
    others. Saved items that are not live and were not seen during this login
    stay pending. Moments that make the whole desktop unsafe to compare skip
    the checkpoint with exit 0. Chrome is captured by window titles alone, so
    the user's windows are never touched, and only after this login verified
    its Chrome restore or a manual save accepted the live windows. The final
    save keeps the Chrome recipe, because Chrome closes while it runs.
    """
    for flag in ("allow_partial", "verify_idle_codex"):
        if getattr(args, flag, False):
            raise RuntimeError(f"--auto cannot be combined with --{flag.replace('_', '-')}")
    return _auto_checkpoint(final=bool(getattr(args, "final", False)))[0]


def _seen_directory(login: dict[str, str] | None) -> Path | None:
    """The record of items seen live during `login`; None when the login is unknown."""
    if login is None:
        return None
    from .startup import startup_directory
    try:
        return startup_directory(runtime_dir(), login["boot_id"], login["login_key"])
    except OSError:
        return None


BROWSERS_ACCEPTED = "browsers-accepted.json"


def _accept_browsers(login: dict[str, str] | None) -> None:
    """Record that the user saved the live Chrome windows during `login`."""
    directory = _seen_directory(login) if login is not None else None
    if directory is not None:
        atomic_json(directory / BROWSERS_ACCEPTED, {"schema_version": 1, **login})


def _browsers_capturable(login: dict[str, str] | None) -> bool:
    """Whether a periodic capture may replace the saved Chrome recipe.

    Until this login verified its Chrome restore, or the user saved Chrome by
    hand during it, saved windows that were not restored exist only in the
    recipe; a capture of the live windows would silently drop them.
    """
    directory = _seen_directory(login) if login is not None else None
    if directory is None:
        return False
    from .provider_progress import login_status_document
    from .stage_state import stage_document, stage_verified
    stage = stage_document(login_status_document(login), "browsers")
    if stage and stage_verified(stage):
        return True
    try:
        accepted = json.loads((directory / BROWSERS_ACCEPTED).read_text())
    except (OSError, ValueError):
        return False
    return accepted == {"schema_version": 1, **login}


def _carry_pending(snapshot: dict[str, Any], previous: dict[str, Any] | None,
                   fresh: Collection[str], *, login: dict[str, str] | None,
                   held: Collection[str] = (), final: bool = False,
                   required_vscode: Collection[dict] = (),
                   dashboards: list[str] | None = None) -> list[str]:
    """Record what this capture saw live, then keep unseen saved items pending.

    `fresh` names the categories whose new capture is published; `held` names
    conversations whose client still runs but cannot be identified now. The
    final save at shutdown counts only what this capture saw live as seen:
    applications close while it runs, so a missing item is not a user's close.
    """
    from . import pending
    from .capture import snapshot_time
    from .codex_resume import unverified_start_ids
    # A supervised resume that has not verified its client is still an attempt.
    live = pending.live_keys(snapshot, fresh) - {
        pending.conversation_item(("codex", value)) for value in unverified_start_ids()}
    directory = _seen_directory(login)
    pending.record_seen(directory, live)
    seen = (live if final else pending.seen_keys(directory) | live) - set(held)
    if dashboards is not None and "terminals" in fresh:
        # After the observations were recorded and before pending.carry: a
        # scope unread for its tab returns to that same tab, and pending.carry
        # then finds the dashboard present and adds no window. The kept scope
        # is not recorded as seen. A dashboard seen live and gone was closed.
        from .agent_deck_restore import carry_dashboards
        dashboards.extend(carry_dashboards(snapshot, previous, seen))
    observed = pending.observed_terminals(snapshot)
    warnings = pending.carry(snapshot, previous, fresh=fresh, seen=seen,
                             now=str(snapshot.get("created_at") or snapshot_time()),
                             required_vscode=required_vscode)
    if "terminals" in fresh:
        pending.carry_unfinished(snapshot, directory, observed)
    return warnings


def _record_seen(keys: Collection[str]) -> None:
    from . import pending
    if keys:
        pending.record_seen(_seen_directory(_checkpoint_login()), keys)


@contextmanager
def _startup_restore_idle() -> Iterator[None]:
    """Hold the login restore lock without waiting; restore marks windows too."""
    from .storage import StateBusy
    descriptor = os.open(_startup_directory() / "restore.lock", os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise StateBusy("the login restore is running") from error
        yield
    finally:
        os.close(descriptor)


FINAL_CAPTURE_LOCK_SECONDS = 15.0


def _auto_checkpoint(categories: Collection[str] = (*AUTO_CATEGORIES, "browsers"), *,
                     final: bool = False) -> tuple[int, bool]:
    """Return the exit code and whether the saved terminals match this capture.

    A checkpoint never waits: another capture, the login restore or a save
    publishing at that moment skips it. It captures without the state lock and
    publishes only if the checkpoint bytes did not change meanwhile. The final
    save at shutdown waits for a running capture instead of skipping; a login
    restore in progress still skips it.
    """
    def skip(reason: str) -> tuple[int, bool]:
        print(f"wsctl: checkpoint skipped: {reason}", file=sys.stderr)
        return 0, False

    from .storage import StateBusy, capture_lock
    try:
        with (capture_lock(wait=True, timeout=FINAL_CAPTURE_LOCK_SECONDS) if final
              else capture_lock(wait=False)), _startup_restore_idle():
            return _checkpoint(skip, categories, final=final)
    except StateBusy as error:
        return skip(str(error))


def _graphical_session_ending() -> str | None:
    """Return why the graphical session is not steadily active, if it is not."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveState", "-p", "Job",
             "graphical-session.target"],
            capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "the graphical session state cannot be read"
    properties = dict(line.partition("=")[::2] for line in result.stdout.splitlines())
    if result.returncode != 0 or properties.get("ActiveState") != "active" or properties.get("Job"):
        return "the graphical session is not steadily active"
    return None


def _checkpoint(skip, categories: Collection[str], *, final: bool = False) -> tuple[int, bool]:
    from . import pending, placement_guard
    from .checkpoint import TopologyChanged, record_provenance, verify_capture_context
    from .storage import current_bytes, load_with_bytes
    try:
        previous, baseline = load_with_bytes()
    except FileNotFoundError:
        return skip("no checkpoint exists yet; run wsctl save once")
    login = _checkpoint_login()
    # Chrome closes while the final save runs; its windows would look closed.
    capture_browsers = "browsers" in categories and not final and _browsers_capturable(login)
    captured = set(categories) - ({"browsers"} if not capture_browsers else set())
    protection = placement_guard.for_capture(_seen_directory(login), previous, captured)
    try:
        snapshot = _capture_all(browsers=capture_browsers, browsers_by_title=True, only=captured,
                                **({"previous_browsers": _browser_state(previous)} if capture_browsers else {}))
    except TopologyChanged as error:
        return skip(str(error))
    conversation_warnings = list(snapshot.get("capture_warnings", {}).get("conversations", []))
    monitor_problem = _fallback_monitor_problem(snapshot, previous)
    if monitor_problem:
        return skip(monitor_problem)
    held = pending.keep_unchanged_clients(snapshot, previous)
    problems = {category: failures for category, failures
                in _category_problems(snapshot, previous, browsers=capture_browsers).items()
                if category in captured}
    if previous.get("sessions") and not snapshot.get("sessions") and not problems["terminals"]:
        problems["terminals"].append("tmux capture unexpectedly contains no sessions")
    # A conversation that is starting or compacting is saved as an unresolved
    # pane while its saved identity stays pending. Any other terminal gap
    # keeps the whole terminal recipe.
    fresh = {category for category, failures in problems.items()
             if not failures or (category == "terminals" and _only_unresolved(failures))}
    retained = {}
    for category, failures in problems.items():
        if category not in fresh:
            reason = _retain_last_good(snapshot, previous, category, failures)
            if reason is not None:
                retained[category] = reason
    kept = {*AUTO_CATEGORIES, "browsers"} - captured
    for category in kept - {"browsers"}:
        field = category.replace("-", "_")
        if field in previous:
            snapshot[field] = copy.deepcopy(previous[field])
    prior_browsers = _browser_state(previous)
    if "browsers" in kept and prior_browsers:
        _set_browser_state(snapshot, copy.deepcopy(prior_browsers))
    protected, absent_vscode = placement_guard.preserve(protection, snapshot, previous, fresh)
    if "vscode" in protected:
        from .vscode import MAX_WINDOWS
        if len(snapshot["vscode"]["windows"]) + len(absent_vscode) > MAX_WINDOWS:
            failures = ["unverified VS Code placements would exceed the saved window limit"]
            problems["vscode"] = failures
            retained["vscode"] = _retain_last_good(snapshot, previous, "vscode", failures)
            fresh.remove("vscode")
            protected.pop("vscode")
            absent_vscode = []
    held.update(key for keys in protected.values() for key in keys)
    carried_dashboards: list[str] = []
    warnings = _carry_pending(snapshot, previous, fresh, login=login, held=held, final=final,
                              required_vscode=absent_vscode, dashboards=carried_dashboards)
    try:
        verify_capture_context(snapshot)
    except TopologyChanged as error:
        return skip(str(error))
    if login != _checkpoint_login():
        return skip("login changed during capture")
    record_provenance(snapshot, previous, source="final-save" if final else "auto-save", owner=login,
                      retained=retained, problems=problems, kept=kept)
    for category, keys in protected.items():
        snapshot["category_provenance"][category]["retained_placements"] = sorted(keys)
    current = "terminals" in fresh
    # The final save always publishes, so the restored snapshot is the one
    # taken at power-off even on an idle desktop.
    if not final and not _checkpoint_changed(snapshot, previous):
        print("wsctl: checkpoint unchanged")
        return 0, current
    with state_lock(wait=False):
        # Only publication holds the state lock. A save published during this
        # capture makes the capture stale.
        if not final:
            # Applications closing for the session end would be published as
            # unseen; the final save owns this checkpoint.
            from .session_ending import marker_active
            if marker_active():
                return skip("the session is ending; the final save owns this checkpoint")
        # The marker is valid only while the agent lives. Publishing while the
        # desktop is being torn down would record closing applications, so the
        # previous checkpoint stays.
        ending = _graphical_session_ending()
        if ending:
            return skip(ending)
        if current_bytes() != baseline:
            return skip("the checkpoint changed during capture")
        path = save(snapshot)
    print(f"wsctl: checkpoint saved to {path}")
    for warning in conversation_warnings:
        print("Duplicate conversation pane: " + warning, file=sys.stderr)
    _report_carried_dashboards(carried_dashboards)
    report = [retained.get(category) or "; ".join(failures)
              for category, failures in problems.items() if failures]
    if report:
        print("Partial state: " + "; ".join(report), file=sys.stderr)
    for warning in warnings:
        print("Pending items: " + warning, file=sys.stderr)
    return 0, current


def cmd_save(args: argparse.Namespace) -> int:
    if getattr(args, "auto", False):
        return _save_auto(args)
    allow_partial = bool(getattr(args, "allow_partial", False))
    verify_idle_codex = bool(getattr(args, "verify_idle_codex", False))
    if getattr(args, "final", False):
        raise RuntimeError("--final is only valid together with --auto")
    from . import pending, placement_guard
    from .storage import capture_lock
    # A checkpoint holds the capture lock only while it captures.
    with capture_lock(timeout=60), state_lock():
        login = _checkpoint_login()
        try:
            previous = load()
        except FileNotFoundError:
            previous = None
        snapshot = _capture_all()
        conversation_warnings = list(snapshot.get("capture_warnings", {}).get("conversations", []))
        status_proofs = []
        if verify_idle_codex:
            from .codex_status import probe
            for session in snapshot.get("sessions", []):
                for window in session.get("windows", []):
                    for pane in window.get("panes", []):
                        codex = pane.get("codex")
                        if not codex or codex.get("session_id"):
                            continue
                        if codex.get("confidence") != "unknown":
                            raise RuntimeError("native status cannot override conflicting conversation evidence")
                        proof = probe(codex["pid"], pane["id"])
                        record = proof.capture_record()
                        if any(codex.get(key) != record.get(key) for key in ("pid", "start_ticks", "tty")):
                            raise RuntimeError("conversation client changed after capture; checkpoint preserved")
                        pane["codex"] = record
                        status_proofs.append(proof)
        held = pending.keep_unchanged_clients(snapshot, previous)
        monitor_problem = _fallback_monitor_problem(snapshot, previous)
        if monitor_problem:
            raise RuntimeError("state not saved: " + monitor_problem + ". The existing checkpoint was preserved.")
        capture_problems = _category_problems(snapshot, previous)
        terminal_problems = list(capture_problems["terminals"])
        browser_problems = list(capture_problems["browsers"])
        social_problems = list(capture_problems["social-apps"])
        file_manager_problems = list(capture_problems["file-manager"])
        vscode_problems = list(capture_problems["vscode"])
        terminals_accepted = allow_partial and not snapshot.get("capture_errors", {}).get("terminals")
        fresh = {category for category in pending.CATEGORIES
                 if not capture_problems[category] or (category == "terminals" and terminals_accepted)}
        retained: dict[str, str] = {}
        # A failed capture never becomes the baseline of its category: it
        # keeps its last good state while the other categories still save.
        # Only --allow-partial accepts a terminal capture with gaps, such
        # as an unresolved conversation that is saved as a plain shell.
        for category, failures in capture_problems.items():
            accepted = (category == "terminals" and allow_partial
                        and not snapshot.get("capture_errors", {}).get("terminals"))
            if failures and not accepted:
                reason = _retain_last_good(snapshot, previous, category, failures)
                if reason is not None:
                    retained[category] = reason
        carried_dashboards: list[str] = []
        pending_warnings = _carry_pending(snapshot, previous, fresh, login=login, held=held,
                                          dashboards=carried_dashboards)
        problems = list(dict.fromkeys(
            retained.get(category, problem)
            for category, failures in (
                ("terminals", terminal_problems), ("browsers", browser_problems),
                ("social-apps", social_problems), ("file-manager", file_manager_problems),
                ("vscode", vscode_problems),
            )
            for problem in failures
        ))
        from .checkpoint import record_provenance, verify_capture_context
        verify_capture_context(snapshot)
        if login != _checkpoint_login():
            raise RuntimeError("login changed during capture; previous checkpoint preserved")
        if status_proofs:
            from .codex_status import revalidate
            if not all(revalidate(proof) for proof in status_proofs):
                raise RuntimeError("native conversation status changed before publication; checkpoint preserved")
            from .native_status_evidence import remember
            remember(status_proofs, login)
        record_provenance(snapshot, previous, source="manual-save",
                          owner=login, retained=retained, problems=capture_problems)
        path = save(snapshot)
        placement_guard.accept(_seen_directory(login), fresh)
        if not browser_problems:
            _accept_browsers(login)
    terminals, sessions, codex, chrome_windows, tabs = _counts(snapshot)
    print(
        f"Saved {terminals} Alacritty windows, {sessions} tmux sessions, "
        f"{codex} AI conversations, and {chrome_windows} Chrome windows "
        f"({tabs} tabs) to {path}"
    )
    for app in configured_apps():
        record = snapshot.get("social_apps", {}).get(app.id)
        if record:
            print(f"  {app.label}: {record['mode']}, {len(record['windows'])} saved window(s)")
    if snapshot.get("file_manager"):
        print(f"  Default file manager: {len(snapshot['file_manager'].get('windows', []))} saved window(s)")
    if snapshot.get("vscode"):
        print(f"  VS Code workspaces: {len(snapshot['vscode'].get('windows', []))} saved window(s)")
    for warning in conversation_warnings:
        print("Duplicate conversation pane: " + warning, file=sys.stderr)
    _report_carried_dashboards(carried_dashboards)
    if problems:
        print("Partial state: " + "; ".join(problems), file=sys.stderr)
    if ("terminals" in retained
            and not snapshot.get("capture_errors", {}).get("terminals")):
        print("Pass --allow-partial to accept the current terminal capture anyway.", file=sys.stderr)
    for warning in pending_warnings:
        print("Pending items: " + warning, file=sys.stderr)
    described = pending.describe(snapshot)
    if described:
        print(f"{len(described)} saved item(s) stay pending until restored; "
              "'wsctl show' lists them and 'wsctl forget' drops one.", file=sys.stderr)
    # Exit 3 reports a published checkpoint that kept some last-good state.
    return 3 if problems and not allow_partial else 0


def _local_time(value: object) -> str:
    """Show a stored timestamp in local time; anything unparseable stays as stored."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return stamp.astimezone().isoformat(timespec="seconds") if stamp.tzinfo else value


def cmd_show(args: argparse.Namespace) -> int:
    snapshot = load()
    if args.json:
        print(json.dumps(snapshot, indent=2, ensure_ascii=False))
        return 0
    print(f"Checkpoint updated  {_local_time(snapshot.get('created_at'))}")
    for category, record in snapshot.get("category_provenance", {}).items():
        captured_at = _local_time(record.get("captured_at")) or "unknown"
        detail = f"; {record['retained_reason']}" if record.get("retained_reason") else ""
        print(f"  {category}: captured {captured_at}{detail}")
    stale = stale_recipe_message(snapshot)
    if stale:
        print(f"  STALE: Chrome recipe {stale.removeprefix(ADVISORY_PREFIX)}")
    print("\nWorkspace       Alacritty  tmux sessions  tmux windows  AI conversations  Chrome  tabs  Files  VS Code")
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
    for app in configured_apps():
        record = snapshot.get("social_apps", {}).get(app.id)
        if record:
            locations = ", ".join(
                f"{item['workspace_name']} / {(item.get('monitor_identity') or {}).get('connector', item.get('monitor'))}"
                for item in record["windows"]
            )
            print(f"  {app.label}: {record['mode']}" + (f" — {locations}" if locations else " — will not launch"))
    from .pending import describe
    items = describe(snapshot)
    if items:
        print("\nPending (saved, not yet restored or seen during this login; 'wsctl forget KEY' drops one):")
        for item in items:
            print(f"  {item['key']}  {item['label']}  since {_local_time(item['since'])}")
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    """Drop pending items and the saved data they keep."""
    from . import pending
    from .checkpoint import category_digest, category_provenance
    with state_lock():
        previous = load()
        snapshot = copy.deepcopy(previous)
        try:
            changed = pending.forget(snapshot, args.keys)
        except KeyError as error:
            print(f"wsctl: not pending: {error.args[0]}", file=sys.stderr)
            return 2
        # Forgetting changes saved data without a capture: each changed
        # category keeps its capture evidence, now naming the reduced content.
        records = snapshot.setdefault("category_provenance", {})
        for category in changed:
            record = category_provenance(previous, category)
            record["content_digest"] = category_digest(snapshot, category)
            if isinstance(record.get("adoption"), dict):
                record["adoption"]["content_digest"] = record["content_digest"]
            records[category] = record
        path = save(snapshot)
    print(f"Forgot {len(args.keys)} pending item(s); saved {path}")
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


def _live_terminal_clients(*, deck_managed: bool = False) -> dict[str, list[dict[str, Any]]]:
    live = capture(deck_routes=set()) if deck_managed else capture()
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


def _restore_deck_dashboard(snapshot: dict[str, Any], args: argparse.Namespace) -> TerminalRestoreOutcome:
    from . import agent_deck_migration

    if not args.workspace or len(args.session or []) != 1 or args.select:
        raise ValueError("--deck-dashboard requires one exact --workspace and one exact --session")
    saved = [item for item in _restore_items(snapshot) if item["name"] == args.session[0]
             and item["launch_terminal"] and item["placement"]
             and item["placement"].get("workspace_name") == args.workspace]
    if len(saved) != 1:
        raise ValueError("The exact tmux session must have one saved terminal placement in the workspace")
    profile, group = args.deck_dashboard
    message = agent_deck_migration.restore(
        saved[0], args.workspace, {"profile": profile, "group": group},
        name=args.deck_window if args.deck_window is not None else "Agent Deck",
        placeholder=args.deck_placeholder, dry_run=args.dry_run,
    )
    print(message)
    attention = getattr(message, "attention", None)
    if not attention or args.dry_run:
        return TerminalRestoreOutcome(1, 0, 0, True)
    # A dashboard away from its saved index is an item that needs attention:
    # the manual command ends with exit code 3, like any restore with such items.
    done = PhaseEvidence(EvidenceState.VERIFIED, "Dashboard opened and verified")
    item = ProviderItemResult("agent-deck", f"{args.session[0]}:dashboard", done, done,
                              PhaseEvidence(EvidenceState.SKIPPED, "Placement is handled by Alacritty"),
                              attention=(attention,))
    return TerminalRestoreOutcome(1, 0, 0, True, results=(item,))


def _restore_terminals(
    snapshot: dict[str, Any],
    args: argparse.Namespace,
) -> TerminalRestoreOutcome:
    if getattr(args, "deck_dashboard", None) is not None:
        return _restore_deck_dashboard(snapshot, args)
    sessions = _filtered_terminal_items(snapshot, args)
    from . import agent_deck, agent_deck_restore, tmux_server
    if "agent_deck" not in snapshot and not agent_deck.enabled():
        return _restore_terminal_layout(snapshot, args, sessions)
    report_status = bool(getattr(args, "login_status", False))
    login_deadline = None
    if report_status and not args.dry_run:
        login_deadline = _conversation_deadline()
        update_stage("codex", "running", "Reconciling Agent Deck conversations")
    restricted = bool(getattr(args, "workspace", None) or getattr(args, "session", None)
                      or getattr(args, "select", False))
    # An unreadable configuration must not fail the restore: stay inactive,
    # mark nothing (the policy is unknown) and report the reason.
    start, config_error = agent_deck_restore.start_setting()
    note = alert = ""
    if config_error:
        print("Agent Deck: " + config_error, file=sys.stderr)
    elif login_deadline is not None and not restricted and not start:
        # Dead sessions of the previous boot show as errors; record them stopped.
        # Do it before the dashboards open: a dashboard's first poll rewrites
        # the status of every session it still shows as running.
        # Leave most of the deadline for reconciling the conversations.
        budget = min(max(0.0, login_deadline - time.monotonic()) / 2, 20.0)
        marked, detail = (agent_deck.mark_dead_stopped(timeout=budget) if budget >= 1.0 else (0, ""))
        if marked:
            noun = "session" if marked == 1 else "sessions"
            note = f"; {marked} {noun} from the previous boot marked stopped"
        if detail:
            alert = detail
            print("Agent Deck: " + detail, file=sys.stderr)
    with tmux_server.workspace_socket():
        layout = _restore_terminal_layout(
            snapshot, args, agent_deck_restore.terminal_layout(sessions), deck_managed=True,
        )
    deadline = time.monotonic() + max(0, getattr(args, "wait", 30))
    if login_deadline is not None:
        deadline = min(deadline, login_deadline)
    result = agent_deck_restore.restore(
        snapshot, sessions, deadline=deadline, restricted=restricted, dry_run=args.dry_run,
        start=start,
    )
    if args.dry_run:
        print(f"Agent Deck: verify and reopen {result.total} saved conversation(s)")
    else:
        _record_deck_restore(result)
    if result.detail:
        print("Agent Deck: " + result.detail, file=sys.stderr)
    if result.legacy_retained:
        print("Agent Deck: " + _native_migration_note(result.legacy_retained))
    if result.unresolved:
        print(f"Agent Deck: {len(result.unresolved)} open record(s) have no native conversation ID and "
              "need attention; none was resumed or counted as restored: "
              + ", ".join(str(row.get("title") or row["id"]) for row in result.unresolved), file=sys.stderr)
    deferred = False
    if report_status and not args.dry_run and not restricted and result.pending:
        owner = _marker_context()
        if owner is not None:
            owner.check()
            atomic_json(_startup_directory() / DECK_RETRY_FILE, {
                "operation": owner.to_dict(),
                "agent_deck": {"version": 1, "sessions": list(result.pending)},
                "total": result.total, "ready": len(result.ready),
                "legacy_retained": result.legacy_retained,
            })
            deferred = True
    return TerminalRestoreOutcome(layout.restored, len(result.ready), result.total,
                                  result.verified or args.dry_run, deferred, layout.results,
                                  legacy_retained=result.legacy_retained,
                                  codex_inactive=not start, codex_stopped=len(result.stopped),
                                  codex_note=note,
                                  codex_alert="; ".join(part for part in (config_error, alert, result.detail) if part))


def _record_deck_restore(result) -> None:
    from .pending import conversation_item, deck_item
    keys = {deck_item(row) for row in result.ready}
    keys.update(conversation_item((row["tool"], row["native_id"]))
                for row in result.ready if row["tool"] in {"codex", "claude"} and row["native_id"])
    _record_seen(keys)


def _native_migration_note(count: int) -> str:
    noun = "conversation" if count == 1 else "conversations"
    return f"{count} unregistered native {noun} retained for separate recovery (wsctl show --json)"


def _await_dashboards(session: dict, name: str) -> tuple[list[str], set[str]]:
    from . import operations
    end = time.monotonic() + 2.0
    while True:
        missing, seen = dashboard_tabs(session, name)
        now = time.monotonic()
        if not missing or now >= end:
            return missing, seen
        owner = operations.current()
        if owner is not None:
            owner.check()
        time.sleep(min(0.10, end - now))


def _restore_terminal_layout(snapshot: dict[str, Any], args: argparse.Namespace,
                             sessions: list[dict[str, Any]], *,
                             deck_managed: bool = False) -> TerminalRestoreOutcome:
    report_status = bool(getattr(args, "login_status", False))
    total_sessions = len(sessions)
    codex_ids = {
        conversation_key(found)
        for session in sessions
        for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if (found := conversation_identity(pane)) is not None
    }
    unidentified = _unidentified_codex_panes(sessions)
    if report_status:
        update_stage(
            "terminals", "running" if sessions else "skipped",
            "Restoring Alacritty and tmux sessions" if sessions else "No saved terminal sessions",
            current=0, total=total_sessions,
        )
        if not deck_managed:
            update_stage(
                "codex", "running" if codex_ids or unidentified else "skipped",
                "Waiting for saved AI conversations" if codex_ids else
                _unidentified_codex_message(unidentified) if unidentified else "No saved AI conversations",
                current=0, total=len(codex_ids) + len(unidentified),
            )
    if not sessions:
        return TerminalRestoreOutcome(0, 0, 0, True)
    live_clients = _live_terminal_clients(deck_managed=deck_managed)
    workspace_switch = getattr(args, "workspace_switch", False)
    if workspace_switch:
        excluded_windows = args.excluded_windows
        # Live clients are named by GNOME's workspaces now; the saved list is
        # older and shifts when a workspace was removed or inserted since.
        names = list(getattr(args, "workspace_names", None) or snapshot.get("desktop", {}).get("workspace_names", []))
        live_clients = {
            name: [item for item in items if _placement_workspace(item.get("placement"), names) == args.workspace
                   and (item.get("placement") or {}).get("id") not in excluded_windows
                   and not (item.get("placement") or {}).get("sticky")]
            for name, items in live_clients.items()
        }
    restored_names: dict[str, str] = {}
    placement_results = []
    from .pending import pending_layout, session_key
    _pending_sessions, pending_windows = pending_layout(snapshot)

    def record_terminal(index, name, result=None, *, reused=False):
        phase = (PhaseEvidence(EvidenceState.WAITING, result.message, True, result.request_id)
                 if result is not None and result.request_id else
                 PhaseEvidence(EvidenceState.VERIFIED, "Observed saved Alacritty placement")
                 if result is not None else PhaseEvidence(EvidenceState.SKIPPED, "No native placement requested"))
        placement_results.append(ProviderItemResult(
            "alacritty", f"{name}/{index}",
            PhaseEvidence(EvidenceState.VERIFIED, "Exact restored tmux session and terminal occurrence"),
            PhaseEvidence(EvidenceState.VERIFIED, "Saved tmux structure reconciled; conversation readiness is tracked separately"),
            phase, reused=reused, native_window_id=(result.native_window_id if result else None)))

    failed_sessions: dict[str, str] = {}
    failed_results: list[ProviderItemResult] = []

    failed_windows = 0

    def fail_terminal(index, name, message):
        nonlocal failed_windows
        failed_windows += 1
        # Like a failed tmux session, one window never stops the others; its
        # saved record stays unfinished and the next login opens it again.
        failed_results.append(ProviderItemResult(
            "alacritty", f"{name}/{index}",
            PhaseEvidence(EvidenceState.VERIFIED, "Exact restored tmux session and terminal occurrence"),
            PhaseEvidence(EvidenceState.VERIFIED, "Saved tmux structure reconciled; conversation readiness is tracked separately"),
            PhaseEvidence(EvidenceState.FAILED, message, True)))
    for session_index, session in enumerate(sessions, start=1):
        saved_name = session["name"]
        if saved_name in failed_sessions:
            continue
        actual_name = restored_names.get(saved_name)
        if actual_name is None:
            try:
                actual_name, actions = recreate_tmux(
                    session,
                    dry_run=args.dry_run,
                    pending_windows=pending_windows.get(saved_name, ()),
                    **({"preserve_existing": True} if workspace_switch else {}),
                )
            except CommandError as error:
                # A foreign or ambiguous live session is preserved untouched
                # and must not stop the other saved sessions. It is not marked
                # seen, so its items stay pending.
                failed_sessions[saved_name] = str(error)
                failed_results.append(ProviderItemResult(
                    "tmux", saved_name,
                    PhaseEvidence(EvidenceState.FAILED, str(error)),
                    PhaseEvidence(EvidenceState.SKIPPED, "Saved items stay pending"),
                    PhaseEvidence(EvidenceState.SKIPPED, "No terminal was opened")))
                print(f"tmux session {saved_name}: {error}", file=sys.stderr)
                continue
            restored_names[saved_name] = actual_name
            if not args.dry_run:
                missing, observed = _await_dashboards(session, actual_name) if deck_managed else ([], set())
                if missing:
                    # A tab that should run a dashboard but shows a shell, or whose
                    # dashboard exited, is never verified; each one is named.
                    for tab in missing:
                        failed_results.append(ProviderItemResult(
                            "agent-deck", tab,
                            PhaseEvidence(EvidenceState.FAILED, "the tab shows a shell; its Agent Deck dashboard is not running"),
                            PhaseEvidence(EvidenceState.SKIPPED, "Saved dashboard stays pending"),
                            PhaseEvidence(EvidenceState.SKIPPED, "Dashboard placement is handled by Alacritty"),
                            attention=("the tab shows a shell; its Agent Deck dashboard is not running",),
                        ))
                else:
                    observed.add(session_key(saved_name))
                if observed:
                    _record_seen(observed)
            for action in actions:
                print(action)
        if not session.get("launch_terminal", True):
            print(f"restored detached tmux session {actual_name}")
            record_terminal(session_index, actual_name)
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
            if not args.dry_run:
                from .pending import clear_unfinished_session
                clear_unfinished_session(_seen_directory(_checkpoint_login()), saved_name)
            result = None
            if placement and not args.no_place:
                result = place_terminal(client, placement, dry_run=args.dry_run,
                                        **({"require_local": True} if workspace_switch else {}))
                print(result.message)
                if not result.success and not result.request_id:
                    fail_terminal(session_index, actual_name, result.message)
                    continue
            else:
                print(f"reuse existing Alacritty for {actual_name}")
            record_terminal(session_index, actual_name, result, reused=True)
            if report_status:
                update_stage(
                    "terminals", "running", f"Restored Alacritty for {actual_name}",
                    current=session_index, total=total_sessions,
                )
            continue
        launch_key = None
        if not args.dry_run:
            # Until its placement is verified, the saved window stays recorded
            # for this login, so a launch that dies does not lose it.
            from .pending import record_unfinished
            launch_key = record_unfinished(_seen_directory(_checkpoint_login()),
                                           {"session": saved_name, "placement": placement})
        result = launch_terminal(
            {**session, "name": actual_name},
            place=not args.no_place,
            dry_run=args.dry_run,
            **({"scoped": True} if workspace_switch else {}),
        )
        print(result.message)
        if not result.success and not result.request_id:
            fail_terminal(session_index, actual_name, result.message)
            continue
        if launch_key is not None and result.success:
            from .pending import clear_unfinished
            clear_unfinished(_seen_directory(_checkpoint_login()), [launch_key])
        elif launch_key is not None and result.request_id:
            from .pending import tag_unfinished
            tag_unfinished(_seen_directory(_checkpoint_login()), launch_key, result.request_id)
        record_terminal(session_index, actual_name, result if placement and not args.no_place else None)
        if report_status:
            update_stage(
                "terminals", "running", f"Restored Alacritty for {actual_name}",
                current=session_index, total=total_sessions,
            )
    failed_ids = {
        conversation_key(found)
        for session in sessions if session["name"] in failed_sessions
        for window in session.get("windows", [])
        for pane in window.get("panes", [])
        if (found := conversation_identity(pane)) is not None
    }
    codex_ready = len(codex_ids - failed_ids)
    codex_verified = not failed_ids
    codex_deferred = False
    if getattr(args, "verify_codex", False) and not args.dry_run and not deck_managed:
        unique_sessions = {
            session["name"]: session for session in sessions
            if session["name"] not in failed_sessions
        }
        verify_began = time.monotonic()
        deadline = verify_began + min(
            max(0, args.wait),
            # Restores initialize serially to avoid competing SQLite opens.
            # Keep one bounded allowance per conversation, capped by --wait.
            max(CODEX_VERIFY_TIMEOUT_SECONDS, 5.0 * len(codex_ids)),
        )
        shared_deadline: float | None = None
        if report_status and codex_ids:
            # A startup restore never outlives the login's shared deadline;
            # a manual restore keeps its own bounded wait and writes no file.
            shared_deadline = _conversation_deadline()
            if shared_deadline > verify_began:
                deadline = min(deadline, shared_deadline)
            else:
                # A repeated startup after the login's deadline: what it starts
                # now gets its own bounded wait, and the stale instant ends none.
                shared_deadline = None
        stable_since: float | None = None
        missing = set(codex_ids)
        while True:
            missing = _missing_conversations(
                ((session, restored_names.get(saved_name, saved_name))
                 for saved_name, session in unique_sessions.items()),
            )
            now = time.monotonic()
            if missing and missing <= {"codex:" + value for value in waiting_directory_ids()}:
                # Cloud mounts start only after terminal restoration finishes.
                # Let the finalizer mount them before verifying these wrappers.
                codex_verified = False
                codex_deferred = True
                codex_ready = len(codex_ids - failed_ids) - len(missing)
                if report_status:
                    update_stage(
                    "codex", "waiting",
                    f"{len(missing)} AI conversation(s) waiting for saved directories; "
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
                    "codex", "degraded" if stable and unidentified else "ready" if stable else "running",
                    (
                        _unidentified_codex_message(unidentified)
                        if stable and unidentified else
                        f"Waiting for {len(missing)} AI conversation(s)"
                        if missing
                        else (
                            "All saved AI conversations resumed"
                            if stable
                            else "Verifying resumed AI conversations remain live"
                        )
                    ),
                    current=len(codex_ids) - len(missing), total=len(codex_ids) + len(unidentified),
                )
            if stable:
                break
            # The deadline bounds only the wait for missing conversations; the
            # stability window of conversations that are all present completes.
            if now >= deadline and missing:
                codex_verified = False
                codex_ready = len(codex_ids - failed_ids) - len(missing)
                if (missing and shared_deadline is not None and now < shared_deadline):
                    # Only this call's own bound ended: the finalizer continues
                    # verifying until the shared deadline.
                    codex_deferred = True
                    update_stage(
                        "codex", "waiting",
                        f"{len(missing)} AI conversation(s) not verified yet; "
                        "verification continues until the login deadline",
                        current=codex_ready, total=len(codex_ids),
                    )
                elif report_status:
                    update_stage(
                        "codex", "degraded",
                        (
                            f"{len(missing)} AI conversation(s) could not be verified; "
                            "check their tmux panes for startup errors or prompts"
                            if missing else
                            "Conversation stability verification timed out; check the restored tmux panes"
                        ),
                        current=codex_ready, total=len(codex_ids),
                    )
                break
            time.sleep(0.5)
        if not args.dry_run:
            # A verified restore counts as seen, so a repeated startup accepts
            # the busy client that it finds in place.
            from .pending import conversation_item
            _record_seen({conversation_item(tuple(value.split(":", 1)))
                          for value in codex_ids - failed_ids - missing})
    if unidentified:
        codex_verified = False
        if report_status:
            update_stage("codex", "degraded", _unidentified_codex_message(unidentified),
                         current=codex_ready, total=len(codex_ids) + len(unidentified))
    return TerminalRestoreOutcome(
        len(sessions) - len(failed_sessions) - failed_windows, codex_ready, len(codex_ids) + len(unidentified),
        codex_verified, codex_deferred, tuple(placement_results) + tuple(failed_results),
    )


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
    workspace_switch = getattr(args, "workspace_switch", False)
    manual_navigation = (not start_browser and not workspace_switch
                         and not getattr(args, "_restore_all_categories", False)
                         and getattr(args, "category", None) == "browsers")
    excluded = snapshot.get("_workspace_switch_exclusions")
    blocked_profiles = snapshot.get("_workspace_switch_blocked_profiles", ())
    cold_profiles = snapshot.get("_workspace_switch_cold_profiles", blocked_profiles)
    if (excluded or not start_browser) and "latest_observation" in chrome:
        # A filtered login cannot vouch for the full native profile catalog, and
        # only a startup may prove a retained observation. Any other restore
        # pairs the recipe like an ordinary one.
        snapshot = copy.deepcopy(snapshot)
        chrome = _browser_state(snapshot)
        chrome.pop("latest_observation")
    reconciliation = None
    unpaired_items: list[tuple[str, str]] = []
    reconciliation_source = snapshot
    authority_guard = None
    owner = None
    def require_reconciliation_owner():
        nonlocal owner, authority_guard
        from .browser_reconciliation import reject
        from . import operations
        owner = operations.current()
        if owner is None or owner.mode != "startup":
            reject("startup operation ownership is unavailable")

        def guard():
            owner.check()
            document = json.loads(status_path().read_text())
            if (operations.current() != owner or not owner.matches(document)
                    or document.get("operation_state") != "running"):
                reject("startup operation ownership changed")

        authority_guard = guard
        guard()
    if start_browser and not args.dry_run and "latest_observation" in chrome:
        from .browser_reconciliation import retained_observation, reject
        retained_observation(snapshot)  # Validate evidence before any native mutation.
        if args.workspace is not None:
            reject("a retained browser catalog requires full-profile verification")
        require_reconciliation_owner()
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
    launched_browsers: list[str] = []
    blocked_errors = {}
    if not args.dry_run:
        required = {str(profile.get("profile") or "Default") for profile, _window in selected}
        if start_browser:
            if authority_guard:
                authority_guard()
            def start_each(profiles):
                # One launch, one deadline; a profile that does not connect fails only its own windows.
                if not profiles:
                    return []
                return ensure_browser_profiles({**chrome, "profiles": list(profiles)}, unavailable=blocked_errors)

            if cold_profiles:
                active = chrome.get("profiles", [])
                regular = [profile for profile in active if profile["profile"] not in cold_profiles]
                scoped = [profile for profile in active if profile["profile"] in cold_profiles]
                launched_browsers = start_each(regular)
                for profile in scoped:
                    name = profile["profile"]
                    try:
                        if name not in connected_profiles():
                            launched_browsers.extend(ensure_browser_profiles(
                                {**chrome, "profiles": [profile]}, workspace_switch=True))
                        elif name in blocked_profiles:
                            # Partially closed windows may still be attached.
                            # A filtered catalog cannot claim any such window.
                            windows = request_browser("list_windows", {}, profile=name, timeout=2)
                            if windows != []:
                                raise BrowserUnavailable(
                                    "Finish the interrupted workspace switch before reconciling this live profile")
                    except BrowserUnavailable as error:
                        blocked_errors[name] = str(error)
            else:
                launched_browsers = start_each(chrome.get("profiles", []))
            if authority_guard:
                authority_guard()
            for browser in launched_browsers:
                print(f"started {browser} companion")
        if workspace_switch:
            ensure_browser_profiles(chrome, workspace_switch=True)
        connected = set(connected_profiles())
        if not start_browser and not workspace_switch:
            for name in required.intersection(blocked_profiles):
                blocked_errors[name] = "Finish the interrupted workspace switch before restoring this profile"
        missing = sorted(required - connected - blocked_errors.keys())
        if missing and not start_browser:
            raise RuntimeError("Chrome companion is not connected for profile(s): " + ", ".join(missing))
        for profile_name in missing:  # At startup only this profile's windows fail.
            blocked_errors[profile_name] = f"Chrome companion is not connected for profile {profile_name!r}"
        for profile_name in sorted(required - blocked_errors.keys()):
            try:
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
            except BrowserUnavailable as error:
                if not start_browser:
                    raise
                blocked_errors[profile_name] = str(error)
        if start_browser:
            if report_status:
                update_stage(
                    "browsers", "running", "Waiting for Chrome's window and tab list to stabilize",
                    current=0, total=total_windows,
                )
            wait = getattr(args, "wait", 15)
            regular = required - set(cold_profiles) - blocked_errors.keys()
            if regular:
                wait_for_browser_settle(regular, timeout=wait)
            for profile_name in sorted(required.intersection(cold_profiles) - blocked_errors.keys()):
                try:
                    wait_for_browser_settle({profile_name}, timeout=wait)
                except BrowserUnavailable as error:
                    blocked_errors[profile_name] = str(error)
    matched = None
    if start_browser and not args.dry_run and "latest_observation" in chrome:
        from .browser_reconciliation import reconcile
        authority_guard()
        matched = reconcile(snapshot, {
            name: request_browser("capture", {}, profile=name)
            for name in sorted(connected_profiles())
        })
    elif not workspace_switch and not args.dry_run and "latest_observation" not in chrome:
        from .browser_reconciliation import reconcile_navigation
        # Chrome restored its own session. Pair its windows with the saved
        # placement; a login with a parked workspace only reads the profiles
        # of the filtered recipe.
        saved_profiles = {str(item.get("profile") or "Default") for item in chrome.get("profiles", [])}
        native = {}
        for name in sorted(connected_profiles()):
            if (excluded and name not in saved_profiles) or name in blocked_errors:
                continue
            try:
                native[name] = request_browser("capture", {"runtime_identity": True}, profile=name, timeout=2)
            except BrowserUnavailable as error:
                # Only this profile's windows fail; they are never guessed.
                blocked_errors.setdefault(name, f"Chrome could not be read: {error}")
        matched = reconcile_navigation(snapshot, native, allow_elimination=start_browser)
        if start_browser and matched is not None:
            require_reconciliation_owner()
    # Bind claims to the saved intent: the reconciled catalog carries live
    # tabs, so the active tab would otherwise change the token on a re-run.
    token_prefix = args.switch_token if workspace_switch else _browser_restore_token_prefix(snapshot)
    if matched is not None:
        if authority_guard:
            authority_guard()
        original_chrome, (chrome, reconciliation) = chrome, matched
        snapshot = copy.deepcopy(snapshot)
        _set_browser_state(snapshot, chrome)
        selected = _selected_browser_windows(snapshot, args.workspace)
        in_scope = {(str(profile.get("profile") or "Default"), str(window.get("id")))
                    for profile, window in browser_windows(original_chrome)
                    if args.workspace is None
                    or str(window.get("workspace") or "").casefold() == args.workspace.casefold()}
        unpaired_items = [
            (str(profile_name), str(label))
            for profile_name, labels in sorted(reconciliation.get("unpaired_windows", {}).items())
            for label in labels if (str(profile_name), str(label)) in in_scope
        ]
        total_windows = len(selected) + len(unpaired_items)
        if owner is not None:
            reconciliation["operation_context"] = owner.to_dict()
            authority_guard()
            atomic_json(_startup_directory() / "browser-reconciliation.json", reconciliation)
        if report_status:
            update_stage("browsers", "running", "Preserving current Chrome tabs and groups at their saved destinations"
                         if reconciliation.get("content_basis") == "current-native-session" else
                         "Verified intact newer Chrome session; reusing original windows and groups",
                         current=0, total=total_windows)
    completed = 0
    failures: list[str] = []
    evidence: list[ProviderItemResult] = []
    # A stalled native host answers no window of its profile: one transport failure
    # fails the rest of that profile without another request, not one deadline each.
    transport_down: dict[str, str] = {}
    for profile, window in selected:
        if authority_guard:
            authority_guard()
        profile_name = str(profile.get("profile") or "Default")
        label = str(window.get("id") or "window")
        evidence_before = len(evidence)
        try:
            if profile_name in blocked_errors:
                raise BrowserUnavailable(blocked_errors[profile_name])
            if profile_name in transport_down:
                raise BrowserHostStalled(transport_down[profile_name])
            restore_token = f"{token_prefix}:{profile_name}:{label}"
            restore_catalog = chrome
            one_window = {
                **chrome,
                "profiles": [{**profile, "windows": [window]}],
            }
            results = restore_browser(
                one_window,
                place=not args.no_place,
                dry_run=args.dry_run,
                restore_token_prefix=token_prefix,
                restore_catalog=restore_catalog,
                **({"observe_current_placement": True} if manual_navigation else {}),
                **({"authority_guard": authority_guard} if authority_guard else {}),
                **({"workspace_switch": True, "workspace": args.workspace} if workspace_switch else {}),
            )
            if authority_guard:
                authority_guard()
            for result in results:
                print(result.message)
                if result.evidence is not None:
                    evidence.append(result.evidence)
            item_evidence = evidence[evidence_before:]
            unsuccessful = [result for result in results if not result.success]
            if unsuccessful:
                if (manual_navigation and waiting_only(item_evidence)
                        and all(item.content.state != EvidenceState.WAITING for item in item_evidence)):
                    continue  # Observe the already submitted handoffs as one batch below.
                raise ProviderRestoreError("; ".join(result.message for result in unsuccessful), item_evidence)
            completed += 1
            if report_status:
                update_stage(
                    "browsers", "running", f"Restored Chrome {profile_name}/{label}",
                    current=completed, total=total_windows,
                )
        except (BrowserUnavailable, RuntimeError) as error:
            if authority_guard:
                authority_guard()  # Revocation aborts this worker, not just this window.
            if isinstance(error, BrowserHostStalled):
                transport_down.setdefault(profile_name, str(error))
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
    if manual_navigation and not args.dry_run:
        from .provider_progress import observe_manual_placements
        observed = observe_manual_placements(evidence)
        for before, after in zip(evidence, observed):
            if before.placement.state == EvidenceState.WAITING:
                if after.success:
                    completed += 1
                if after.placement.state == EvidenceState.FAILED:
                    failures.append(f"{after.item_id}: {after.placement.detail}")
        evidence = observed
    if reconciliation is not None:
        if not failures and selected:
            try:
                from .browser_reconciliation import ACTION, preservation_problems, reconcile, reject
                preserving = reconciliation.get("content_basis") == "current-native-session"
                native = {name: request_browser("capture", {"runtime_identity": True} if preserving else {}, profile=name)
                          for name in sorted(connected_profiles())}
                if preserving:
                    # Content may change during login; only a missing or retyped
                    # paired window fails, and only its own item.
                    try:
                        problems = preservation_problems(chrome, native)
                    except (TypeError, ValueError, KeyError, AttributeError):
                        reject("current native browser state is malformed")
                    for (profile_name, window_id), problem in sorted(
                            problems.items(), key=lambda item: (str(item[0][0]), str(item[0][1]))):
                        label = next((str(item.get("id") or "window") for owner_profile, item in selected
                                      if str(owner_profile.get("profile") or "Default") == profile_name
                                      and item.get("_reconcile_window_id") == window_id),
                                     f"window-{window_id}")
                        item_id = f"{profile_name}/{label}"
                        failures.append(f"{item_id}: {ACTION.format(reason=problem)}")
                        # Fail only this item's evidence so the category is classified per item.
                        failed = PhaseEvidence(EvidenceState.FAILED, problem, True)
                        index = next((i for i, item in enumerate(evidence) if item.item_id == item_id), None)
                        if index is None:
                            evidence.append(ProviderItemResult("chrome", item_id, placement=failed))
                        else:
                            if evidence[index].success:
                                completed -= 1  # Verified earlier, no longer bound.
                            evidence[index] = dataclass_replace(evidence[index], placement=failed)
                else:
                    _checked, final_evidence = reconcile(reconciliation_source, native)
                    if final_evidence["native_windows"] != reconciliation["native_windows"]:
                        reject("native window identities changed during verification")
            except (BrowserUnavailable, RuntimeError) as error:
                failures.append(str(error))
        for profile_name, label in unpaired_items:
            from .browser_reconciliation import ACTION
            reason = blocked_errors.get(profile_name) or reconciliation.get("unpaired_reasons", {}).get(profile_name)
            if reason is None:
                from .browser_reopen import _ineligible
                saved_window = next(item for owner_profile, item in browser_windows(original_chrome)
                                    if str(owner_profile.get("profile") or "Default") == profile_name
                                    and str(item.get("id")) == label)
                reason = ("no current native window pairs with its saved tabs unambiguously; "
                          + (_ineligible(saved_window) or "run `wsctl browser reopen` to open it"))
            failures.append(f"{profile_name}/{label}: " + ACTION.format(reason=reason))
            evidence.append(ProviderItemResult("chrome", f"{profile_name}/{label}",
                placement=PhaseEvidence(EvidenceState.FAILED, reason, True)))
        reconciliation["state"] = ("waiting-reuse-only" if failures and waiting_only(evidence) else
                                   "failed" if failures else "verified-reuse-only")
        reconciliation["verified_windows"] = completed
        reconciliation["failures"] = failures
        if owner is not None:
            authority_guard()
            atomic_json(_startup_directory() / "browser-reconciliation.json", reconciliation)
    if manual_navigation and not args.dry_run:
        for item in evidence:
            token = f" ({item.placement.request_id})" if item.placement.request_id else ""
            print(f"Chrome {item.item_id} · identity {item.identity.state.value} · "
                  f"content {item.content.state.value} · placement {item.placement.state.value}{token}")
        if evidence:
            print(f"Verified {completed}/{total_windows} Chrome window(s)")
    if failures:
        if manual_navigation and any(
            item.placement.state == EvidenceState.WAITING
            or any(phase.state == EvidenceState.FAILED for phase in (item.identity, item.content, item.placement))
            for item in evidence
        ):
            for failure in failures:
                print(f"Chrome {failure}", file=sys.stderr)
            return ProviderCount(completed, evidence)
        raise ProviderRestoreError(
            f"Restored {completed}/{total_windows} Chrome window(s); " + "; ".join(failures), evidence,
        )
    return ProviderCount(len(selected) if args.dry_run else completed, evidence)


def _targets(category: str | None) -> tuple[str, ...]:
    return (category,) if category else CATEGORIES


def _restore(snapshot: dict[str, Any], args: argparse.Namespace, *, startup: bool = False) -> dict[str, Any]:
    if startup and not args.dry_run:
        from . import operations
        owner = operations.current()
        if owner is not None and owner.mode == "startup":
            from .startup_control import check_abort
            check_abort(owner)
    if getattr(args, "maximize", False):
        if args.category != "vscode" or not args.workspace or args.no_place:
            raise ValueError("--maximize requires restore vscode --workspace with placement enabled")
        windows = [window for window in (snapshot.get("vscode") or {}).get("windows", [])
                   if window["placement"]["workspace_name"] == args.workspace]
        if len(windows) != 1:
            raise ValueError("--maximize requires exactly one saved editor window in the workspace")
        window, = windows
        snapshot = {**snapshot, "vscode": {**snapshot["vscode"], "windows": [
            {**window, "placement": {**window["placement"], "state": "maximized"}}]}}
    targets = _targets(args.category)
    if getattr(args, "deck_dashboard", None) is not None:
        if args.category != "terminals":
            raise ValueError("--deck-dashboard requires an explicit terminals category")
    elif any(getattr(args, option, None) is not None for option in ("deck_window", "deck_placeholder")):
        raise ValueError("--deck-window and --deck-placeholder require --deck-dashboard")
    switch_options = ({"workspace_switch": True, "excluded_windows": args.excluded_windows}
                      if getattr(args, "workspace_switch", False) else {})
    if args.select and "terminals" not in targets:
        raise ValueError("--select only applies to terminals")
    if args.session and "terminals" not in targets:
        raise ValueError("--session only applies to terminals")
    counts = {
        **{category: 0 for category in CATEGORIES},
        "codex_ready": 0,
        "codex_total": 0,
        "codex_verified": 1,
        "codex_deferred": 0,
        "legacy_retained": 0,
        "codex_inactive": 0,
        "codex_stopped": 0,
        "codex_note": "",
        "codex_alert": "",
    }
    if len(targets) > 1 and not args.session and not args.select:
        jobs = {}
        for category in targets:
            category_args = argparse.Namespace(**vars(args))
            category_args.category = category
            category_args._restore_all_categories = True
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
                counts["legacy_retained"] = result.get("legacy_retained", 0)
                for key in ("codex_inactive", "codex_stopped", "codex_note", "codex_alert"):
                    counts[key] = result.get(key, counts[key])
        if errors:
            raise RuntimeError("; ".join(errors))
        return counts
    if "terminals" in targets:
        outcome = _restore_terminals(snapshot, args)
        counts["terminals"] = ProviderCount(outcome.restored, outcome.results)
        counts["codex_ready"] = outcome.codex_ready
        counts["codex_total"] = outcome.codex_total
        counts["codex_verified"] = int(outcome.codex_verified)
        counts["codex_deferred"] = int(outcome.codex_deferred)
        counts["legacy_retained"] = outcome.legacy_retained
        counts["codex_inactive"] = int(outcome.codex_inactive)
        counts["codex_stopped"] = outcome.codex_stopped
        counts["codex_note"] = outcome.codex_note
        counts["codex_alert"] = outcome.codex_alert
    if "browsers" in targets and not args.session and not args.select:
        stale = None if args.dry_run else stale_recipe_message(snapshot)
        age_item = (ProviderItemResult(
            "browsers", RECIPE_AGE_ITEM, PhaseEvidence(EvidenceState.SKIPPED), PhaseEvidence(EvidenceState.SKIPPED),
            PhaseEvidence(EvidenceState.SKIPPED), attention=(stale,)) if stale else None)
        try:
            counts["browsers"] = _restore_browsers(snapshot, args, start_browser=startup)
        except ProviderRestoreError as error:
            if age_item is None:
                raise
            # A failed window must not hide that the recipe itself is old.
            raise ProviderRestoreError(str(error), (*error.results, age_item)) from error
        results = tuple(getattr(counts["browsers"], "results", ()))
        if age_item is not None and (int(counts["browsers"]) or results):
            # An old retained recipe restores faithfully but is not the desktop
            # that was last seen: advisory attention, never a blocked capture.
            print(f"Chrome recipe {stale.removeprefix(ADVISORY_PREFIX)}", file=sys.stderr)
            counts["browsers"] = ProviderCount(int(counts["browsers"]), (*results, age_item))
    for provider in providers.PROVIDERS:
        if provider.name in targets and not args.session and not args.select:
            def report(state: str, message: str, current: int, total: int, name=provider.name) -> None:
                if getattr(args, "login_status", False):
                    update_stage(name, state, message, current=current, total=total)
            counts[provider.name] = provider.restore(snapshot, args, report, switch_options)
    return counts


def _place_attached_terminal(snapshot: dict, args: argparse.Namespace) -> int:
    from . import tmux_server
    if (args.category != "terminals" or len(args.session or []) != 1 or not args.workspace
            or args.select or args.no_place or any(getattr(args, option, None) for option in (
                "deck_dashboard", "deck_window", "deck_placeholder", "maximize"))):
        raise ValueError("--terminal-placement-only requires one session and workspace with placement enabled")
    name, = args.session
    saved = [item for item in _filtered_terminal_items(snapshot, args)
             if item.get("launch_terminal") and item.get("placement")]
    if len(saved) != 1:
        raise CommandError("Expected one saved terminal placement for the requested session")
    with tmux_server.workspace_socket():
        live = _live_terminal_clients(deck_managed=True).get(name, [])
        if len(live) != 1 or not (live[0].get("placement") or {}).get("id") or not live[0].get("alacritty_pid"):
            raise CommandError("Expected one attached Alacritty window for the requested session")
        result = place_terminal(live[0], saved[0]["placement"], dry_run=args.dry_run, require_local=True)
    print(result.message)
    if result.request_id:
        print(f"Placement receipt: {result.request_id}")
    return 0 if result.success else 3


def cmd_restore(args: argparse.Namespace) -> int:
    from . import placement_guard
    from .workspace_switch import suppress
    with _startup_lock():
        path = getattr(args, "from_history", None)
        snapshot = load_history(path) if path is not None else load()
        snapshot = suppress(snapshot)
        if getattr(args, "terminal_placement_only", False):
            return _place_attached_terminal(snapshot, args)
        counts = _restore(snapshot, args)
        if not any(getattr(args, flag, False) for flag in ("dry_run", "no_place", "from_history", "maximize")):
            for category in placement_guard.CATEGORIES:
                results = getattr(counts[category], "results", ())
                if results:
                    placement_guard.record(_seen_directory(_checkpoint_login()), category, snapshot, results,
                                           workspace=getattr(args, "workspace", None), verified_only=True)
    pending = [item for category in CATEGORIES for item in getattr(counts[category], "results", ())
               if item.placement.state == EvidenceState.WAITING]
    if pending:
        raise ProviderRestoreError("Application content restored; GNOME still owns pending native placement", pending)
    attention = [item for category in CATEGORIES for item in getattr(counts[category], "results", ())
                 if any(phase.state == EvidenceState.FAILED
                        for phase in (item.identity, item.content, item.placement))
                 or (item.provider == "agent-deck" and item.attention)]
    if attention:
        # A manual restore reports unrestored items; startup records them as results.
        print(f"{len(attention)} item(s) need attention and stay pending", file=sys.stderr)
        return 3  # Like a partial save; 1 means that nothing was selected.
    if counts["codex_total"] and not counts["codex_verified"] and not args.dry_run:
        print(f"{counts['codex_ready']}/{counts['codex_total']} conversations verified; "
              "the remaining saved conversations stay pending", file=sys.stderr)
        return 3
    if args.category in {"social-apps", "file-manager", "vscode"}:
        return 0  # A deliberate background/stopped skip is successful reconciliation.
    if not counts["codex_total"] and not any(counts[category] for category in CATEGORIES):
        print("No matching windows or sessions selected.", file=sys.stderr)
        return 1
    return 0


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "current-boot"


def _login_generation_file() -> str | None:
    """The session id: the bus-name hash that operation contexts and the HUD share."""
    from .startup import login_identity
    return login_identity(runtime_dir())[0]


def _login_key_file(session_id: str | None = None) -> str | None:
    """The login key: separates logins that share a session id.

    A runtime without the key file has the session id as its key.
    """
    from .startup import login_key_file
    return login_key_file(runtime_dir()) or session_id or _login_generation_file()


def _startup_directory() -> Path:
    from .startup import startup_directory
    return startup_directory(runtime_dir(), _boot_id(), _login_key_file())


def _conversation_deadline() -> float:
    """Return this login's shared verification deadline, creating it on first use."""
    path = _startup_directory() / "conversation-deadline.json"
    now = time.monotonic()
    limit = now + CONVERSATION_DEADLINE_SECONDS
    for _attempt in range(2):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            value = float(document["deadline"])
            if document.get("boot_id") == _boot_id() and value == value:
                return min(value, limit)  # A damaged file never extends the bound.
            return now  # Written for another boot: nothing may extend this login.
        except (OSError, ValueError, KeyError, TypeError):
            pass
        temporary = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}")
        try:
            atomic_json(temporary, {"deadline": limit, "boot_id": _boot_id()})
            try:
                os.link(temporary, path)  # Atomic create: only the first user wins.
            except FileExistsError:
                continue
            return limit
        finally:
            temporary.unlink(missing_ok=True)
    return now


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

def _attention_message(results) -> str:
    """Say how many items need attention and why; real problems come before notes."""
    from .provider_progress import failure_summary
    counted = [result for result in results if result.item_id != RECIPE_AGE_ITEM]
    needing = sorted((result for result in results if not result.success or has_advisory(result.to_dict())),
                     key=lambda result: result.success)
    broken = [result for result in needing if result.item_id != RECIPE_AGE_ITEM]
    summary = failure_summary([result.to_dict() for result in needing], "see item details")
    return f"{len(broken)} of {len(counted)} item(s) need attention · {summary}" if broken else summary


def _publish_category_outcome(category: str, snapshot: dict[str, Any], *,
                              count=0, error: BaseException | None = None,
                              workspace: str | None = None, no_place: bool = False) -> str:
    results = tuple(getattr(error if error is not None else count, "results", ()))
    from .provider_results import ProviderRestoreError
    # Only item evidence that shows a problem can stand in for the exception.
    raised = error is not None and not (
        isinstance(error, ProviderRestoreError) and results
        and not all(result.success for result in results))
    if results:
        # The document derives the state from the same evidence; so does the caller.
        derived = stage_state([result.to_dict() for result in results],
                              provider_error=raised and not waiting_only(results))
        state, shown = derived.real, derived.shown
    else:
        # An exception without item evidence is the job's failure.
        state = "failed" if raised else "ready" if count else "skipped"
        shown = state
    # Advisory notes only add `degraded` to what is shown; the marker and the
    # errors follow the real state (see provider_results).
    message = (str(error) if state == "failed" and error is not None else
               "Waiting for compositor placement verification" if state == "waiting" else
               _attention_message(results) if shown in {"degraded", "failed"} and results else
               f"Verified {int(count)} {category} item(s)" if count else f"No saved {category} items")
    update_stage(category, shown, message, error=message if state == "failed" else None)
    from . import operations, placement_guard
    if category in placement_guard.CATEGORIES and not no_place:
        placement_guard.record(_seen_directory(_checkpoint_login()), category, snapshot, results,
                               context=operations.current(), workspace=workspace, failed=raised)
    publish_provider_results(category, results,
                             error=message if error is not None and state == "failed" else None)
    from .pending import restored_keys
    # A restored item that the user closes before the next checkpoint was
    # seen during this login, so it does not stay pending.
    _record_seen(restored_keys(category, snapshot, results))
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
            # The refresh below derives the aggregate from the category states.
            stage["provider_completion_pending"] = True
        else:
            stage.update(state="ready", message="Application workspace restoration verified", current=1, total=1)
        login_status._refresh_provider_placements(status)
    login_status._locked_update(mutate, mode="startup", allow_expired=True)

def _status_stages() -> dict[str, dict[str, Any]]:
    try:
        document = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {stage["id"]: stage for stage in document.get("stages", ())
            if isinstance(stage, dict) and isinstance(stage.get("id"), str)} if isinstance(document, dict) else {}


def _publish_workspace_restored() -> None:
    """Start the target that loads the cloud drives and runs the login finalizer."""
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl", "--user", "start", "--no-block",
                WORKSPACE_RESTORED_TARGET,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("could not publish completed workspace restore: systemctl timed out") from error
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


def _missing_in_session(session, actual_name) -> set[str]:
    """Saved conversations without a ready client in one tmux session."""
    if not any((pane.get('conversation') or {}).get('provider') == 'claude'
               for window in session.get('windows', []) for pane in window.get('panes', [])):
        return {'codex:' + value for value in missing_codex_ids(session, actual_name)}
    return missing_conversation_ids(session, actual_name)


def _missing_conversations(sessions) -> set[str]:
    """Return saved conversations that no live client holds.

    `sessions` pairs each saved session with the tmux session that restored it
    (an older build may have left a `NAME-wsctl` copy, which counts too). A
    ready client in that session satisfies the conversation wherever its pane
    is. A conversation that also runs in a different tmux session was restored
    there on purpose, as a plain shell. A client that is live in its own
    session but not ready (it is busy, or shows a prompt) satisfies it only
    when this login already saw it live; a client just started must be ready.
    """
    from .restore import _session_fingerprint, _tagged_restore_session
    per_session = []
    for session, name in sessions:
        names = {name}
        missing = _missing_in_session(session, name)
        if missing:
            # Only a session that is not satisfied looks for a copy of an older build.
            copy = _tagged_restore_session(session['name'], _session_fingerprint(session))
            if copy and copy != name:
                names.add(copy)
                missing &= _missing_in_session(session, copy)
        if missing:
            per_session.append((names, missing))
    if not per_session:
        return set()
    from . import conversation_guard
    from .pending import conversation_item, seen_keys
    live = conversation_guard.live_sessions()
    seen: set[str] | None = None

    def held(names, value):
        nonlocal seen
        provider, identity = value.split(':', 1)
        where = set(conversation_guard.matching(live, provider, identity))
        if where - names:
            return True
        if not where:
            return False
        if seen is None:
            seen = seen_keys(_seen_directory(_checkpoint_login()))
        return conversation_item((provider, identity)) in seen
    return {value for names, missing in per_session for value in missing if not held(names, value)}


def _attention_sessions() -> set[str]:
    """Saved tmux sessions whose restore result this login needs attention."""
    from .stage_state import stage_document
    try:
        document = json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {
        str(item.get("item_id")) for item in stage_document(document, "terminals").get("provider_results", ())
        if isinstance(item, dict) and item.get("provider") == "tmux"
        and isinstance(item.get("identity"), dict) and item["identity"].get("state") == "failed"
    }


def finish_deferred_codex() -> bool:
    """Finish verification or an authorized Agent Deck retry after mounts."""
    with _startup_lock():
        return _finish_deferred_codex()


def _finish_deferred_codex() -> bool:
    try:
        with status_path().open(encoding="utf-8") as stream:
            status = json.load(stream)
    except (OSError, ValueError):
        status = {}
    stage = next((item for item in status.get("stages", [])
                  if isinstance(item, dict) and item.get("id") == "codex"), {}) if isinstance(status, dict) else {}
    if stage.get("state") == "failed":
        return False
    from .workspace_switch import suppress
    snapshot = suppress(load())
    from . import agent_deck, agent_deck_restore
    if "agent_deck" in snapshot or agent_deck.enabled():
        if stage.get("state") != "waiting":
            return stage.get("state") in {"ready", "skipped"}
        from .pending import deck_item, seen_keys
        owner = _marker_context()
        try:
            receipt = json.loads((_startup_directory() / DECK_RETRY_FILE).read_text())
            if not isinstance(receipt, dict) or owner is None or receipt.get("operation") != owner.to_dict():
                raise ValueError("deferred Agent Deck restore no longer owns this login")
            agent_deck.validate(receipt["agent_deck"])
            already_seen = seen_keys(_seen_directory(_checkpoint_login()))
            requested = receipt["agent_deck"]["sessions"]
            ready, total = receipt["ready"], receipt["total"]
            legacy_retained = receipt.get("legacy_retained", 0)
            if (type(ready) is not int or type(total) is not int or not 0 <= ready <= total - len(requested)
                    or type(legacy_retained) is not int or legacy_retained < 0):
                raise ValueError("invalid deferred Agent Deck counts")
            if snapshot.get("_workspace_switch_exclusions"):
                allowed = {deck_item(row) for row in (snapshot.get("agent_deck") or {}).get("sessions", [])}
                kept = [row for row in requested if deck_item(row) in allowed]
                total -= len(requested) - len(kept)
                requested = kept
            remaining = [row for row in requested if deck_item(row) not in already_seen]
            verified = ready + len(requested) - len(remaining)
        except (OSError, TypeError, KeyError, ValueError) as error:
            update_stage("codex", "degraded", f"Agent Deck retry unavailable: {error}")
            return False
        result = agent_deck_restore.restore(
            {"agent_deck": {"version": 1, "sessions": remaining}}, [],
            deadline=_conversation_deadline(), start=agent_deck_restore.start_setting()[0],
        )
        _record_deck_restore(result)
        ready = verified + len(result.ready)
        complete = ready == total and result.verified
        update_stage(
            "codex", "ready" if complete else "degraded",
            f"{ready}/{total} Agent Deck conversations verified"
            + (f"; {result.detail or 'remaining conversations stay pending'}" if not complete else "")
            + (". " + _native_migration_note(legacy_retained) if legacy_retained else ""),
            current=ready, total=total,
        )
        return complete
    unidentified = _unidentified_codex_panes(snapshot.get("sessions", []))
    attention = _attention_sessions()

    def identities(items):
        return {
            conversation_key(found)
            for session in items
            for window in session.get("windows", [])
            for pane in window.get("panes", [])
            if (found := conversation_identity(pane)) is not None
        }
    sessions = {session["name"]: session for session in snapshot.get("sessions", [])
                if session["name"] not in attention}
    expected = identities(sessions.values())
    # Sessions that need attention were not restored; their conversations stay
    # pending and are never waited for.
    held = identities(session for session in snapshot.get("sessions", [])
                      if session["name"] in attention) - expected
    suffix = (f"; {len(held)} conversation(s) remain pending in sessions that need attention"
              if held else "")
    providers = {value.split(":", 1)[0] for value in expected}
    label = "Codex" if providers == {"codex"} else "Claude" if providers == {"claude"} else "AI"
    deadline = _conversation_deadline()
    may_wait = (bool(expected & {"codex:" + value for value in pending_start_ids()})
                or any(value.startswith("claude:") for value in expected))
    stable_since: float | None = None
    first_check = True
    published_waiting = ""
    while True:
        missing = _missing_conversations(
            ((session, name) for name, session in sessions.items())) & expected
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
        if stable or not may_wait or (missing and now >= deadline):
            break
        waiting_message = (
            f"{len(expected) - len(missing)}/{len(expected)} {label} conversation(s) verified; "
            "verification continues until the login deadline"
        )
        if missing and waiting_message != published_waiting:
            published_waiting = waiting_message
            update_stage("codex", "waiting", waiting_message,
                         current=len(expected) - len(missing), total=len(expected) + len(unidentified))
        time.sleep(0.5)
    ready = len(expected) - len(missing)
    update_stage(
        "codex",
        "degraded" if not stable or unidentified or held else "ready" if expected else "skipped",
        (
            _unidentified_codex_message(unidentified)
            if unidentified else
            f"{ready}/{len(expected)} {label} conversation(s) verified after cloud drives; "
            "check the remaining tmux panes for startup errors or prompts" + suffix
            if missing else
            "Conversation stability verification timed out; check the restored tmux panes" + suffix
            if not stable else
            f"Resumed {len(expected)} {label} conversation(s)" + suffix if expected else
            f"No conversation was restored{suffix}" if held else
            "No saved AI conversations"
        ),
        current=ready, total=len(expected) + len(held) + len(unidentified),
    )
    from .pending import conversation_item
    _record_seen({conversation_item(tuple(value.split(":", 1))) for value in expected - missing})
    return stable and not unidentified and not held


def _finish_deferred(provider: providers.Provider) -> bool:
    """Restore a storage-backed recipe that startup deferred until the cloud mounts started."""
    name, nouns = provider.name, provider.noun + "s"
    with _startup_lock():
        if not stage_deferred(_status_stages().get(name)):
            return not_failed(_status_stages().get(name))
        # A corrupt/missing checkpoint must stay visible; finalization catches
        # this failure and still warms drives. Do not silently mark it ready.
        from .workspace_switch import suppress
        snapshot = suppress(load())
        update_stage(name, "running", f"Restoring {nouns} after cloud drives mounted")
        args = argparse.Namespace(
            category=name, workspace=None, session=None, select=False,
            dry_run=False, no_place=False, login_status=True, wait=30,
            force=False,
            verify_codex=False,
        )
        try:
            counts = _restore(snapshot, args, startup=True)
        except (BrowserUnavailable, FileNotFoundError, ValueError, RuntimeError) as error:
            state = _publish_category_outcome(name, snapshot, error=error)
            return state != "failed"
        outcome = counts.get(name, 0)
        state = _publish_category_outcome(name, snapshot, count=outcome)
        if state in {"waiting", "failed"}:
            return state != "failed"
        count = int(outcome)
        update_stage(name, "ready" if count else "skipped",
                     f"Restored {count} {provider.noun}(s)" if count else f"No saved {nouns}",
                     current=count, total=count)
        return True


def finish_deferred_file_manager() -> bool:
    """Restore a storage-backed file-manager recipe after cloud mounts start."""
    return _finish_deferred(providers.BY_NAME["file-manager"])


def finish_deferred_vscode() -> bool:
    """Restore VS Code workspaces deferred until storage mounts are ready."""
    return _finish_deferred(providers.BY_NAME["vscode"])


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


def cmd_startup(args: argparse.Namespace) -> int:
    from .workspace_switch import suppress
    if args.dry_run:
        dry_counts = _restore(suppress(load()), args, startup=True)
        return int(not any(dry_counts[category] for category in CATEGORIES))
    set_overall("running", "Restoring saved workspace state")
    # tmux sessions are rebuilt by the terminal restore below; this stage has
    # no work of its own and must not claim completion before any work.
    update_stage("tmux", "skipped", "Part of terminal restore")
    with _startup_lock():
        snapshot = suppress(load())
        record_restored_snapshot(snapshot.get("created_at"))
        targets = _targets(args.category)
        stages = _status_stages()
        # A repeated startup reconciles: each category finds what is live and
        # restores what is missing. A category whose placements the compositor
        # still owns is left alone, so no second request is queued.
        pending = [category for category in targets if not placement_pending(stages.get(category))]
        if "browsers" in pending and (stages.get("browsers") or {}).get("provider_results"):
            # Chrome owns its tabs and this operation already paired and placed
            # its windows; a failed item is retried by `wsctl restore browsers`.
            pending.remove("browsers")
        # The login finalizer mounts the drives and then restores a storage
        # category that startup left `waiting` with no results. Before it
        # starts a repeat defers such a category again; while it runs the
        # category is the finalizer's and a repeat leaves it as it is; after
        # it ended a repeat restores the category directly.
        finalization = stages.get("login-finalization", {}).get("state")
        deferred = set()
        if args.category is None:
            for provider in providers.PROVIDERS:
                if provider.needs_storage is None:
                    continue
                category, nouns = provider.name, provider.noun + "s"
                needs = partial(provider.needs_storage, snapshot.get(provider.snapshot_key))
                if category not in pending or finalization in TERMINAL_STATES or not needs():
                    continue
                pending.remove(category)
                if finalization in (None, "pending"):
                    deferred.add(category)
                    update_stage(category, "waiting", f"Waiting for cloud drives before restoring {nouns}")
        if not pending and not deferred:
            _publish_workspace_attempt_completion()
            if args.category is None:
                _publish_workspace_restored()
            print("Nothing to restore now: every remaining category waits for compositor "
                  "placements or the cloud drives, or has its Chrome windows already restored.")
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
        jobs = {}
        for category in pending:
            category_args = argparse.Namespace(**vars(args))
            category_args.category = category
            category_args.login_status = True
            jobs[category] = partial(_restore, snapshot, category_args, startup=True)
        for category, counts, error in completed_jobs(jobs):
            if error is not None:
                state = _publish_category_outcome(category, snapshot, error=error,
                                                  workspace=getattr(args, "workspace", None),
                                                  no_place=getattr(args, "no_place", False))
                if state == "failed":
                    if category == "terminals":
                        update_stage("codex", "failed", "Terminal restoration did not complete", error=str(error))
                # Both waiting and failed attempts prevent automatic relaunch.
                continue
            state = _publish_category_outcome(category, snapshot, count=counts[category],
                                              workspace=getattr(args, "workspace", None),
                                              no_place=getattr(args, "no_place", False))
            if state in {"waiting", "failed", "degraded"}:
                if category != "terminals":
                    continue
            if counts[category]:
                print(f"Startup restored {counts[category]} {category} item(s).")
            elif failed_items := sum(1 for item in getattr(counts[category], "results", ()) if not item.success):
                print(f"Startup restored no {category} items; {failed_items} failed.")
            else:
                print(f"Startup has no saved {category} items.")
            if category == "terminals":
                terminal_items = _filtered_terminal_items(snapshot, args)
                alacritty_total = sum(
                    bool(item.get("launch_terminal", True))
                    for item in terminal_items
                )
                tmux_total = len({str(item.get("name")) for item in terminal_items})
                if state not in {"waiting", "failed", "degraded"}:
                    update_stage(
                        "terminals", "ready" if counts[category] else "skipped",
                        (
                            f"Restored {alacritty_total} Alacritty window(s) and "
                            f"verified {tmux_total} tmux session(s)"
                        ) if counts[category] else "No saved terminal sessions",
                        current=counts[category], total=counts[category],
                    )
                codex_total = counts.get("codex_total", 0)
                codex_ready = counts.get("codex_ready", codex_total)
                legacy_retained = counts.get("legacy_retained", 0)
                codex_verified = bool(counts.get("codex_verified", 1))
                from . import agent_deck
                deck_managed = "agent_deck" in snapshot or agent_deck.enabled()
                unidentified = [] if deck_managed else _unidentified_codex_panes(terminal_items)
                inactive = bool(counts.get("codex_inactive")) and deck_managed
                alert = counts.get("codex_alert", "")
                codex_note = counts.get("codex_note", "")
                codex_state = (
                    ("degraded" if alert or not codex_verified else
                     "ready" if codex_total or codex_note else "skipped")
                    if inactive else
                    "skipped" if not codex_total else
                    "ready" if codex_verified else
                    "degraded" if unidentified else
                    "waiting" if counts.get("codex_deferred", 0) else
                    "degraded"
                )
                update_stage(
                    "codex", codex_state,
                    (
                        _unidentified_codex_message(unidentified)
                        if unidentified else
                        (f"{codex_ready} Agent Deck conversations running; "
                         f"{counts.get('codex_stopped', 0)} not running (start them from Agent Deck)"
                         if codex_total else "No saved open Agent Deck conversations")
                        + codex_note + (f"; {alert}" if alert else "")
                        if inactive else
                        f"Resumed {codex_total} AI conversation(s)"
                        if codex_state == "ready" else
                        (
                            f"{codex_ready}/{codex_total} AI conversation(s) verified; "
                            "verification continues until the login deadline"
                        )
                        if codex_state == "waiting" else
                        (
                            f"{codex_ready}/{codex_total} AI conversation(s) verified; "
                            + ("check Agent Deck for saved conversations that still need attention"
                               if deck_managed else
                               "check the remaining tmux panes for startup errors or prompts")
                        )
                        if codex_state == "degraded" else
                        "No saved open Agent Deck conversations" if deck_managed else
                        "No saved AI conversations"
                    ) + (". " + _native_migration_note(legacy_retained) if legacy_retained else ""),
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
        _publish_workspace_attempt_completion()
        set_overall("running", "Workspace restored; loading cloud systems")
        try:
            if args.category is None:
                _publish_workspace_restored()
        except RuntimeError as error:
            update_stage("workspace", "failed", str(error), error=str(error))
            raise
    return 0


def _autosave_from_tmux(*, allow_unresolved_codex: bool = False) -> tuple[Path | None, list[str]]:
    """Save only the terminal category from a tmux hook."""
    with state_lock():
        from . import pending
        try:
            previous = load()
        except FileNotFoundError:
            previous = {}
        snapshot = capture()
        held = pending.keep_unchanged_clients(snapshot, previous, carry_identity=False)
        problems = _terminal_problems(snapshot)
        monitor_problem = _fallback_monitor_problem(snapshot, previous)
        if monitor_problem:
            problems.append(monitor_problem)
        if previous.get("sessions") and not snapshot.get("sessions"):
            problems.append("tmux capture unexpectedly contains no sessions")
        unsafe = [problem for problem in problems if not problem.endswith(UNRESOLVED_PROBLEM)]
        if problems and (not allow_unresolved_codex or unsafe):
            return None, problems

        prior_chrome = _browser_state(previous)
        candidate = copy.deepcopy(snapshot)
        # Continuum is a terminal autosave. Browser state is checkpointed by
        # explicit/full saves (including GNOME end-session), not by a periodic
        # hook which may run while Chrome's own startup restoration is partial.
        # Keeping the prior category also prevents temporary or diagnostic
        # Chrome windows from replacing the durable browser recipe.
        if prior_chrome:
            _set_browser_state(candidate, copy.deepcopy(prior_chrome))
        # Terminal-only autosave must never erase the social visibility recipe.
        if "social_apps" in previous:
            candidate["social_apps"] = copy.deepcopy(previous["social_apps"])
        if "file_manager" in previous:
            candidate["file_manager"] = copy.deepcopy(previous["file_manager"])
        if "vscode" in previous:
            candidate["vscode"] = copy.deepcopy(previous["vscode"])
        login = _checkpoint_login()
        carried_dashboards: list[str] = []
        _carry_pending(candidate, previous, {"terminals"}, login=login, held=held,
                       dashboards=carried_dashboards)
        _report_carried_dashboards(carried_dashboards)
        from .checkpoint import record_provenance
        record_provenance(candidate, previous, source="terminal-autosave",
                          owner=login, problems={"terminals": problems})
        return save(candidate), problems


def cmd_tmux_save(_args: argparse.Namespace) -> int:
    """Compatibility hook: older tmux configurations call it after a save.

    wsctl alone owns the tmux layout, so the hook only runs the terminal
    checkpoint and never lets a failure reach the calling tmux plugin.
    """
    try:
        _auto_checkpoint(("terminals",))
    except Exception as error:
        print(f"wsctl: terminal checkpoint failed: {error}", file=sys.stderr)
    return 0


def cmd_tmux_obsolete(_args: argparse.Namespace) -> int:
    """Compatibility shim for hooks of older tmux configurations."""
    print("wsctl: tmux-resurrect hooks are obsolete; wsctl restores tmux itself", file=sys.stderr)
    return 0


def _tmux_config_path(config: str | None = None) -> Path:
    if config:
        path = Path(config).expanduser()
    else:
        config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        candidate = config_home / "tmux/tmux.conf"
        path = candidate if candidate.exists() or candidate.is_symlink() else Path.home() / ".tmux.conf"
    return path.resolve() if path.is_symlink() else path


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
        if line.strip() in {directive, f"set -g @resurrect-processes '{TMUX_LEGACY_PROCESS_MAPPING}'"}
    ]
    if not managed:
        return False
    for index in reversed(managed):
        del lines[index]
        if (
            index > 0
            and lines[index - 1]
            in {"# Keep restored Codex panes alive as shells when a resume exits.",
                "# Keep restored conversation panes alive as shells when a resume exits."}
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


def cmd_tmux_configure(_args: argparse.Namespace) -> int:
    print("wsctl no longer edits the tmux configuration; "
          "`wsctl tmux unconfigure` removes the mapping older releases wrote")
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
    if current.returncode == 0 and current.stdout.strip() in {TMUX_CODEX_PROCESS_MAPPING, TMUX_LEGACY_PROCESS_MAPPING}:
        subprocess.run(
            ["tmux", "set-option", "-gu", "@resurrect-processes"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(f"tmux conversation restore mapping {'removed' if changed else 'not present'}: {path}")
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
    from .workspace_switch import add_parser as add_workspace_parser
    add_workspace_parser(sub)
    from .browser_reopen import add_parser as add_browser_parser
    add_browser_parser(sub)

    save_parser = sub.add_parser("save", help="replace the saved workspace state")
    save_parser.add_argument(
        "--allow-partial", action="store_true",
        help="save even when a companion, placement, or Codex ID is unavailable",
    )
    save_parser.add_argument(
        "--auto", action="store_true",
        help="periodic checkpoint: keep the saved Chrome recipe and skip unsafe moments",
    )
    save_parser.add_argument(
        "--final", action="store_true",
        help="with --auto: the bounded final save at shutdown; waits for a running capture",
    )
    save_parser.add_argument(
        "--verify-idle-codex", action="store_true",
        help="verify unresolved IDs with /status in idle empty native Codex composers",
    )
    save_parser.set_defaults(func=cmd_save)

    show_parser = sub.add_parser("show", help="show the saved state grouped by workspace")
    show_parser.add_argument("--json", action="store_true")
    show_parser.add_argument("--details", action="store_true", help="list tmux and browser window names")
    show_parser.set_defaults(func=cmd_show)

    forget_parser = sub.add_parser("forget", help="drop pending saved items that should not be restored")
    forget_parser.add_argument("keys", nargs="+", metavar="KEY", help="a pending item key from 'wsctl show'")
    forget_parser.set_defaults(func=cmd_forget)

    restore_parser = sub.add_parser("restore", help="restore all state or one category")
    restore_parser.add_argument("category", nargs="?", choices=CATEGORIES)
    restore_parser.add_argument("--terminal-placement-only", action="store_true",
                                help="place an attached terminal without reconciling its tmux layout")
    restore_parser.add_argument("--maximize", action="store_true",
                                help="maximize the single saved VS Code window in the selected workspace")
    restore_parser.add_argument("--from-history", type=Path, metavar="GENERATION",
                                help="use a verified recovery/history generation for this restore")
    restore_parser.add_argument("--workspace", help="restore one named workspace")
    restore_parser.add_argument("--session", action="append", help="restore one tmux session; repeatable")
    restore_parser.add_argument("--select", action="store_true", help="choose terminal sessions with fzf")
    restore_parser.add_argument("--deck-dashboard", nargs=2, metavar=("PROFILE", "GROUP"),
                                help="open only this Agent Deck dashboard in one saved terminal")
    restore_parser.add_argument("--deck-window", help="exact new dashboard window name (default: Agent Deck)")
    restore_parser.add_argument("--deck-placeholder",
                                help="select the dashboard when this saved window is an empty background shell")
    restore_parser.add_argument("--dry-run", action="store_true")
    restore_parser.add_argument("--no-place", action="store_true", help="skip GNOME window placement")
    restore_parser.set_defaults(
        func=cmd_restore,
        verify_codex=False,
    )

    startup_parser = sub.add_parser("startup", help="restore once per login and start missing applications")
    startup_parser.add_argument("category", nargs="?", choices=CATEGORIES)
    startup_parser.add_argument("--workspace", help=argparse.SUPPRESS)
    startup_parser.add_argument("--session", action="append", help=argparse.SUPPRESS)
    startup_parser.add_argument("--select", action="store_true", help=argparse.SUPPRESS)
    startup_parser.add_argument("--dry-run", action="store_true")
    startup_parser.add_argument("--no-place", action="store_true", help="skip GNOME window placement")
    startup_parser.add_argument("--force", action="store_true", help=argparse.SUPPRESS)  # obsolete, ignored
    startup_parser.add_argument("--wait", type=float, default=15, help="seconds to wait for GNOME (default: 15)")
    startup_parser.add_argument("--await-tmux", action="store_true", help=argparse.SUPPRESS)  # obsolete, ignored
    startup_parser.set_defaults(
        func=cmd_startup,
        verify_codex=True,
    )

    tmux_parser = sub.add_parser("tmux", help="compatibility hooks for older tmux configurations")
    tmux_sub = tmux_parser.add_subparsers(dest="tmux_command", required=True)
    for name in ("begin", "restore", "end", "save-allowed", "contract"):
        shim = tmux_sub.add_parser(name, help=argparse.SUPPRESS)
        shim.add_argument("arguments", nargs="*", help=argparse.SUPPRESS)
        shim.add_argument("--wait", type=float, help=argparse.SUPPRESS)
        shim.set_defaults(func=cmd_tmux_obsolete)
    tmux_save = tmux_sub.add_parser("save", help="run the terminal checkpoint (compatibility hook)")
    tmux_save.add_argument("state_file", nargs="?", help=argparse.SUPPRESS)
    tmux_save.set_defaults(func=cmd_tmux_save)
    tmux_configure = tmux_sub.add_parser("configure", help="obsolete: never edits the tmux configuration")
    tmux_configure.add_argument("--config", help=argparse.SUPPRESS)
    tmux_configure.set_defaults(func=cmd_tmux_configure)
    tmux_unconfigure = tmux_sub.add_parser(
        "unconfigure", help="remove the mapping older releases wrote into the tmux configuration"
    )
    tmux_unconfigure.add_argument("--config", help=argparse.SUPPRESS)
    tmux_unconfigure.set_defaults(func=cmd_tmux_unconfigure)

    from .deployment import add_parser as add_deployment_parser
    from .provider_progress import cmd_placement_progress
    add_deployment_parser(sub)
    progress = sub.add_parser("placement-progress", help=argparse.SUPPRESS)
    progress.set_defaults(func=cmd_placement_progress)
    return result


def _finished_startup() -> str | None:
    """Why a repeated startup is refused: this login's operation already ended."""
    from . import operations
    try:
        document = json.loads(status_path().read_text(encoding="utf-8"))
        context = operations.OperationContext.from_dict(document.get("operation_context"))
    except (OSError, ValueError, TypeError):
        return None
    if document.get("mode") != "startup" or context.boot_id != operations.boot_id():
        return None
    if document.get("operation_state") != "running" or context.remaining() <= 0:
        return ("the startup of this login has finished and cannot be repeated; "
                "restore by hand with `wsctl restore`")
    return None


def main(argv: list[str] | None = None) -> int:
    args: argparse.Namespace | None = None
    try:
        args = parser().parse_args(argv)
        from . import operations
        if args.command == "tmux":
            # Compatibility hooks never join or judge an operation, even when
            # the inherited context is damaged.
            return int(args.func(args))
        inherited = operations.current()
        if inherited is not None:
            operations.context_from_status(status_path(), inherited.mode, inherited.operation_id,
                                           allow_expired=args.command == "placement-progress")
        elif args.command == "startup" or getattr(args, "login_status", False):
            if args.command == "startup" and not args.dry_run and (message := _finished_startup()):
                # A refusal changes nothing, so the status stays as it is.
                print(f"wsctl: {message}", file=sys.stderr)
                return 2
            operations.context_from_status(status_path(), "startup")
        return int(args.func(args))
    except (BrowserUnavailable, OSError, ValueError, RuntimeError) as error:
        if args is not None and getattr(args, "command", None) == "startup":
            fail_active(str(error))
        if getattr(args, "command", None) == "restore" and isinstance(error, ProviderRestoreError):
            for item in error.results:
                receipt = item.placement.request_id
                if receipt and receipt.startswith("native-handoff:"):
                    print(f"{item.provider}/{item.item_id} placement receipt: "
                          f"{receipt.removeprefix('native-handoff:')}", file=sys.stderr)
        print(f"wsctl: {error}", file=sys.stderr)
        return 2
