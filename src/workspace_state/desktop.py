from __future__ import annotations

import ast
import json
from typing import Any

from .util import CommandError, run


def _winctl(arguments: list[str]) -> Any:
    output = run(["gnome-winctl", *arguments, "--json"])
    try:
        result = json.loads(output)
    except json.JSONDecodeError as error:
        raise CommandError("gnome-winctl returned invalid JSON") from error
    if isinstance(result, dict) and result.get("ok") is False:
        raise CommandError(str(result.get("error") or "gnome-winctl operation failed"))
    return result


def _gsettings(schema: str, key: str) -> str:
    return run(["gsettings", "get", schema, key]).strip()


def workspace_names() -> list[str]:
    shell = capture_shell()
    workspaces = shell.get("workspaces", [])
    if workspaces:
        return [str(item.get("name") or item.get("index", "")) for item in workspaces]
    try:
        raw = _gsettings("org.gnome.desktop.wm.preferences", "workspace-names")
        return [str(item) for item in ast.literal_eval(raw)]
    except (CommandError, FileNotFoundError, SyntaxError, ValueError):
        return []


def capture_shell() -> dict[str, Any]:
    try:
        result = _winctl(["state"])
    except (CommandError, FileNotFoundError):
        return {"available": False, "windows": [], "monitors": [], "workspaces": []}
    if not isinstance(result, dict):
        return {"available": False, "windows": [], "monitors": [], "workspaces": []}
    result["available"] = True
    return result


def list_windows() -> list[dict[str, Any]]:
    result = capture_shell().get("windows", [])
    return result if isinstance(result, list) else []


def remap_monitor(placement: dict[str, Any]) -> dict[str, Any]:
    identity = placement.get("monitor_identity")
    current = capture_shell()
    candidates = current.get("monitors", [])
    if not candidates:
        return placement

    monitor = None
    if identity:
        edid_hash = identity.get("edid_hash")
        if edid_hash:
            monitor = next(
                (item for item in candidates if (item.get("identity") or {}).get("edid_hash") == edid_hash),
                None,
            )
        checksum = identity.get("edid_checksum")
        if monitor is None and checksum:
            monitor = next(
                (item for item in candidates if (item.get("identity") or {}).get("edid_checksum") == checksum),
                None,
            )
        serial = identity.get("serial")
        if monitor is None and serial:
            monitor = next(
                (item for item in candidates if (item.get("identity") or {}).get("serial") == serial),
                None,
            )
        if monitor is None:
            connector_matches = [
                item for item in candidates
                if (item.get("identity") or {}).get("connector") == identity.get("connector")
                and (item.get("identity") or {}).get("vendor") == identity.get("vendor")
                and (item.get("identity") or {}).get("product") == identity.get("product")
            ]
            if len(connector_matches) == 1:
                monitor = connector_matches[0]
            else:
                model_matches = [
                    item for item in candidates
                    if (item.get("identity") or {}).get("vendor") == identity.get("vendor")
                    and (item.get("identity") or {}).get("product") == identity.get("product")
                ]
                if len(model_matches) == 1:
                    monitor = model_matches[0]

    if monitor is None:
        monitor = next((item for item in candidates if item.get("primary")), candidates[0])

    updated = dict(placement)
    updated["monitor"] = monitor["index"]
    updated["monitor_geometry"] = {
        key: monitor[key] for key in ("index", "x", "y", "width", "height") if key in monitor
    }
    updated["monitor_identity"] = dict(monitor.get("identity") or identity or {})
    old_monitor = placement.get("monitor_geometry") or {}
    geometry = placement.get("geometry") or {}
    if old_monitor and geometry:
        updated["geometry"] = {
            **geometry,
            "x": int(monitor["x"]) + int(geometry["x"]) - int(old_monitor["x"]),
            "y": int(monitor["y"]) + int(geometry["y"]) - int(old_monitor["y"]),
        }
    return updated


def remap_workspace(placement: dict[str, Any]) -> dict[str, Any]:
    name = placement.get("workspace_name")
    if not name:
        return placement
    names = workspace_names()
    if name not in names:
        return placement
    updated = dict(placement)
    updated["workspace"] = names.index(name)
    return updated


def _place(selector: dict[str, Any], placement: dict[str, Any]) -> bool:
    try:
        result = _winctl([
            "place",
            "--selector-json", json.dumps(selector, separators=(",", ":")),
            "--target-json", json.dumps(placement, separators=(",", ":")),
        ])
    except (CommandError, FileNotFoundError):
        return False
    return isinstance(result, dict) and bool(result.get("placed"))


def place_by_title(title: str, placement: dict[str, Any]) -> bool:
    return _place({"title": title}, placement)


def expect_window(app_id: str, placement: dict[str, Any]) -> str | None:
    try:
        result = _winctl([
            "expect",
            "--selector-json", json.dumps({"app_id": app_id}, separators=(",", ":")),
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
    return str(result.get("status") or "unknown") if isinstance(result, dict) else "unknown"


def cancel_expected_window(expectation_id: str) -> bool:
    try:
        result = _winctl(["cancel", expectation_id])
    except (CommandError, FileNotFoundError):
        return False
    return isinstance(result, dict) and bool(result.get("cancelled"))


def move_window(window_id: int, placement: dict[str, Any]) -> bool:
    return _place({"id": int(window_id)}, placement)


def place_by_pid(pid: int, placement: dict[str, Any]) -> bool:
    return _place({"pid": int(pid)}, placement)
