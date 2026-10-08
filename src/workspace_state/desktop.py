from __future__ import annotations

import ast
import json
import threading
from functools import wraps
from typing import Any

from .util import CommandError, run


# Chrome identification can focus another workspace. Keep each final native
# placement transaction intact while unrelated app launches/I/O run in parallel.
placement_lock = threading.RLock()


def serialized_placement(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with placement_lock:
            return function(*args, **kwargs)
    return locked


DESKTOP_REQUIRED_CAPABILITIES = {
    "list_windows",
    "list_monitors",
    "list_workspaces",
    "place_window",
    "expect_window",
    "expectation_status",
    "monitor_recovery",
    "placement_lifecycle_v2",
}


def _winctl(arguments: list[str], *, timeout: float | None = None) -> Any:
    command = ["gnome-winctl", *arguments, "--json"]
    output = run(command) if timeout is None else run(command, timeout=timeout)
    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise CommandError("gnome-winctl returned invalid JSON") from error
    if isinstance(result, dict) and result.get("ok") is False:
        raise CommandError(str(result.get("error") or "gnome-winctl operation failed"))
    return result


def _gsettings(schema: str, key: str) -> str:
    return run(["gsettings", "get", schema, key]).strip()


def workspace_names(*, shell: dict[str, Any] | None = None) -> list[str]:
    if shell is None:
        shell = capture_shell()
    workspaces = shell.get("workspaces", [])
    if workspaces:
        return [str(item.get("name") or item.get("index", "")) for item in workspaces]
    try:
        raw = _gsettings("org.gnome.desktop.wm.preferences", "workspace-names")
        return [str(item) for item in ast.literal_eval(raw)]
    except (CommandError, FileNotFoundError, SyntaxError, ValueError):
        return []


def capture_shell(*, timeout: float | None = None) -> dict[str, Any]:
    try:
        result = _winctl(["state"]) if timeout is None else _winctl(["state"], timeout=timeout)
    except (CommandError, FileNotFoundError):
        return {"available": False, "windows": [], "monitors": [], "workspaces": []}
    if not isinstance(result, dict):
        return {"available": False, "windows": [], "monitors": [], "workspaces": []}
    result["available"] = True
    return result


def list_windows() -> list[dict[str, Any]]:
    result = capture_shell().get("windows", [])
    return result if isinstance(result, list) else []


def desktop_topology_signature(shell: dict[str, Any]) -> str:
    """Return the display/workspace state that must settle before login restore."""
    monitors = [
        {
            key: item.get(key)
            for key in ("index", "connector", "x", "y", "width", "height", "scale", "primary")
        } | {"identity": item.get("identity") or {}}
        for item in shell.get("monitors", [])
        if isinstance(item, dict)
    ]
    workspaces = [
        {"index": item.get("index"), "name": item.get("name")}
        for item in shell.get("workspaces", [])
        if isinstance(item, dict)
    ]
    return json.dumps(
        {"monitors": monitors, "workspaces": workspaces},
        sort_keys=True,
        separators=(",", ":"),
    )


def desktop_readiness(
    shell: dict[str, Any],
    capabilities: set[str] | None = None,
) -> tuple[bool, str]:
    """Describe whether GNOME's display/workspace placement service is ready."""
    if not shell.get("available"):
        return False, "GNOME window placement service"
    required = capabilities or set()
    available = set(shell.get("capabilities", []))
    missing = sorted(required - available)
    if missing:
        return False, "GNOME window placement capabilities: " + ", ".join(missing)
    monitors = shell.get("monitors", [])
    if not isinstance(monitors, list) or not monitors:
        return False, "GNOME display topology"
    workspaces = shell.get("workspaces", [])
    if not isinstance(workspaces, list) or not workspaces:
        return False, "GNOME workspaces"
    if any(item.get("index") is None or item.get("name") is None for item in workspaces):
        return False, "GNOME workspace names"
    policy = shell.get("monitor_policy")
    if "monitor_recovery" in required and not isinstance(policy, dict):
        return False, "GNOME display recovery status"
    if isinstance(policy, dict):
        if policy.get("screen_unavailable"):
            return False, "unlocked GNOME desktop"
        if (
            policy.get("display_identity_ready") is False
            or (
                "monitor_recovery" in required
                and policy.get("display_identity_ready") is not True
            )
        ):
            return False, "physical display identities"
        if (
            policy.get("display_identity_cache_valid") is False
            or (
                "monitor_recovery" in required
                and policy.get("display_identity_cache_valid") is not True
            )
        ):
            return False, "physical display identity cache"
        if policy.get("display_identity_refreshing"):
            return False, "physical display identity refresh"
        if policy.get("display_identity_retry_pending"):
            return False, "physical display identity retry"
        if policy.get("recovery_active"):
            return False, "GNOME display recovery"
    return True, "GNOME displays and workspaces"


def _unique_monitor(
    candidates: list[dict[str, Any]],
    identity: dict[str, Any],
) -> dict[str, Any] | None:
    if len(candidates) == 1:
        return candidates[0]
    connector = str(identity.get("connector") or "")
    if connector:
        connector_matches = [
            item for item in candidates
            if str((item.get("identity") or {}).get("connector") or "") == connector
        ]
        if len(connector_matches) == 1:
            return connector_matches[0]
    return None


def _monitor_for_identity(
    identity: dict[str, Any],
    monitors: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for field in ("edid_hash", "edid_checksum"):
        value = str(identity.get(field) or "")
        comparable = any((item.get("identity") or {}).get(field) for item in monitors)
        if value and comparable:
            matches = [
                item for item in monitors
                if str((item.get("identity") or {}).get(field) or "") == value
            ]
            return _unique_monitor(matches, identity)

    serial = str(identity.get("serial") or "")
    comparable_serial = any(
        (item.get("identity") or {}).get("serial") for item in monitors
    )
    if serial and comparable_serial:
        matches = [
            item for item in monitors
            if str((item.get("identity") or {}).get("serial") or "") == serial
            and (
                not identity.get("vendor")
                or (item.get("identity") or {}).get("vendor") == identity.get("vendor")
            )
            and (
                not identity.get("product")
                or (item.get("identity") or {}).get("product") == identity.get("product")
            )
        ]
        return _unique_monitor(matches, identity)

    connector = str(identity.get("connector") or "")
    if connector:
        matches = [
            item for item in monitors
            if str((item.get("identity") or {}).get("connector") or "") == connector
            and (
                not identity.get("vendor")
                or (item.get("identity") or {}).get("vendor") == identity.get("vendor")
            )
            and (
                not identity.get("product")
                or (item.get("identity") or {}).get("product") == identity.get("product")
            )
        ]
        return _unique_monitor(matches, identity)

    vendor = str(identity.get("vendor") or "")
    product = str(identity.get("product") or "")
    if vendor or product:
        matches = [
            item for item in monitors
            if (not vendor or (item.get("identity") or {}).get("vendor") == vendor)
            and (not product or (item.get("identity") or {}).get("product") == product)
        ]
        return _unique_monitor(matches, identity)
    return None


def remap_monitor(
    placement: dict[str, Any],
    *,
    require_identity: bool = False,
    shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    identity = placement.get("monitor_intent") or placement.get("monitor_identity")
    saved_identity = dict(identity or {})
    updated = dict(placement)
    if saved_identity:
        updated["monitor_identity"] = dict(saved_identity)
        updated["monitor_intent"] = dict(saved_identity)
    current = capture_shell() if shell is None else shell
    candidates = current.get("monitors", [])
    if not candidates:
        return updated

    monitor = _monitor_for_identity(saved_identity, candidates) if saved_identity else None
    matched_identity = monitor is not None
    if require_identity and not saved_identity:
        raise CommandError("saved window has no physical display identity")
    if require_identity and monitor is None:
        label = (
            saved_identity.get("product")
            or saved_identity.get("serial")
            or saved_identity.get("connector")
            or "unknown display"
        )
        raise CommandError(f"saved physical display is not connected: {label}")
    if monitor is None and not saved_identity:
        try:
            saved_index = int(placement.get("monitor", -1))
        except (TypeError, ValueError):
            saved_index = -1
        monitor = next(
            (item for item in candidates if int(item.get("index", -2)) == saved_index),
            None,
        )

    if monitor is None:
        monitor = next((item for item in candidates if item.get("primary")), candidates[0])

    updated["monitor"] = monitor["index"]
    updated["monitor_geometry"] = {
        key: monitor[key]
        for key in ("index", "connector", "x", "y", "width", "height")
        if key in monitor
    }
    if saved_identity:
        intent = dict(saved_identity)
        if matched_identity:
            intent.update(monitor.get("identity") or {})
    else:
        intent = dict(monitor.get("identity") or {})
    if intent:
        updated["monitor_identity"] = dict(intent)
        updated["monitor_intent"] = dict(intent)
    old_monitor = placement.get("monitor_geometry") or {}
    geometry = placement.get("geometry") or {}
    if old_monitor and geometry:
        updated["geometry"] = {
            **geometry,
            "x": int(monitor["x"]) + int(geometry["x"]) - int(old_monitor["x"]),
            "y": int(monitor["y"]) + int(geometry["y"]) - int(old_monitor["y"]),
        }
    return updated


def remap_workspace(
    placement: dict[str, Any], *, shell: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = placement.get("workspace_name")
    if not name:
        return placement
    names = workspace_names(shell=shell) if shell is not None else workspace_names()
    if name not in names:
        return placement
    updated = dict(placement)
    updated["workspace"] = names.index(name)
    return updated


def resolve_placement_target(
    placement: dict[str, Any], *, provider: str, shell: dict[str, Any],
) -> dict[str, Any]:
    """Resolve one saved target against one coherent topology observation.

    Window/content verification still reads fresh state after mutations. This
    snapshot lives only for target resolution and is never cached across jobs.
    """
    if not shell.get("available") or not shell.get("monitors"):
        raise CommandError("GNOME display state is unavailable")
    name = placement["workspace_name"]
    names = [item.get("name") for item in shell.get("workspaces", [])]
    if names.count(name) != 1:
        raise CommandError(f"Saved {provider} workspace is unavailable or ambiguous: {name}")
    return remap_monitor(remap_workspace(placement, shell=shell), require_identity=True, shell=shell)


def _place_result(selector: dict[str, Any], placement: dict[str, Any],
                  *, timeout: float | None = None) -> dict[str, Any]:
    try:
        arguments = [
            "place",
            "--selector-json", json.dumps(selector, separators=(",", ":")),
            "--target-json", json.dumps(placement, separators=(",", ":")),
        ]
        result = _winctl(arguments) if timeout is None else _winctl(arguments, timeout=timeout)
    except (CommandError, FileNotFoundError):
        return {"placed": False, "status": "unavailable"}
    return result if isinstance(result, dict) else {"placed": False, "status": "invalid"}


def _place(selector: dict[str, Any], placement: dict[str, Any]) -> bool:
    return bool(_place_result(selector, placement).get("placed"))


def place_by_title(title: str, placement: dict[str, Any]) -> bool:
    return _place({"title": title}, placement)


def expect_window(
    app_id: str,
    placement: dict[str, Any],
    *,
    title: str | None = None,
) -> str | None:
    selector = {"app_id": app_id}
    if title is not None:
        selector["title"] = title
    try:
        result = _winctl([
            "expect",
            "--selector-json", json.dumps(selector, separators=(",", ":")),
            "--target-json", json.dumps(placement, separators=(",", ":")),
            "--timeout", "20",
        ])
    except (CommandError, FileNotFoundError):
        return None
    if not isinstance(result, dict) or not result.get("token"):
        return None
    return str(result["token"])


def expected_window_status(expectation_id: str) -> str:
    try:
        result = _winctl(["expectation", expectation_id])
    except (CommandError, FileNotFoundError):
        return "unknown"
    if not isinstance(result, dict):
        return "unknown"
    if result.get("deferred"):
        return "deferred"
    # Keep the legacy caller vocabulary while strengthening its meaning:
    # only compositor verification may be called placed.
    state = str(result.get("status") or "unknown")
    return "placed" if state == "verified" else state


def cancel_expected_window(expectation_id: str) -> bool:
    try:
        result = _winctl(["cancel", expectation_id])
    except (CommandError, FileNotFoundError):
        return False
    return isinstance(result, dict) and bool(result.get("cancelled"))


def move_window(window_id: int, placement: dict[str, Any]) -> bool:
    return _place({"id": int(window_id)}, placement)


def move_window_result(window_id: int, placement: dict[str, Any],
                       *, timeout: float | None = None) -> dict[str, Any]:
    if timeout is None:
        return _place_result({"id": int(window_id)}, placement)
    return _place_result({"id": int(window_id)}, placement, timeout=timeout)


def place_by_pid(pid: int, placement: dict[str, Any]) -> bool:
    return _place({"pid": int(pid)}, placement)
