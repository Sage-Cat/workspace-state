"""`wsctl browser reopen`: an explicit rebuild of saved Chrome windows that are gone.

Startup never creates a Chrome window. This command, which only a user starts,
opens a saved regular window from its recorded tabs and places it. It refuses
while a restore runs, when Chrome is not running, when the saved window is
grouped, or when a live window might be the one that is missing. It writes
neither the checkpoint nor the login status document.
"""
from __future__ import annotations

import argparse
import copy
import fcntl
import os
import sys
from contextlib import contextmanager
from typing import Any, Iterator

from . import snapshot_view
from .provider_results import EvidenceState, PhaseEvidence, ProviderItemResult

GROUPED = "Open it in Chrome; grouped windows are not reopened automatically"


class ReopenRefused(RuntimeError):
    """Nothing was changed."""


@contextmanager
def _restore_lock() -> Iterator[None]:
    from . import cli
    descriptor = os.open(cli._startup_directory() / "restore.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReopenRefused("A restore is running; retry when it has finished. Nothing was changed.") from error
        yield
    finally:
        os.close(descriptor)


def _ineligible(window: dict[str, Any]) -> str | None:
    if window.get("groups") or any(tab.get("group") is not None for tab in window.get("tabs", [])):
        return GROUPED
    if window.get("type") != "normal" or window.get("incognito") is not False:
        return "Only regular windows are reopened"
    if not window.get("tabs") or any(not tab.get("url") for tab in window["tabs"]):
        return "The saved window has no reopenable tabs"
    return None


def _read_native(profiles: list[str]) -> dict[str, dict[str, Any]]:
    from . import cli
    return {name: cli.request_browser("capture", {"runtime_identity": True}, profile=name, timeout=2)
            for name in profiles}


def plan(snapshot: dict[str, Any], args: argparse.Namespace, native: dict[str, dict[str, Any]],
         excluded: frozenset[str] = frozenset()):
    """Return the selected saved windows with their pairing and the reconciled catalog."""
    from . import cli
    from .browser import browser_windows
    from .browser_reconciliation import ReconciliationRequired, reconcile_navigation
    if "latest_observation" in snapshot_view._browser_state(snapshot):
        # A retained observation is evidence for startup only; here the recipe is an ordinary one.
        snapshot = copy.deepcopy(snapshot)
        snapshot_view._browser_state(snapshot).pop("latest_observation")
    chrome = snapshot_view._browser_state(snapshot)
    saved = [(str(profile.get("profile") or "Default"), str(window.get("id")), window)
             for profile, window in browser_windows(chrome)]
    saved = [item for item in saved if item[0] not in excluded]
    if args.profile:
        saved = [item for item in saved if item[0] == args.profile]
    if args.window:
        wanted = set(args.window)
        unknown = sorted(wanted - {label for _name, label, _window in saved})
        if unknown:
            raise ReopenRefused("No saved Chrome window named " + ", ".join(unknown))
        counts = {label: sum(1 for item in saved if item[1] == label) for label in wanted}
        if any(count > 1 for count in counts.values()):
            raise ReopenRefused("Several profiles have a window with that name; add --profile")
        saved = [item for item in saved if item[1] in wanted]
    if not saved:
        raise ReopenRefused("No saved Chrome windows match")
    try:
        matched = reconcile_navigation(snapshot, native, allow_elimination=False)
    except ReconciliationRequired as error:
        raise ReopenRefused(str(error)) from error
    catalog, receipt = matched
    unpaired = {(name, label) for name, labels in receipt["unpaired_windows"].items() for label in labels}
    return saved, unpaired, catalog, receipt


def reopen(args: argparse.Namespace) -> int:
    from . import cli
    from .browser import BROWSER_PROTOCOL_VERSION, BROWSER_REQUIRED_CAPABILITIES, BrowserUnavailable
    from .browser_reconciliation import can_recreate_ungrouped, same_native_inventory
    from .provider_progress import observe_manual_placements
    from .storage import load
    from .workspace_switch import suppress

    snapshot = suppress(load())
    chrome = snapshot_view._browser_state(snapshot)
    saved_profiles = sorted({str(item.get("profile") or "Default") for item in chrome.get("profiles", [])})
    blocked = frozenset(snapshot.get("_workspace_switch_blocked_profiles", ()))
    if args.profile in blocked:
        raise ReopenRefused(f"Finish the interrupted workspace switch before reopening windows in profile "
                            f"{args.profile!r}. Nothing was changed.")
    wanted_profiles = [args.profile] if args.profile else [name for name in saved_profiles if name not in blocked]
    connected = set(cli.connected_profiles())
    for name in wanted_profiles:
        if name not in connected:
            raise ReopenRefused(f"Chrome is not running for profile {name!r}. Nothing was started: open Chrome "
                                "(it restores its own windows), then run this command again.")
    for name in wanted_profiles:
        info = cli.browser_companion_info(name)
        capabilities = set(info.get("capabilities", []))
        if (int(info.get("protocol_version") or 0) < BROWSER_PROTOCOL_VERSION
                or not (BROWSER_REQUIRED_CAPABILITIES | {"recreate_window"}).issubset(capabilities)):
            raise ReopenRefused(f"The Chrome companion for profile {name!r} is outdated; "
                                "reload the Workspace State Companion")
    token_prefix = cli._browser_restore_token_prefix(snapshot)
    evidence: list[ProviderItemResult] = []
    failures: list[str] = []
    def selected(name: str) -> bool:
        if args.profile and args.profile != name:
            return False
        return not args.window or any(
            str(window.get("id")) in args.window
            for entry in chrome.get("profiles", []) if str(entry.get("profile") or "Default") == name
            for window in entry.get("windows", []))

    for name in sorted(blocked.intersection(saved_profiles)):
        if not selected(name):
            continue
        failures.append(f"profile {name}: finish the interrupted workspace switch first")
        print(f"Chrome profile {name} was skipped: finish the interrupted workspace switch first", file=sys.stderr)
    handled: set[tuple[str, str]] = set()
    while True:
        try:
            native = _read_native(sorted(connected))
            saved, unpaired, catalog, receipt = plan(snapshot, args, native, blocked)
        except (ReopenRefused, BrowserUnavailable, RuntimeError) as error:
            if not handled:
                raise
            # An earlier window may already have been opened: report it, then stop.
            failures.append(f"the remaining windows were not reopened: {error}")
            print(f"Chrome windows were not reopened: {error}", file=sys.stderr)
            break
        if not handled and args.window:
            for name, label, _window in saved:
                if (name, label) not in unpaired:
                    print(f"Chrome {name}/{label} is already open in native window "
                          f"{receipt['native_windows'][name][label]}")
        pending = [item for item in saved if (item[0], item[1]) in unpaired and (item[0], item[1]) not in handled]
        if not pending:
            break
        name, label, window = pending[0]
        handled.add((name, label))
        item_id = f"{name}/{label}"
        reason = _ineligible(window)
        if reason is None and not can_recreate_ungrouped(window, receipt["native_windows"], native):
            reason = ("It overlaps a live window, or a live window is not paired with a saved one, so it may "
                      "already be open")
        if reason is not None:
            failures.append(f"{item_id}: {reason}")
            evidence.append(ProviderItemResult("chrome", item_id,
                                               placement=PhaseEvidence(EvidenceState.FAILED, reason, True)))
            print(f"Chrome {item_id} was not reopened: {reason}", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"would reopen Chrome {item_id} ({len(window['tabs'])} tabs)")
            continue
        try:
            for live_name, inventory in sorted(native.items()):
                live = cli.request_browser("capture", {"runtime_identity": True}, profile=live_name, timeout=2)
                if not same_native_inventory(inventory, live):
                    raise BrowserUnavailable("The live Chrome inventory changed; no window was opened")
            status = cli.request_browser("restore_status", {"restore_token": f"{token_prefix}:{name}:{label}",
                                                            "window": window}, profile=name, timeout=2)
            if not isinstance(status, dict) or status.get("exists") is not False:
                raise BrowserUnavailable("The saved window still holds a live claim; no window was opened")
            catalog_copy = copy.deepcopy(catalog)
            entry = next((item for item in catalog_copy["profiles"] if item["profile"] == name), None)
            if entry is None:
                entry = {key: value for key, value in next(
                    item for item in chrome["profiles"] if str(item.get("profile") or "Default") == name).items()
                    if key != "windows"}
                entry["windows"] = []
                catalog_copy["profiles"].append(entry)
            entry["windows"].append(window)
            profile_entry = next(item for item in chrome["profiles"] if str(item.get("profile") or "Default") == name)
            one_window = {**chrome, "profiles": [{**profile_entry, "windows": [window]}]}
            results = cli.restore_browser(one_window, restore_token_prefix=token_prefix,
                                          restore_catalog=catalog_copy, recreate_window=True)
        except (BrowserUnavailable, RuntimeError) as error:
            failures.append(f"{item_id}: {error}")
            evidence.append(ProviderItemResult("chrome", item_id,
                                               placement=PhaseEvidence(EvidenceState.FAILED, str(error), True)))
            print(f"Chrome {item_id} was not reopened: {error}", file=sys.stderr)
            continue
        for result in results:
            print(result.message)
            if result.evidence is not None:
                evidence.append(result.evidence)
            waiting = result.evidence is not None and result.evidence.placement.state == EvidenceState.WAITING
            if not result.success and not waiting:  # A handoff the compositor owns is observed below.
                failures.append(result.message)
    if args.dry_run:
        if not handled and not failures:
            print("No saved Chrome window needs to be reopened.")
        return 3 if failures else 0
    observed = observe_manual_placements(evidence)
    for before, after in zip(evidence, observed):
        if before.placement.state == EvidenceState.WAITING and after.placement.state == EvidenceState.FAILED:
            failures.append(f"{after.item_id}: {after.placement.detail}")
    for item in observed:
        print(f"Chrome {item.item_id} · identity {item.identity.state.value} · content {item.content.state.value}"
              f" · placement {item.placement.state.value}")
    if not handled and not failures:
        print("No saved Chrome window needs to be reopened.")
        return 0
    for failure in failures:
        print(f"Chrome {failure}", file=sys.stderr)
    return 3 if failures else 0


def cmd_browser(args: argparse.Namespace) -> int:
    try:
        with _restore_lock():
            return reopen(args)
    except ReopenRefused as error:
        print(str(error), file=sys.stderr)
        return 1


def add_parser(sub) -> None:
    parser = sub.add_parser("browser", help="explicit Chrome window commands")
    commands = parser.add_subparsers(dest="browser_command", required=True)
    command = commands.add_parser("reopen", help="reopen saved regular Chrome windows that are no longer open")
    command.add_argument("--profile", help="only this Chrome profile")
    command.add_argument("--window", action="append", metavar="ID",
                         help="only this saved window, for example window-3; repeatable")
    command.add_argument("--dry-run", action="store_true", help="say what would be reopened")
    parser.set_defaults(func=cmd_browser)
