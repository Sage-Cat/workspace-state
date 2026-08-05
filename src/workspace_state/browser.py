from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .desktop import (
    cancel_expected_window,
    capture_shell,
    expect_window,
    expected_window_status,
    remap_monitor,
    remap_workspace,
    workspace_names,
)
from .util import data_home


class BrowserUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserRestoreResult:
    message: str
    success: bool = True


SUPPORTED_BROWSER_COMMANDS = {
    "google-chrome": "google-chrome",
}


def runtime_dir() -> Path:
    base = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return base / "workspace-state"


def profile_socket_path(profile: str) -> Path:
    digest = hashlib.sha256(profile.encode("utf-8")).hexdigest()[:16]
    return runtime_dir() / f"chrome-{digest}.sock"


def _host_paths() -> list[Path]:
    directory = runtime_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob("chrome-*.sock"))


def _request_path(
    path: Path,
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 60,
) -> Any:
    request = json.dumps({"action": action, "payload": payload or {}}, separators=(",", ":"))
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(path))
        connection.sendall(request.encode("utf-8") + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = connection.recv(65536)
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > 64 * 1024 * 1024:
                raise BrowserUnavailable("native host response is too large")
    except (OSError, TimeoutError) as error:
        raise BrowserUnavailable(f"Chrome native host is unavailable: {error}") from error
    finally:
        connection.close()
    if not chunks:
        raise BrowserUnavailable("Chrome native host closed without a response")
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0])
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BrowserUnavailable("Chrome native host returned invalid JSON") from error
    if not response.get("ok"):
        raise BrowserUnavailable(str(response.get("error") or "Chrome extension request failed"))
    return response.get("result")


def request_browser(
    action: str,
    payload: dict[str, Any] | None = None,
    *,
    profile: str,
    timeout: float = 60,
) -> Any:
    path = profile_socket_path(profile)
    if not path.exists():
        raise BrowserUnavailable(
            f"Chrome profile {profile!r} is not connected; open Chrome with the workspace-state extension enabled"
        )
    return _request_path(path, action, payload, timeout=timeout)


def connected_profiles() -> list[str]:
    profiles = []
    for path in _host_paths():
        try:
            result = _request_path(path, "ping", timeout=2)
            profile = str((result or {}).get("profile") or "Default")
            profiles.append(profile)
        except BrowserUnavailable:
            continue
    return sorted(set(profiles))


def ensure_browser_profiles(chrome: dict[str, Any], *, timeout: float = 15) -> list[str]:
    """Start supported browsers headlessly enough for their wsctl host to connect."""
    required = {
        str(profile.get("profile") or "Default")
        for profile in chrome.get("profiles", [])
    }
    missing = required - set(connected_profiles())
    if not missing:
        return []

    launched: list[str] = []
    extension = data_home() / "chrome-extension"
    for profile in chrome.get("profiles", []):
        profile_name = str(profile.get("profile") or "Default")
        if profile_name not in missing:
            continue
        app_id = _normalized_app_id(str(profile.get("app_id") or "google-chrome"))
        command_name = SUPPORTED_BROWSER_COMMANDS.get(app_id)
        command = shutil.which(command_name) if command_name else None
        if command is None:
            raise BrowserUnavailable(f"cannot start unsupported browser app ID: {app_id}")
        profile_directory = str(profile.get("profile_directory") or "")
        if not profile_directory:
            if profile_name != "Default":
                raise BrowserUnavailable(
                    f"Chrome profile {profile_name!r} has no saved profile directory; "
                    "configure it in the companion options and save again"
                )
            profile_directory = "Default"
        subprocess.Popen(
            [
                command,
                f"--profile-directory={profile_directory}",
                f"--load-extension={extension}",
                "--no-startup-window",
            ],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        launched.append(f"{command_name} ({profile_directory})")

    deadline = time.monotonic() + timeout
    while missing and time.monotonic() < deadline:
        time.sleep(0.1)
        missing -= set(connected_profiles())
    if missing:
        raise BrowserUnavailable(
            "Chrome companion did not connect for profile(s): " + ", ".join(sorted(missing))
        )
    return launched


def _normalized_app_id(value: str) -> str:
    result = value.casefold().strip()
    return result.removesuffix(".desktop")


def _shell_app_ids(window: dict[str, Any]) -> set[str]:
    return {
        _normalized_app_id(str(window.get(key) or ""))
        for key in ("app_id", "wm_class", "wm_class_instance", "sandboxed_app_id")
        if window.get(key)
    }


def _looks_like_chrome(window: dict[str, Any]) -> bool:
    return any(
        "chrome" in value or "chromium" in value
        for value in _shell_app_ids(window)
    )


def _geometry_score(browser_window: dict[str, Any], shell_window: dict[str, Any]) -> int:
    left = browser_window.get("bounds") or browser_window.get("geometry") or {}
    right = shell_window.get("geometry") or {}
    score = sum(
        abs(int(left.get(browser_key, 0)) - int(right.get(shell_key, 0)))
        for browser_key, shell_key in (
            ("left", "x"), ("top", "y"), ("width", "width"), ("height", "height"),
        )
    )
    active_title = next((
        str(tab.get("title") or "")
        for tab in browser_window.get("tabs", []) if tab.get("active")
    ), "")
    shell_title = str(shell_window.get("title") or "")
    if active_title and active_title.casefold() not in shell_title.casefold():
        score += 1_000_000
    return score


def _attach_desktop_placements(
    profiles: list[dict[str, Any]],
    shell: dict[str, Any],
    names: list[str],
) -> None:
    browser_windows = [
        (profile, window)
        for profile in profiles for window in profile.get("windows", [])
    ]
    configured_ids = {
        _normalized_app_id(str(profile.get("app_id") or "google-chrome"))
        for profile in profiles
    }
    shell_windows = [
        window for window in shell.get("windows", [])
        if _shell_app_ids(window).intersection(configured_ids) or _looks_like_chrome(window)
    ]
    pairs = sorted(
        (
            _geometry_score(browser_window, shell_window), browser_index, shell_index
        )
        for browser_index, (_profile, browser_window) in enumerate(browser_windows)
        for shell_index, shell_window in enumerate(shell_windows)
    )
    matches: dict[int, dict[str, Any]] = {}
    used_shell: set[int] = set()
    for _score, browser_index, shell_index in pairs:
        if browser_index in matches or shell_index in used_shell:
            continue
        matches[browser_index] = shell_windows[shell_index]
        used_shell.add(shell_index)

    for index, (profile, window) in enumerate(browser_windows):
        placement = matches.get(index)
        window.setdefault("app_id", profile.get("app_id") or "google-chrome")
        if placement is None:
            window["workspace"] = None
            window["workspace_index"] = None
            window["monitor"] = None
            window.setdefault("geometry", {
                "x": int((window.get("bounds") or {}).get("left", 0)),
                "y": int((window.get("bounds") or {}).get("top", 0)),
                "width": int((window.get("bounds") or {}).get("width", 1000)),
                "height": int((window.get("bounds") or {}).get("height", 700)),
            })
            continue
        workspace_index = int(placement.get("workspace", 0))
        monitor_identity = dict(placement.get("monitor_identity") or {})
        monitor_identity.update({
            "index": int(placement.get("monitor", 0)),
            "geometry": placement.get("monitor_geometry"),
        })
        window["workspace_index"] = workspace_index
        window["workspace"] = (
            names[workspace_index] if 0 <= workspace_index < len(names) else str(workspace_index)
        )
        window["monitor"] = monitor_identity
        window["geometry"] = placement.get("geometry")
        window["state"] = placement.get("state", window.get("state", "normal"))


def capture_browser(
    *,
    shell: dict[str, Any] | None = None,
    names: list[str] | None = None,
) -> dict[str, Any]:
    profiles: list[dict[str, Any]] = []
    errors = []
    for path in _host_paths():
        try:
            captured = _request_path(path, "capture")
            if isinstance(captured, dict):
                profiles.append(captured)
        except BrowserUnavailable as error:
            errors.append(str(error))
    if shell is None:
        shell = capture_shell()
    if names is None:
        names = workspace_names()
    _attach_desktop_placements(profiles, shell, names)
    return {
        "available": bool(profiles),
        "profiles": sorted(profiles, key=lambda item: str(item.get("profile", ""))),
        "errors": errors,
    }


def browser_window_placement(window: dict[str, Any]) -> dict[str, Any] | None:
    if window.get("workspace_index") is None or window.get("monitor") is None:
        return None
    monitor = window["monitor"]
    identity = {
        key: monitor[key]
        for key in ("connector", "edid_hash", "vendor", "product", "serial")
        if monitor.get(key)
    }
    return {
        "workspace": int(window["workspace_index"]),
        "workspace_name": window.get("workspace"),
        "monitor": int(monitor.get("index", 0)),
        "monitor_identity": identity,
        "monitor_geometry": monitor.get("geometry"),
        "geometry": window.get("geometry"),
        "state": window.get("state", "normal"),
    }


def browser_windows(chrome: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (profile, window)
        for profile in chrome.get("profiles", [])
        for window in profile.get("windows", [])
    ]


def restore_browser(
    chrome: dict[str, Any],
    *,
    workspace: str | None = None,
    place: bool = True,
    dry_run: bool = False,
    restore_token_prefix: str | None = None,
) -> list[BrowserRestoreResult]:
    selected = [
        (profile, window)
        for profile, window in browser_windows(chrome)
        if workspace is None or str(window.get("workspace") or "").casefold() == workspace.casefold()
    ]
    selected.sort(key=lambda item: bool(item[1].get("focused")))
    if not selected:
        return []

    actions: list[BrowserRestoreResult] = []
    for profile, window in selected:
        profile_name = str(profile.get("profile") or "Default")
        tab_count = len(window.get("tabs", []))
        label = str(window.get("id") or "window")
        if dry_run:
            actions.append(BrowserRestoreResult(
                f"restore Chrome {profile_name}/{label} ({tab_count} tabs)",
            ))
            continue

        placement = browser_window_placement(window)
        expectation = None
        if place and placement:
            placement = remap_monitor(remap_workspace(placement))
            expectation = expect_window(
                str(window.get("app_id") or profile.get("app_id") or "google-chrome"),
                placement,
            )
            if not expectation:
                actions.append(BrowserRestoreResult(
                    f"Chrome {profile_name}/{label} was not restored: GNOME placement is unavailable",
                    False,
                ))
                continue
        elif place:
            actions.append(BrowserRestoreResult(
                f"Chrome {profile_name}/{label} was not restored: no desktop placement was saved",
                False,
            ))
            continue
        try:
            result = request_browser(
                "restore_window",
                {
                    "window": window,
                    "place_expected": bool(expectation),
                    "restore_token": (
                        f"{restore_token_prefix}:{profile_name}:{label}"
                        if restore_token_prefix else None
                    ),
                },
                profile=profile_name,
            )
        except Exception:
            if expectation:
                cancel_expected_window(expectation)
            raise

        placed = not expectation
        if expectation:
            for _ in range(50):
                status = expected_window_status(expectation)
                if status == "placed":
                    placed = True
                    break
                if status in {"expired", "cancelled", "failed", "unknown"}:
                    break
                time.sleep(0.1)
            if not placed:
                cancel_expected_window(expectation)
                window_id = (result or {}).get("window_id")
                if window_id is not None:
                    try:
                        request_browser(
                            "close_restored_window",
                            {
                                "window_id": window_id,
                                "restore_token": (
                                    f"{restore_token_prefix}:{profile_name}:{label}"
                                    if restore_token_prefix else None
                                ),
                            },
                            profile=profile_name,
                        )
                    except BrowserUnavailable:
                        pass
        warning_count = len((result or {}).get("warnings", []))
        suffix = ""
        if expectation and not placed:
            suffix = "; placement failed"
        if warning_count:
            suffix += f"; {warning_count} tab warning(s)"
        actions.append(BrowserRestoreResult(
            f"restored Chrome {profile_name}/{label} ({tab_count} tabs{suffix})",
            not expectation or placed,
        ))
    return actions
