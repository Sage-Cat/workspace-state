"""Keep saved placements while a login restore has unresolved targets."""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import hashlib
import json
import time
from pathlib import Path

from .pending import social_key, vscode_key
from .provider_results import EvidenceState
from .startup import read_stage_marker
from .util import atomic_json


CATEGORIES = frozenset({"vscode", "social-apps"})
ACCEPTED = "accepted-placements.json"


def _receipt(directory: Path, category: str) -> Path:
    return directory / f"{category}-placement-guards.json"


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _digest(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def _entry_id(key: str, target: dict) -> str:
    return f"{key}/{_digest(target)}"


def _targets(snapshot: dict, category: str, workspace: str | None):
    """Number exactly the items passed to the provider, then bind to recipe keys."""
    if category == "vscode":
        targets = {}
        windows = [item for item in (snapshot.get(category) or {}).get("windows", [])
                   if item.get("placement") and (workspace is None
                       or item["placement"].get("workspace_name") == workspace)]
        for index, item in enumerate(windows, 1):
            key, target = vscode_key(item), item["placement"]
            _, _, item_ids = targets.setdefault(_entry_id(key, target), (key, target, []))
            item_ids.append(str(index))
        yield from targets.values()
    else:
        for app, record in (snapshot.get("social_apps") or {}).items():
            windows = [window for window in record.get("windows", [])
                       if workspace is None or window.get("workspace_name") == workspace]
            if record.get("mode") == "windowed" and windows:
                yield social_key(app), record, tuple(f"{app}:{i}" for i in range(1, len(windows) + 1))


def accept(directory: Path | None, fresh) -> None:
    """Remember successful manual acceptance independently of later autosaves."""
    if directory is not None and (categories := CATEGORIES.intersection(fresh)):
        path = directory / ACCEPTED
        accepted = _read(path)
        accepted.update(dict.fromkeys(categories, time.monotonic()))
        atomic_json(path, accepted)


def record(directory: Path | None, category: str, snapshot: dict, results, *,
           context=None, workspace: str | None = None, failed: bool = False,
           verified_only: bool = False) -> None:
    """Save only unresolved destinations, with exact request IDs for later verification."""
    if directory is None or category not in CATEGORIES or (not results and not failed):
        return
    by_id = {result.item_id: result for result in results if result.provider == category}
    path = _receipt(directory, category)
    previous = _read(path)
    guards = previous.copy()
    now = time.monotonic()
    for key, target, item_ids in _targets(snapshot, category, workspace):
        current = [by_id[item_id] for item_id in item_ids if item_id in by_id]
        if category == "social-apps" and (app_error := by_id.get(key.split(":", 1)[1])):
            current.append(app_error)
        entry = _entry_id(key, target)
        unresolved = [result for result in current if result.placement.state in {
            EvidenceState.FAILED, EvidenceState.WAITING, EvidenceState.UNKNOWN}]
        whole_target = category != "social-apps" or len(item_ids) == len(target["windows"])
        if not verified_only and (unresolved or (failed and not results)):
            requests = {result.item_id: result.placement.request_id for result in unresolved
                        if result.placement.state == EvidenceState.WAITING and result.placement.request_id}
            # If a subset of a social app's windows verified, the remaining
            # unresolved windows still own the app's whole visibility recipe.
            waiting = (len(current) == len(item_ids) and len(requests) == len(unresolved) > 0
                       and (whole_target or entry not in previous))
            guards[entry] = {"recorded_at": now, "requests": requests if waiting else {},
                             "context": context.to_dict() if context is not None else None,
                             "snapshot": snapshot.get("created_at", "")}
        elif (whole_target and len(current) == len(item_ids)
              and all(result.placement.state == EvidenceState.VERIFIED for result in current)):
            guards.pop(entry, None)
    if guards != previous:
        atomic_json(path, guards)


def _protected(directory: Path, category: str, previous: dict) -> set[str]:
    path = _receipt(directory, category)
    guards = _read(path)
    if not guards:
        return set()
    accepted = _read(directory / ACCEPTED).get(category, 0)
    marker = read_stage_marker(directory / f"{category}.done", category)
    verified = {item.get("item_id"): item["placement"].get("request_id")
                for item in marker.provider_results if item.get("provider") == category
                and isinstance(item.get("placement"), dict) and item["placement"].get("state") == "verified"
                } if marker is not None else {}
    active = {}
    for key, target, _ in _targets(previous, category, None):
        entry = _entry_id(key, target)
        guard = guards.get(entry)
        if not isinstance(guard, dict) or not isinstance(guard.get("recorded_at"), (float, int)):
            continue
        if isinstance(accepted, (float, int)) and accepted >= guard["recorded_at"]:
            continue
        requests = guard.get("requests")
        context = guard.get("context")
        if (requests and isinstance(requests, dict) and context and marker is not None
                and marker.operation_context == context and marker.snapshot == guard.get("snapshot")
                and all(token and verified.get(item_id) == token for item_id, token in requests.items())):
            continue
        active[entry] = guard
    if guards != active:
        atomic_json(path, active)
    return set(active)


def for_capture(directory: Path | None, previous: dict, categories) -> dict[str, set[str]]:
    """Resolve existing receipts before the compositor sample is taken."""
    return ({category: _protected(directory, category, previous)
             for category in CATEGORIES.intersection(categories)} if directory is not None else {})


def preserve(protection: dict[str, set[str]], snapshot: dict, previous: dict,
             fresh) -> tuple[dict[str, set[str]], list[dict]]:
    """Merge live observations and identify absent VS Code targets for pending carry."""
    from .vscode import volatile_project
    preserved = {}
    absent_vscode = []
    for category, protected in protection.items():
        if category not in fresh or not protected:
            continue
        if category == "vscode":
            saved = defaultdict(list)
            for item in previous["vscode"]["windows"]:
                saved[vscode_key(item)].append(item)
            windows = snapshot["vscode"]["windows"]
            observed = defaultdict(list)
            for item in windows:
                observed[vscode_key(item)].append(item)
            for key, items in saved.items():
                guarded = [item for item in items if _entry_id(key, item["placement"]) in protected]
                live = observed[key]
                if not live:
                    guarded = [item for item in guarded if not volatile_project(item)]
                    absent_vscode.extend(guarded)
                if not guarded:
                    continue
                preserved.setdefault(category, set()).add(key)
                if len(items) == len(live) == 1:
                    live[0]["placement"] = deepcopy(guarded[0]["placement"])
                elif live:
                    # The project key can name several indistinguishable
                    # windows. Keep their saved destinations without guessing
                    # which new rectangle belongs to which saved window.
                    windows[:] = [item for item in windows if vscode_key(item) != key] + deepcopy(items)
        else:
            current = snapshot["social_apps"]
            for app, target in previous["social_apps"].items():
                key = social_key(app)
                if app in current and _entry_id(key, target) in protected:
                    preserved.setdefault(category, set()).add(key)
                    if current[app]["mode"] == "windowed":
                        current[app] = deepcopy(target)
    return preserved, absent_vscode
