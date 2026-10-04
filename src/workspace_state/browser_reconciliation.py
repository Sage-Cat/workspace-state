"""Read-only proof for a retained recipe's newer, intact native browser session.

This selects a reuse-only startup input; it never saves/adopts a checkpoint.
The old recipe remains canonical until a separate successful capture.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math


class ReconciliationRequired(RuntimeError):
    pass


ACTION = ("Browser reconciliation needs review: {reason}. Current windows, tabs and groups "
          "were preserved; no replacement was created. Compare the saved recipe with the "
          "current Chrome windows, then explicitly save the current desktop if it is the "
          "wanted baseline, or recover the missing original content before retrying.")


def reject(reason):
    raise ReconciliationRequired(ACTION.format(reason=reason))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def window_signature(window):
    """Ignore runtime/ordinal IDs, titles and focus, never content or group membership."""
    if (not isinstance(window, dict) or window.get("type") not in {"normal", "popup"}
            or type(window.get("incognito")) is not bool):
        reject("invalid window evidence")
    tabs, groups = window.get("tabs"), window.get("groups")
    if not isinstance(tabs, list) or not tabs or not isinstance(groups, list):
        reject("incomplete tab/group evidence")
    descriptors = {}
    for group in groups:
        if (not isinstance(group, dict) or not isinstance(group.get("id"), str)
                or group["id"] in descriptors
                or not isinstance(group.get("title"), str)
                or not isinstance(group.get("color"), str) or not group["color"]
                or type(group.get("collapsed")) is not bool):
            reject("ambiguous group identity")
        members = [i for i, tab in enumerate(tabs) if isinstance(tab, dict)
                   and tab.get("group") == group["id"]]
        if not members or any(tabs[i].get("pinned") for i in members):
            reject("invalid group membership")
        descriptors[group["id"]] = [members, group.get("title", ""),
                                     group.get("color"), bool(group.get("collapsed"))]
    content = []
    for tab in tabs:
        if (not isinstance(tab, dict) or not isinstance(tab.get("url"), str) or not tab["url"]
                or type(tab.get("pinned")) is not bool):
            reject("missing tab URL")
        group = tab.get("group")
        if group is not None and group not in descriptors:
            reject("missing group evidence")
        content.append([tab["url"], bool(tab.get("pinned")), descriptors.get(group)])
    return json.dumps([window["type"], bool(window.get("incognito")), content],
                      separators=(",", ":"), ensure_ascii=False)


def profiles_by_name(state):
    if not isinstance(state, dict) or state.get("available") is not True or state.get("errors"):
        reject("browser capture was incomplete")
    profiles = state.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        reject("missing profile evidence")
    result = {}
    for profile in profiles:
        name = profile.get("profile") if isinstance(profile, dict) else None
        if not isinstance(name, str) or not name or name in result:
            reject("ambiguous profile identity")
        if not isinstance(profile.get("windows"), list) or not profile["windows"]:
            reject("empty profile evidence")
        result[name] = profile
    return result


WITNESS_KEY = "retained_browser_capture"
RETAINED_REASON = "retained the saved browsers recipe because restoration did not complete this login"


def browser_state(snapshot):
    return snapshot.get("browsers", {}).get("google_chrome", snapshot.get("chrome", {}))


def _capture_evidence(snapshot, saved):
    if WITNESS_KEY not in snapshot:
        return {"created_at": snapshot.get("created_at"),
                "capture_context": snapshot.get("capture_context"),
                "preserved_categories": snapshot.get("capture_errors", {}).get("preserved_categories", [])}
    witness = snapshot[WITNESS_KEY]
    if (not isinstance(witness, dict) or type(witness.get("schema_version")) is not int
            or witness["schema_version"] != 1 or witness.get("source") != "terminal-autosave"
            or witness.get("browser_digest") != digest(saved)
            or not isinstance(witness.get("capture_evidence"), dict)):
        reject("the preserved browser capture witness is invalid or obsolete")
    return witness["capture_evidence"]


def observation_witness(snapshot):
    """Keep validated original evidence, never assign terminal capture time to it."""
    if retained_observation(snapshot) is None:
        return None
    saved = browser_state(snapshot)
    return {"schema_version": 1, "source": "terminal-autosave",
            "browser_digest": digest(saved),
            "capture_evidence": deepcopy(_capture_evidence(snapshot, saved))}


def retained_observation(snapshot):
    """Return eligible evidence, rejecting stale/unbound observations without mutation."""
    saved = browser_state(snapshot)
    if "latest_observation" not in saved:
        return None
    observation = saved["latest_observation"]
    evidence = _capture_evidence(snapshot, saved)
    context = evidence.get("capture_context") or {}
    preserved = evidence.get("preserved_categories", [])
    expected = RETAINED_REASON
    if (not isinstance(context, dict) or type(context.get("schema_version")) is not int
            or context["schema_version"] != 1 or not isinstance(preserved, list)
            or not isinstance(context.get("provider_evidence"), dict)
            or not isinstance(context["provider_evidence"].get("browsers"), dict)):
        reject("the newer observation lacks complete retained-capture evidence")
    if (not isinstance(observation, dict) or observation.get("schema_version") != 1
            or expected not in preserved
            or context.get("provider_evidence", {}).get("browsers", {}).get("state") != "captured"):
        reject("the newer observation lacks complete retained-capture evidence")
    try:
        stamp = datetime.fromisoformat(observation["captured_at"].replace("Z", "+00:00"))
        captured = datetime.fromisoformat(context["captured_at"].replace("Z", "+00:00"))
        outer = datetime.fromisoformat(evidence["created_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None or captured.tzinfo is None or outer.tzinfo is None:
            raise ValueError("unscoped time")
        # Legacy top-level timestamps have second precision; context has fractions.
        if abs((stamp-captured).total_seconds()) >= 1 or abs((stamp-outer).total_seconds()) >= 1:
            raise ValueError("unbound time")
    except (KeyError, TypeError, ValueError, AttributeError):
        reject("the newer observation is not bound to its complete capture")
    observed = observation.get("browser_state")
    old_profiles, new_profiles = profiles_by_name(saved), profiles_by_name(observed)
    if set(old_profiles) != set(new_profiles):
        reject("the observed profile set differs from the retained recipe")
    for name, profile in new_profiles.items():
        if any(profile.get(key) != old_profiles[name].get(key)
               for key in ("profile_directory", "app_id")):
            reject("profile configuration changed")
        labels = set()
        signatures = set()
        for window in profile["windows"]:
            signature = window_signature(window)
            label = window.get("id")
            if not isinstance(label, str) or not label or label in labels or signature in signatures:
                reject("observed windows do not have unique content identities")
            labels.add(label)
            signatures.add(signature)
            if (type(window.get("workspace_index")) is not int
                    or window["workspace_index"] < 0 or not isinstance(window.get("monitor"), dict)
                    or type(window["monitor"].get("index")) is not int
                    or window["monitor"]["index"] < 0
                    or not isinstance(window.get("geometry"), dict)
                    or any(type(window["geometry"].get(key)) not in {int, float}
                           or not math.isfinite(window["geometry"][key]) for key in ("x", "y", "width", "height"))
                    or any(window["geometry"][key] <= 0 for key in ("width", "height"))):
                reject("the newer observation lacks exact native placement")
    return deepcopy(observed)


def reconcile(snapshot, native_profiles):
    """Exact full-catalog bijection; overlap/count/ordinal guesses never authorize reuse."""
    observed = retained_observation(snapshot)
    if observed is None:
        return None
    profiles = profiles_by_name(observed)
    if set(native_profiles) != set(profiles):
        reject("the complete native profile inventory differs")
    mappings = {}
    for name, expected in profiles.items():
        native = native_profiles[name]
        if any(native.get(key) != expected.get(key) for key in ("profile", "profile_directory", "app_id")):
            reject("native profile identity differs")
        live = native.get("windows", [])
        if len(live) != len(expected["windows"]):
            reject("native window counts differ from the newer observation")
        by_signature = {}
        ids = set()
        for window in live:
            signature = window_signature(window)
            identity = window.get("runtime_window_id")
            if type(identity) is not int or identity in ids or signature in by_signature:
                reject("native windows have ambiguous identities")
            ids.add(identity)
            by_signature[signature] = identity
        for window in expected["windows"]:
            signature = window_signature(window)
            if signature not in by_signature:
                reject("native URLs or group structure differ from the newer observation")
            window["_reconcile_window_id"] = by_signature.pop(signature)
        mappings[name] = {w["id"]: w["_reconcile_window_id"] for w in expected["windows"]}
    return observed, {"schema_version": 1, "state": "matched-reuse-only",
                      "source_checkpoint_digest": digest(snapshot),
                      "observation_digest": digest(snapshot.get("browsers", {}).get("google_chrome", {}).get("latest_observation")),
                      "native_windows": mappings,
                      "canonical_checkpoint_changed": False}
