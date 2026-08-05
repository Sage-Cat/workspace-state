from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from .util import CommandError, run

BUS = "org.sagecat.WorkspaceState"
OBJECT = "/org/sagecat/WorkspaceState"


def _gsettings(schema: str, key: str) -> str:
    return run(["gsettings", "get", schema, key]).strip()


def workspace_names() -> list[str]:
    raw = _gsettings("org.gnome.desktop.wm.preferences", "workspace-names")
    # GSettings prints a Python-compatible string array for ordinary names.
    import ast

    try:
        return [str(item) for item in ast.literal_eval(raw)]
    except (SyntaxError, ValueError):
        return []


def capture_shell() -> dict[str, Any]:
    try:
        output = run([
            "gdbus", "call", "--session", "--dest", BUS,
            "--object-path", OBJECT, "--method", f"{BUS}.Capture",
        ])
    except (CommandError, FileNotFoundError):
        return {"available": False, "windows": [], "monitors": []}

    try:
        result = json.loads(_gdbus_value(output))
        _add_monitor_identities(result)
        result["available"] = True
        return result
    except (SyntaxError, ValueError, json.JSONDecodeError, IndexError, TypeError):
        return {"available": False, "windows": [], "monitors": []}


def list_windows() -> list[dict[str, Any]]:
    try:
        output = run([
            "gdbus", "call", "--session", "--dest", BUS,
            "--object-path", OBJECT, "--method", f"{BUS}.ListWindows",
        ])
        payload = _gdbus_value(output)
        result = json.loads(payload)
        return result if isinstance(result, list) else []
    except (CommandError, FileNotFoundError, SyntaxError, ValueError, json.JSONDecodeError, TypeError):
        return []


def _gdbus_value(output: str) -> Any:
    import ast

    value = ast.literal_eval(output.strip().rstrip(","))
    return value[0] if isinstance(value, tuple) else value


def _edid_hashes() -> dict[str, str]:
    result = {}
    for path in Path("/sys/class/drm").glob("card*-*/edid"):
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        entry = path.parent.name
        connector = entry.split("-", 1)[1] if "-" in entry else entry
        result[connector] = hashlib.sha256(raw).hexdigest()
    return result


def _add_monitor_identities(result: dict[str, Any]) -> None:
    """Match Shell monitor indexes to stable identities from monitors.xml."""
    path = Path.home() / ".config/monitors.xml"
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return

    edid_hashes = _edid_hashes()
    shell_by_rect = {
        (int(m["x"]), int(m["y"]), int(m["width"]), int(m["height"])): m
        for m in result.get("monitors", [])
    }
    for configuration in root.findall("configuration"):
        matches: list[tuple[dict[str, Any], dict[str, str]]] = []
        for logical in configuration.findall("logicalmonitor"):
            monitor = logical.find("monitor")
            spec = monitor.find("monitorspec") if monitor is not None else None
            mode = monitor.find("mode") if monitor is not None else None
            if spec is None or mode is None:
                continue
            try:
                scale = float(logical.findtext("scale", "1"))
                rect = (
                    int(logical.findtext("x", "0")), int(logical.findtext("y", "0")),
                    round(int(mode.findtext("width", "0")) / scale),
                    round(int(mode.findtext("height", "0")) / scale),
                )
            except (ValueError, ZeroDivisionError):
                continue
            shell_monitor = shell_by_rect.get(rect)
            if shell_monitor is None:
                continue
            identity = {key: spec.findtext(key, "") for key in ("connector", "vendor", "product", "serial")}
            if identity["connector"] in edid_hashes:
                identity["edid_hash"] = edid_hashes[identity["connector"]]
            matches.append((shell_monitor, identity))
        if len(matches) == len(shell_by_rect) and matches:
            for shell_monitor, identity in matches:
                shell_monitor["identity"] = identity
            break

    by_index = {int(m["index"]): m.get("identity") for m in result.get("monitors", [])}
    for window in result.get("windows", []):
        identity = by_index.get(int(window.get("monitor", -1)))
        if identity:
            window["monitor_identity"] = identity


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
        monitor = next(
            (item for item in candidates if item.get("primary")),
            candidates[0],
        )

    updated = dict(placement)
    updated["monitor"] = monitor["index"]
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


def place_by_title(title: str, placement: dict[str, Any]) -> bool:
    geometry = placement.get("geometry", {})
    args = [
        "gdbus", "call", "--session", "--dest", BUS,
        "--object-path", OBJECT, "--method", f"{BUS}.PlaceByTitle",
        title,
        str(max(0, int(placement.get("workspace", 0)))),
        str(max(0, int(placement.get("monitor", 0)))),
        str(int(geometry.get("x", 0))), str(int(geometry.get("y", 0))),
        str(max(1, int(geometry.get("width", 1000)))),
        str(max(1, int(geometry.get("height", 700)))),
        str(int(placement.get("maximized", 0))),
    ]
    try:
        return "true" in run(args).lower()
    except (CommandError, FileNotFoundError):
        return False


def expect_window(app_id: str, placement: dict[str, Any]) -> str | None:
    geometry = placement.get("geometry") or {}
    state = str(placement.get("state") or ("maximized" if placement.get("maximized") else "normal"))
    args = [
        "gdbus", "call", "--session", "--dest", BUS,
        "--object-path", OBJECT, "--method", f"{BUS}.PlaceNextWindow",
        app_id,
        str(max(0, int(placement.get("workspace", 0)))),
        str(max(0, int(placement.get("monitor", 0)))),
        str(int(geometry.get("x", 0))), str(int(geometry.get("y", 0))),
        str(max(1, int(geometry.get("width", 1000)))),
        str(max(1, int(geometry.get("height", 700)))),
        state,
    ]
    try:
        return str(_gdbus_value(run(args)))
    except (CommandError, FileNotFoundError, SyntaxError, ValueError, TypeError):
        return None


def expected_window_status(expectation_id: str) -> str:
    try:
        return str(_gdbus_value(run([
            "gdbus", "call", "--session", "--dest", BUS,
            "--object-path", OBJECT, "--method", f"{BUS}.PlacementStatus",
            expectation_id,
        ])))
    except (CommandError, FileNotFoundError, SyntaxError, ValueError, TypeError):
        return "unknown"


def cancel_expected_window(expectation_id: str) -> bool:
    try:
        return "true" in run([
            "gdbus", "call", "--session", "--dest", BUS,
            "--object-path", OBJECT, "--method", f"{BUS}.CancelPlacement",
            expectation_id,
        ]).lower()
    except (CommandError, FileNotFoundError):
        return False


def move_window(window_id: int, placement: dict[str, Any]) -> bool:
    geometry = placement.get("geometry") or {}
    state = str(placement.get("state") or ("maximized" if placement.get("maximized") else "normal"))
    try:
        return "true" in run([
            "gdbus", "call", "--session", "--dest", BUS,
            "--object-path", OBJECT, "--method", f"{BUS}.MoveWindow",
            str(max(0, int(window_id))),
            str(max(0, int(placement.get("workspace", 0)))),
            str(max(0, int(placement.get("monitor", 0)))),
            str(int(geometry.get("x", 0))), str(int(geometry.get("y", 0))),
            str(max(1, int(geometry.get("width", 1000)))),
            str(max(1, int(geometry.get("height", 700)))),
            state,
        ]).lower()
    except (CommandError, FileNotFoundError):
        return False
