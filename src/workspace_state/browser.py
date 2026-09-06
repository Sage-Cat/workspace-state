from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .desktop import (
    cancel_expected_window,
    capture_shell,
    expect_window,
    expected_window_status,
    move_window_result,
    remap_monitor,
    remap_workspace,
    workspace_names,
)
from .util import CommandError, launch_graphical_service


class BrowserUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserRestoreResult:
    message: str
    success: bool = True


SUPPORTED_BROWSER_COMMANDS = {
    "google-chrome": "google-chrome",
}
BROWSER_PROTOCOL_VERSION = 2
BROWSER_REQUIRED_CAPABILITIES = {
    "list_windows",
    "restore_window",
    "restore_status",
    "identify_window",
    "focus_window",
    "release_window_identification",
    "close_restored_window",
    "scoped_creation_marker",
}
BROWSER_SETTLE_SECONDS = 2.0
NATIVE_WINDOW_TIMEOUT = 5.0


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


def browser_companion_info(profile: str) -> dict[str, Any]:
    result = request_browser("ping", profile=profile, timeout=2)
    return result if isinstance(result, dict) else {}


def ensure_browser_profiles(chrome: dict[str, Any], *, timeout: float = 15) -> list[str]:
    """Start saved profiles and let Chrome restore its own previous session first."""
    required = {
        str(profile.get("profile") or "Default")
        for profile in chrome.get("profiles", [])
    }
    missing = required - set(connected_profiles())
    if not missing:
        return []

    launched: list[str] = []
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
        try:
            launch_graphical_service(
                [
                    command,
                    f"--profile-directory={profile_directory}",
                    "--restore-last-session",
                ],
                f"chrome-{profile_directory}",
            )
        except CommandError as error:
            raise BrowserUnavailable(str(error)) from error
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


def wait_for_browser_settle(
    profiles: set[str],
    *,
    timeout: float = 15,
    stable_for: float = BROWSER_SETTLE_SECONDS,
) -> None:
    """Wait until Chrome's native session restoration stops changing windows."""
    if not profiles:
        return
    deadline = time.monotonic() + max(0.0, timeout)
    previous: dict[str, str] = {}
    stable_since: dict[str, float] = {}
    while True:
        now = time.monotonic()
        for profile in sorted(profiles):
            windows = request_browser("list_windows", {}, profile=profile, timeout=2)
            value = json.dumps(windows, sort_keys=True, separators=(",", ":"))
            if previous.get(profile) != value:
                previous[profile] = value
                stable_since[profile] = now
        if all(now - stable_since.get(profile, now) >= stable_for for profile in profiles):
            return
        if now >= deadline:
            raise BrowserUnavailable(
                "Chrome session restoration did not settle for profile(s): "
                + ", ".join(sorted(profiles))
            )
        time.sleep(0.1)


def _normalized_app_id(value: str) -> str:
    result = value.casefold().strip()
    return result.removesuffix(".desktop")


def _shell_app_ids(window: dict[str, Any]) -> set[str]:
    result = {
        _normalized_app_id(str(window.get(key) or ""))
        for key in ("app_id", "wm_class", "wm_class_instance", "sandboxed_app_id")
        if window.get(key)
    }
    result.update(
        _normalized_app_id(str(value))
        for value in window.get("app_ids", [])
        if value
    )
    return result


def _looks_like_chrome(window: dict[str, Any]) -> bool:
    return any(
        "chrome" in value or "chromium" in value
        for value in _shell_app_ids(window)
    )


def _shell_browser_windows(shell: dict[str, Any], app_id: str) -> list[dict[str, Any]]:
    normalized = _normalized_app_id(app_id)
    return [
        window for window in shell.get("windows", [])
        if normalized in _shell_app_ids(window) or _looks_like_chrome(window)
    ]


def _window_matches_resolved_placement(
    window: dict[str, Any],
    target: dict[str, Any],
) -> bool:
    def integer(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    if integer(window.get("workspace"), -1) != integer(target.get("workspace"), -2):
        return False
    if integer(window.get("monitor"), -1) != integer(target.get("monitor"), -2):
        return False
    state = str(target.get("state") or "normal")
    if str(window.get("state") or "normal") != state:
        return False
    if state != "normal":
        return True
    # gnome-winctl's resolved_target geometry is in the compositor's global
    # coordinate space. Comparing it with geometry_relative works only when a
    # monitor happens to start at (0, 0); normal/popup Chrome windows on an
    # offset monitor would be moved correctly and then reported as failed.
    # A raw monitor-space fallback is still accepted explicitly for callers
    # which have not gone through gnome-winctl target normalization.
    if target.get("coordinate_space") == "monitor":
        actual = window.get("geometry_relative") or window.get("geometry") or {}
    else:
        actual = window.get("geometry") or window.get("geometry_relative") or {}
    expected = target.get("geometry") or {}
    return all(
        abs(integer(actual.get(key), 0) - integer(expected.get(key), 0)) <= 8
        for key in ("x", "y", "width", "height")
    )


def _wait_for_native_placement(
    native_id: int,
    target: dict[str, Any],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        shell = capture_shell()
        window = next(
            (
                candidate for candidate in shell.get("windows", [])
                if int(candidate.get("id", -1)) == native_id
            ),
            None,
        )
        if window is not None and _window_matches_resolved_placement(window, target):
            return True
        time.sleep(0.05)
    return False


def _identify_native_window(
    *,
    profile: str,
    chrome_window_id: int,
    app_id: str,
    window_type: str = "normal",
    timeout: float = NATIVE_WINDOW_TIMEOUT,
) -> tuple[int, dict[str, Any]]:
    token = uuid.uuid4().hex
    if window_type == "popup":
        # Chrome may silently create a requested tab in a normal window even
        # when tabs.create receives a popup windowId. Never navigate or add a
        # marker to a popup: it may contain a non-recoverable blob/payment
        # document. Focus plus the popup's active title gives us a safe exact
        # native mapping without touching its tab contents.
        summary = request_browser(
            "focus_window",
            {"window_id": chrome_window_id},
            profile=profile,
            timeout=2,
        )
        if not isinstance(summary, dict):
            raise BrowserUnavailable("Chrome returned an invalid popup summary")
        expected_title = str(summary.get("active_title") or "").casefold()
        bounds = summary.get("bounds") or {}
        identification = {
            "window_id": chrome_window_id,
            "marker_tab_id": None,
            "previous_active_tab_id": None,
            "token": token,
            "strategy": "focused_popup",
        }
        deadline = time.monotonic() + timeout
        consecutive_id: int | None = None
        consecutive_samples = 0
        attempt = 0
        while time.monotonic() < deadline:
            candidates = _shell_browser_windows(capture_shell(), app_id)
            titled = [
                window for window in candidates
                if expected_title and expected_title in str(window.get("title") or "").casefold()
            ]
            active = [window for window in (titled or candidates) if window.get("active")]
            matches = active if len(active) == 1 else titled if len(titled) == 1 else []
            if not matches and len(titled) > 1:
                # Focus may be denied for an inactive workspace. A uniquely
                # closest decorated frame is a deterministic fallback when
                # more than one popup has the same page title.
                scored = sorted(
                    (
                        abs(int((window.get("geometry") or {}).get("width", 0)) - int(bounds.get("width", 0)))
                        + abs(int((window.get("geometry") or {}).get("height", 0)) - int(bounds.get("height", 0))),
                        int(window.get("id", -1)),
                        window,
                    )
                    for window in titled
                )
                if len(scored) == 1 or scored[0][0] < scored[1][0]:
                    matches = [scored[0][2]]
            if len(matches) == 1:
                window_id = int(matches[0]["id"])
                if window_id == consecutive_id:
                    consecutive_samples += 1
                else:
                    consecutive_id = window_id
                    consecutive_samples = 1
                if consecutive_samples >= 2:
                    return window_id, identification
            else:
                consecutive_id = None
                consecutive_samples = 0
            attempt += 1
            if attempt % 10 == 0:
                request_browser(
                    "focus_window",
                    {"window_id": chrome_window_id},
                    profile=profile,
                    timeout=2,
                )
            time.sleep(0.05)
        raise BrowserUnavailable("focused Chrome popup did not map to one GNOME window")

    identification = request_browser(
        "identify_window",
        {"window_id": chrome_window_id, "token": token},
        profile=profile,
    )
    if not isinstance(identification, dict):
        raise BrowserUnavailable("Chrome returned an invalid window identification")
    deadline = time.monotonic() + timeout
    consecutive_id: int | None = None
    consecutive_samples = 0
    attempt = 0
    while time.monotonic() < deadline:
        marked = [
            window for window in _shell_browser_windows(capture_shell(), app_id)
            if token in str(window.get("title") or "")
        ]
        active = [window for window in marked if window.get("active")]
        # Chrome focus is authoritative on the active workspace. Mutter does
        # not activate a window which an expectation has already moved to an
        # inactive workspace, but the private UUID marker still uniquely binds
        # that Chrome API window to one stable GNOME ID.
        candidates = active if active else marked
        if len(candidates) == 1:
            window_id = int(candidates[0]["id"])
            if window_id == consecutive_id:
                consecutive_samples += 1
            else:
                consecutive_id = window_id
                consecutive_samples = 1
            if consecutive_samples >= 2:
                return window_id, identification
        else:
            consecutive_id = None
            consecutive_samples = 0
        attempt += 1
        if attempt % 10 == 0:
            request_browser(
                "focus_window",
                {"window_id": chrome_window_id},
                profile=profile,
                timeout=2,
            )
        time.sleep(0.05)
    try:
        request_browser(
            "release_window_identification",
            {**identification, "focus": False},
            profile=profile,
            timeout=2,
        )
    except BrowserUnavailable:
        pass
    raise BrowserUnavailable("focused Chrome window did not map to one GNOME window")


def _place_browser_window(
    *,
    profile: str,
    chrome_window_id: int,
    app_id: str,
    placement: dict[str, Any],
    window_type: str = "normal",
    timeout: float = NATIVE_WINDOW_TIMEOUT,
) -> bool:
    identification: dict[str, Any] | None = None
    released = False
    try:
        native_id, identification = _identify_native_window(
            profile=profile,
            chrome_window_id=chrome_window_id,
            app_id=app_id,
            window_type=window_type,
            timeout=timeout,
        )
        shell_before = capture_shell()
        try:
            active_workspace = int(shell_before.get("active_workspace"))
            target_workspace = int(placement.get("workspace"))
        except (TypeError, ValueError):
            active_workspace = target_workspace = -1
        if active_workspace >= 0 and active_workspace != target_workspace:
            # Mutter defers monitor/state/geometry changes for inactive
            # workspaces. Apply those values to this exact stable ID on the
            # active workspace first, then move the already-correct window to
            # its saved workspace. The final deferred record remains useful for
            # topology recovery but no longer leaves an initially wrong display.
            staging = dict(placement)
            staging["workspace"] = active_workspace
            staging.pop("workspace_name", None)
            staged = move_window_result(native_id, staging)
            if not staged.get("placed"):
                return False
            staged_target = staged.get("resolved_target") or staging
            if not _wait_for_native_placement(native_id, staged_target, timeout):
                return False
        result = move_window_result(native_id, placement)
        if not result.get("placed"):
            return False
        request_browser(
            "release_window_identification",
            {
                **identification,
                "focus": bool(result.get("deferred")),
            },
            profile=profile,
        )
        released = True
        if result.get("deferred"):
            request_browser(
                "focus_window",
                {"window_id": chrome_window_id},
                profile=profile,
            )
        resolved = result.get("resolved_target") or placement
        if _wait_for_native_placement(native_id, resolved, timeout):
            return True
        # Mutter cannot apply monitor geometry/state for a window on an
        # inactive workspace. gnome-winctl has already moved the exact stable
        # window ID to that workspace and retained an authoritative deferred
        # placement which it applies as soon as the workspace becomes active.
        # Treat that durable handoff as success instead of deleting a newly
        # restored Chrome window merely because its workspace stayed inactive.
        return bool(result.get("deferred"))
    finally:
        if identification is not None and not released:
            try:
                request_browser(
                    "release_window_identification",
                    {**identification, "focus": False},
                    profile=profile,
                    timeout=2,
                )
            except BrowserUnavailable:
                pass


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
        for key in (
            "connector", "edid_hash", "edid_checksum", "vendor", "product", "serial",
        )
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
    operation_token_prefix = restore_token_prefix or uuid.uuid4().hex
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
        app_id = str(window.get("app_id") or profile.get("app_id") or "google-chrome")
        expectation = None
        creation_token = None
        if place and placement:
            placement = remap_monitor(remap_workspace(placement))
            creation_token = uuid.uuid4().hex
            expectation = expect_window(
                app_id,
                placement,
                title=f"wsctl-create:{creation_token} - Google Chrome",
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
        restore_token = f"{operation_token_prefix}:{profile_name}:{label}"
        try:
            result = request_browser(
                "restore_window",
                {
                    "window": window,
                    "place_expected": bool(expectation),
                    "restore_token": restore_token,
                    "creation_token": creation_token,
                },
                profile=profile_name,
            )
        except Exception:
            if expectation:
                cancel_expected_window(expectation)
            raise

        if not isinstance(result, dict) or not isinstance(result.get("window_id"), int):
            if expectation:
                cancel_expected_window(expectation)
            raise BrowserUnavailable("Chrome did not identify the restored browser window")
        reused = bool((result or {}).get("reused"))
        expectation_placed = not expectation or reused
        if expectation and reused:
            cancel_expected_window(expectation)
        elif expectation:
            for _ in range(50):
                status = expected_window_status(expectation)
                if status == "placed":
                    expectation_placed = True
                    break
                if status in {"expired", "cancelled", "failed", "unknown"}:
                    break
                time.sleep(0.1)
            if not expectation_placed:
                cancel_expected_window(expectation)
        placed = not place
        if place and placement:
            try:
                placed = _place_browser_window(
                    profile=profile_name,
                    chrome_window_id=result["window_id"],
                    app_id=app_id,
                    placement=placement,
                    window_type=str(window.get("type") or "normal"),
                )
            except BrowserUnavailable:
                placed = False
        if place and not placed and result.get("created"):
            try:
                request_browser(
                    "close_restored_window",
                    {
                        "window_id": result["window_id"],
                        "restore_token": restore_token,
                        "created": True,
                    },
                    profile=profile_name,
                )
            except BrowserUnavailable:
                pass
        warning_count = len((result or {}).get("warnings", []))
        suffix = ""
        if place and not placed:
            suffix = "; placement failed"
        elif expectation and not expectation_placed and placed:
            suffix = "; placed after expectation retry"
        if warning_count:
            suffix += f"; {warning_count} tab warning(s)"
        verb = "reused open" if reused else "restored"
        actions.append(BrowserRestoreResult(
            f"{verb} Chrome {profile_name}/{label} ({tab_count} tabs{suffix})",
            not place or placed,
        ))
    return actions
