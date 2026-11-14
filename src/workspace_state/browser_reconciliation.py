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
from collections import Counter, defaultdict
from urllib.parse import urlsplit
from uuid import UUID


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


def window_signature(window, *, ignore_group_collapse=False):
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
                                     group.get("color"),
                                     False if ignore_group_collapse else bool(group.get("collapsed"))]
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


def navigation_signature(window):
    """Placement equivalence only: keep every HTTP(S) URL byte before '#'."""
    value = deepcopy(window)
    for tab in value.get("tabs", []):
        url = tab.get("url")
        if isinstance(url, str):
            parsed = urlsplit(url)
            if parsed.scheme in {"http", "https"} and parsed.netloc:
                tab["url"] = url.partition("#")[0]
    return window_signature(value)


def _placement_signature(window):
    return digest({key: window.get(key) for key in (
        "workspace", "workspace_index", "monitor", "geometry", "geometry_relative", "state")})


def _runtime_identity(window):
    identity = window.get("runtime_identity")
    if (type(window.get("runtime_window_id")) is not int or not isinstance(identity, dict)
            or not isinstance(identity.get("tabs"), list) or not isinstance(identity.get("groups"), list)
            or len(identity["tabs"]) != len(window["tabs"])
            or len(identity["groups"]) != len(window["groups"])
            or any(not isinstance(tab, list) or len(tab) != 2
                   or any(type(item) is not int for item in tab) for tab in identity["tabs"])
            or any(type(group) is not int or group < 0 for group in identity["groups"])
            or len({tab[0] for tab in identity["tabs"]}) != len(identity["tabs"])
            or len(set(identity["groups"])) != len(identity["groups"])):
        reject("current tab/group identities are incomplete")
    return identity


def _url_multiset(window):
    """Tab URLs without HTTP(S) fragments; other schemes stay exact."""
    urls = Counter()
    for tab in window.get("tabs", []):
        url = tab.get("url") if isinstance(tab, dict) else None
        if isinstance(url, str) and url:
            parsed = urlsplit(url)
            web = parsed.scheme in {"http", "https"} and parsed.netloc
            urls[url.partition("#")[0] if web else url] += 1
    return urls


def _window_kind(window):
    return window.get("type"), bool(window.get("incognito"))


def _by_runtime_id(window):
    value = window.get("runtime_window_id")
    return value if type(value) is int else -1


def _overlap_pairs(expected, native):
    """Pair saved and native windows whose shared tab URLs single each other out.

    Chrome owns tabs and groups and may restore newer content than the saved
    recipe. Windows with identical navigation and one saved destination pair
    first. A remaining pair needs the strictly largest URL overlap from both
    sides, recomputed after each pairing, and the same window type. Placement
    is reversible, so the last window left on each side pairs by elimination.
    Return None unless every window pairs and identical native windows share
    one destination.
    """
    old = sorted(expected, key=lambda window: str(window.get("id")))
    live = sorted(native, key=_by_runtime_id)
    pairs = []
    old_buckets, live_buckets = defaultdict(list), defaultdict(list)
    for window in old:
        old_buckets[navigation_signature(window)].append(window)
    for window in live:
        live_buckets[navigation_signature(window)].append(window)
    for signature, originals in old_buckets.items():
        current = live_buckets.get(signature, [])
        if (len(current) == len(originals)
                and len({_placement_signature(window) for window in originals}) == 1):
            pairs += zip(originals, current)
    paired = {id(window) for pair in pairs for window in pair}
    old = [window for window in old if id(window) not in paired]
    live = [window for window in live if id(window) not in paired]
    urls = {id(window): _url_multiset(window) for window in old + live}

    def score(saved, current):
        if _window_kind(saved) != _window_kind(current):
            return 0
        return sum((urls[id(saved)] & urls[id(current)]).values())

    navigation = {id(window): navigation_signature(window) for window in live}
    while old and live:
        for saved in old:
            scores = [(score(saved, current), current) for current in live]
            best = max(value for value, _current in scores)
            winners = [current for value, current in scores if value == best]
            # A tie is harmless only between interchangeable native windows
            # and between saved windows bound for one destination.
            if best == 0 or len({navigation[id(current)] for current in winners}) != 1:
                continue
            if any(score(other, winners[0]) >= best
                   and _placement_signature(other) != _placement_signature(saved)
                   for other in old if other is not saved):
                continue
            pairs.append((saved, winners[0]))
            old = [window for window in old if window is not saved]
            live = [window for window in live if window is not winners[0]]
            break
        else:
            break
    if len(old) == 1 and len(live) == 1 and _window_kind(old[0]) == _window_kind(live[0]):
        pairs.append((old[0], live[0]))
        old, live = [], []
    if old or live:
        return None
    destinations = defaultdict(set)
    for saved, current in pairs:
        destinations[navigation_signature(current)].add(_placement_signature(saved))
    if any(len(places) > 1 for places in destinations.values()):
        return None
    return pairs


def reconcile_navigation(snapshot, native_profiles):
    """Bind a complete native catalog to saved placement, never to old content.

    A catalog whose windows differ only in HTTP(S) fragments pairs by its
    navigation buckets. Otherwise each native window pairs with the saved
    window whose tab URLs single it out (see `_overlap_pairs`); this attempt
    then places the current native tabs and groups at the saved destination.
    Unequal window counts and unresolved ties use the ordinary strict restore.
    """
    saved = browser_state(snapshot)
    if "latest_observation" in saved:
        return None  # Retained-capture evidence keeps its separate exact contract.
    profiles = profiles_by_name(saved)
    if set(native_profiles) != set(profiles):
        return None
    pairs = []
    changed = False
    for name, expected in profiles.items():
        native = native_profiles[name]
        if (not isinstance(native, dict) or any(native.get(key) != expected.get(key)
                for key in ("profile", "profile_directory", "app_id"))
                or not isinstance(native.get("windows"), list)
                or len(native["windows"]) != len(expected["windows"])):
            return None
        old_buckets, live_buckets = defaultdict(list), defaultdict(list)
        for window in expected["windows"]:
            old_buckets[navigation_signature(window)].append(window)
        for window in native["windows"]:
            live_buckets[navigation_signature(window)].append(window)
        if (set(old_buckets) != set(live_buckets) or any(
                len(old_buckets[key]) != len(live_buckets[key]) for key in old_buckets)):
            overlap = _overlap_pairs(expected["windows"], native["windows"])
            if overlap is None:
                return None
            changed = True
            pairs.append((name, expected, native, None, None, overlap))
            continue
        changed |= Counter(map(window_signature, expected["windows"])) != Counter(
            map(window_signature, native["windows"]))
        pairs.append((name, expected, native, old_buckets, live_buckets, None))
    if not changed:
        return None
    result = deepcopy(saved)
    result_profiles = profiles_by_name(result)
    mappings = {}
    for name, expected, native, old_buckets, live_buckets, overlap in pairs:
        ids = [window.get("runtime_window_id") for window in native["windows"]]
        labels = [window.get("id") for window in expected["windows"]]
        if (any(type(value) is not int for value in ids) or len(set(ids)) != len(ids)
                or any(not isinstance(label, str) or not label for label in labels)
                or len(set(labels)) != len(labels)):
            reject("current placement identities are ambiguous")
        by_label = {window["id"]: window for window in result_profiles[name]["windows"]}
        mappings[name] = {}
        matched = overlap
        if matched is None:
            matched = []
            for signature, originals in old_buckets.items():
                if len({_placement_signature(window) for window in originals}) != 1:
                    reject("fragment-equivalent windows have different saved destinations")
                # These slots have the same complete content shape and destination.
                # Pairing them does not guess an original identity or change content.
                matched += zip(sorted(originals, key=lambda w: w["id"]),
                               sorted(live_buckets[signature], key=lambda w: w["runtime_window_id"]))
        for old, live in matched:
            target = by_label[old["id"]]
            if navigation_signature(old) != navigation_signature(live):
                # Only this attempt's catalog carries the current native content;
                # the canonical recipe stays unchanged.
                target["tabs"] = deepcopy(live["tabs"])
                target["groups"] = deepcopy(live["groups"])
            target["_reconcile_window_id"] = live["runtime_window_id"]
            target["_preserve_current_content"] = True
            target["_runtime_identity"] = deepcopy(_runtime_identity(live))
            mappings[name][old["id"]] = live["runtime_window_id"]
    return result, {"schema_version": 1, "state": "matched-reuse-only",
                    "content_basis": "current-native-session",
                    "source_checkpoint_digest": digest(snapshot),
                    "native_windows": mappings, "canonical_checkpoint_changed": False}


def _window_binding_problem(saved, live_windows):
    """Return why a paired native window no longer binds, or None.

    Chrome window IDs are unique within a browser session, so after pairing only
    the ID, type and incognito flag matter; tab content may change freely.
    """
    window = next((item for item in live_windows
                   if isinstance(item, dict) and item.get("runtime_window_id") == saved.get("_reconcile_window_id")), None)
    if window is None:
        return "current native window is missing"
    if _window_kind(window) != _window_kind(saved):
        return "current native window type changed"
    return None


def preservation_window_problem(catalog, native_profiles, profile_name, window_id):
    """Return a problem for one preserved window, or None. Other windows are ignored."""
    expected = profiles_by_name(catalog).get(profile_name)
    if expected is None:
        return "current native profile is unknown"
    actual = native_profiles.get(profile_name)
    if not isinstance(actual, dict):
        return "current native profile is unavailable"
    if any(actual.get(key) != expected.get(key) for key in ("profile", "profile_directory", "app_id")):
        return "current native profile identity changed"
    saved = next((item for item in expected["windows"] if item.get("_reconcile_window_id") == window_id), None)
    if saved is None:
        return "current native window is not in the preserved catalog"
    live = actual.get("windows")
    return _window_binding_problem(saved, live if isinstance(live, list) else [])


def preservation_problems(catalog, native_profiles):
    """Map (profile, native window id) to a problem for every unbound catalog window."""
    problems = {}
    for name, expected in profiles_by_name(catalog).items():
        for saved in expected["windows"]:
            window_id = saved.get("_reconcile_window_id")
            problem = preservation_window_problem(catalog, native_profiles, name, window_id)
            if problem:
                problems[(name, window_id)] = problem
    return problems


def verify_navigation_reconciliation(catalog, native_profiles, profile_name=None, window_id=None):
    """Revalidate paired native windows without comparing tab content.

    With `profile_name` and `window_id` only that window is checked; otherwise
    every catalog window is, and the first problem is rejected.
    """
    if profile_name is not None:
        problem = preservation_window_problem(catalog, native_profiles, profile_name, window_id)
        if problem:
            reject(problem)
        return
    for problem in preservation_problems(catalog, native_profiles).values():
        reject(problem)


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


def recipe_digest(saved):
    recipe = deepcopy(saved)
    recipe.pop("latest_observation", None)
    return digest(recipe)


def capture_observation(snapshot, recipe, observed):
    """Bind new captures to their own transaction and exact browser payload."""
    context = snapshot.get("capture_context") or {}
    if context.get("schema_version") == 2:
        return {"schema_version": 2, "capture_id": context.get("capture_id"),
                "captured_at": context.get("completed_at"),
                "browser_digest": digest(observed), "retained_recipe_digest": recipe_digest(recipe),
                "browser_state": observed}
    # Readability for callers replaying a legacy capture; the consumer requires
    # original shutdown producer evidence, never timestamp proximity alone.
    return {"schema_version": 1, "captured_at": str(snapshot.get("created_at", "")),
            "browser_state": observed}


def _capture_evidence(snapshot, saved):
    if WITNESS_KEY not in snapshot:
        from .checkpoint import category_digest
        records = snapshot.get("category_provenance")
        records = records if isinstance(records, dict) else {}
        evidence = {"created_at": snapshot.get("created_at"),
                "capture_context": snapshot.get("capture_context"),
                "preserved_categories": snapshot.get("capture_errors", {}).get("preserved_categories", []),
                "category_provenance": {key: deepcopy(records.get(key)) for key in ("terminals", "browsers")},
                "terminal_content_digest": category_digest(snapshot, "terminals")}
        if snapshot.get("capture_context") is None and "latest_observation" in saved:
            return recover_legacy_evidence(snapshot)
        return evidence
    witness = snapshot[WITNESS_KEY]
    if (not isinstance(witness, dict) or type(witness.get("schema_version")) is not int
            or witness["schema_version"] != 1 or witness.get("source") != "terminal-autosave"
            or witness.get("browser_digest") != digest(saved)
            or not isinstance(witness.get("capture_evidence"), dict)):
        reject("the preserved browser capture witness is invalid or obsolete")
    return witness["capture_evidence"]


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("missing capture time")
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("unscoped capture time")
    return stamp


def _legacy_binding(saved, observation, evidence, context, captured, outer):
    """Recognize the old shutdown producer contract without inventing a digest.

    V1 did not independently digest the observation. Its original publication
    evidence can establish compatibility, but only the later exact native
    catalog bijection proves live browser content; it grants no adoption.
    """
    records = evidence.get("category_provenance")
    if not isinstance(records, dict):
        raise ValueError("missing legacy publication evidence")
    browser, terminal = records.get("browsers"), records.get("terminals")
    if not isinstance(browser, dict) or not isinstance(terminal, dict):
        raise ValueError("missing legacy category evidence")
    retention = browser.get("retention_evidence")
    terminal_context = terminal.get("capture_context")
    if (observation.get("captured_at") != evidence.get("created_at")
            or outer.microsecond != 0 or captured.replace(microsecond=0) > outer
            or type(browser.get("schema_version")) is not int or browser["schema_version"] != 1
            or browser.get("state") != "retained" or browser.get("retained_reason") != RETAINED_REASON
            or browser.get("content_digest") != recipe_digest(saved)
            or not isinstance(retention, dict) or retention.get("attempted_at") != evidence.get("created_at")
            or retention.get("capture_errors") != []
            or type(terminal.get("schema_version")) is not int or terminal["schema_version"] != 1
            or terminal.get("source") != "shutdown-save"
            or terminal.get("content_digest") != evidence.get("terminal_content_digest")
            or terminal.get("captured_at") != context.get("captured_at")
            or not isinstance(terminal_context, dict)
            or not isinstance(context.get("topology_signature"), str) or not context["topology_signature"]
            or terminal_context.get("topology_signature") != context["topology_signature"]
            or terminal_context.get("captured_at") != context.get("captured_at")
            or context["provider_evidence"].get("terminals", {}).get("state") != "captured"
            or terminal_context.get("provider_evidence") != context["provider_evidence"].get("terminals")):
        raise ValueError("unbound legacy publication")
    if terminal.get("state") == "failed":
        warnings = terminal.get("capture_errors")
        if (not isinstance(warnings, list) or not warnings
                or any(not isinstance(item, str) or not item.endswith("Codex session ID(s) are unresolved")
                       for item in warnings)):
            raise ValueError("partial legacy terminal capture")
    elif terminal.get("state") != "captured" or terminal.get("capture_errors"):
        raise ValueError("partial legacy terminal capture")


def _transaction_binding(saved, observation, evidence, context, captured, outer):
    identity = context.get("capture_id")
    if (not isinstance(identity, str) or str(UUID(identity)) != identity
            or UUID(identity).version != 4):
        raise ValueError("invalid capture identity")
    completed = _timestamp(context.get("completed_at"))
    observed_digest = digest(observation.get("browser_state"))
    if (observation.get("capture_id") != identity
            or observation.get("captured_at") != context.get("completed_at")
            or context.get("snapshot_created_at") != evidence.get("created_at")
            or not captured <= completed or not captured.replace(microsecond=0) <= outer <= completed
            or observation.get("browser_digest") != observed_digest
            or context["provider_evidence"]["browsers"].get("content_digest") != observed_digest
            or observation.get("retained_recipe_digest") != recipe_digest(saved)):
        raise ValueError("unbound transaction content")


def recover_legacy_evidence(snapshot, *, history_directory=None):
    """Recover one exact original publication lost by an old terminal-only hook.

    This is a lookup, not adoption or repair. No time-nearest, content-overlap
    or runtime-ID matching is used, and failed/ambiguous history never gains
    capture authority.
    """
    from .checkpoint import category_digest, history_generations
    saved = browser_state(snapshot)
    observation = saved.get("latest_observation")
    records = snapshot.get("category_provenance")
    terminal = records.get("terminals") if isinstance(records, dict) else None
    browser = records.get("browsers") if isinstance(records, dict) else None
    if (snapshot.get("capture_context") is not None or WITNESS_KEY in snapshot
            or not isinstance(observation, dict) or type(observation.get("schema_version")) is not int
            or observation["schema_version"] != 1 or not isinstance(terminal, dict)
            or type(terminal.get("schema_version")) is not int or terminal["schema_version"] != 1
            or terminal.get("source") != "terminal-autosave" or terminal.get("capture_context") is not None
            or terminal.get("content_digest") != category_digest(snapshot, "terminals")
            or terminal.get("captured_at") != snapshot.get("created_at")
            or terminal.get("state") not in {"captured", "failed"} or not isinstance(browser, dict)
            or RETAINED_REASON not in snapshot.get("capture_errors", {}).get("preserved_categories", [])):
        reject("the newer observation lacks original terminal-autosave ownership")
    if terminal["state"] == "failed":
        warnings = terminal.get("capture_errors")
        if (not isinstance(warnings, list) or not warnings
                or any(not isinstance(item, str) or not item.endswith("Codex session ID(s) are unresolved")
                       for item in warnings)):
            reject("the terminal-autosave capture was incomplete")
    elif terminal.get("capture_errors"):
        reject("the terminal-autosave capture was incomplete")
    try:
        current_stamp = _timestamp(snapshot.get("created_at"))
        generations = history_generations(history_directory)
        matches = []
        for name, candidate in generations:
            context = candidate.get("capture_context")
            if (WITNESS_KEY in candidate or not isinstance(context, dict)
                    or type(context.get("schema_version")) is not int or context["schema_version"] != 1
                    or digest(browser_state(candidate)) != digest(saved)
                    or digest(candidate.get("category_provenance", {}).get("browsers")) != digest(browser)):
                continue
            evidence = _capture_evidence(candidate, browser_state(candidate))
            _validated_observation(candidate, evidence)  # Original complete shutdown producer contract.
            if current_stamp < _timestamp(candidate.get("created_at")):
                continue
            matches.append((name, evidence))
    except (OSError, TypeError, ValueError, AttributeError, RecursionError, ReconciliationRequired):
        reject("the original browser capture history is unavailable or invalid")
    if len(matches) != 1:
        reject("the original browser capture history does not have one exact matching publication")
    name, evidence = matches[0]
    evidence = deepcopy(evidence)
    evidence["history_generation"] = name
    return evidence


def observation_witness(snapshot):
    """Keep validated original evidence, never assign terminal capture time to it."""
    saved = browser_state(snapshot)
    if "latest_observation" not in saved:
        return None
    evidence = _capture_evidence(snapshot, saved)
    _validated_observation(snapshot, evidence)
    return {"schema_version": 1, "source": "terminal-autosave",
            "browser_digest": digest(saved),
            "capture_evidence": deepcopy(evidence)}


def retained_observation(snapshot):
    """Return eligible evidence, rejecting stale/unbound observations without mutation."""
    saved = browser_state(snapshot)
    if "latest_observation" not in saved:
        return None
    evidence = _capture_evidence(snapshot, saved)
    return _validated_observation(snapshot, evidence)


def _validated_observation(snapshot, evidence):
    saved = browser_state(snapshot)
    observation = saved["latest_observation"]
    context = evidence.get("capture_context") or {}
    preserved = evidence.get("preserved_categories", [])
    expected = RETAINED_REASON
    if (not isinstance(context, dict) or type(context.get("schema_version")) is not int
            or context["schema_version"] not in {1, 2} or not isinstance(preserved, list)
            or not isinstance(context.get("provider_evidence"), dict)
            or not isinstance(context["provider_evidence"].get("browsers"), dict)):
        reject("the newer observation lacks complete retained-capture evidence")
    if (not isinstance(observation, dict) or type(observation.get("schema_version")) is not int
            or observation["schema_version"] != context["schema_version"]
            or expected not in preserved
            or context.get("provider_evidence", {}).get("browsers", {}).get("state") != "captured"):
        reject("the newer observation lacks complete retained-capture evidence")
    try:
        _timestamp(observation.get("captured_at"))
        captured, outer = _timestamp(context.get("captured_at")), _timestamp(evidence.get("created_at"))
        binding = _legacy_binding if context["schema_version"] == 1 else _transaction_binding
        binding(saved, observation, evidence, context, captured, outer)
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
