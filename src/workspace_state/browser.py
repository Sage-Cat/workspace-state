from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .desktop import (
    serialized_placement,
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
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, placement_accepted, placement_matches)


class BrowserUnavailable(RuntimeError):
    pass


class BrowserPlacementPending(BrowserUnavailable):
    """The compositor accepted placement but has not verified its outcome."""
    def __init__(self, message: str, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


@dataclass(frozen=True)
class BrowserRestoreResult:
    message: str
    success: bool = True
    evidence: ProviderItemResult | None = field(default=None, compare=False)


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
    "exact_url_restore",
    "exact_url_pending",
    "reuse_only_groups",
    "original_groups_required",
    "unclaimed_original_guard",
    "lazy_tab_restore",
    "exact_capture_identity",
    "native_mutation_status",
    "reconciliation_reuse_only",
}
BROWSER_SETTLE_SECONDS = 2.0
NATIVE_WINDOW_TIMEOUT = 5.0
CONTENT_PLACEMENT_PREFIX = "chrome-content:"


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
    deadline = time.monotonic() + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("native host request deadline exceeded")
        connection.settimeout(left)
    try:
        remaining()
        connection.connect(str(path))
        remaining()
        connection.sendall(request.encode("utf-8") + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            remaining()
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


def wait_for_quiescence(*, timeout: float = 10, release_expired: bool = False) -> None:
    """Observe work; the stopped-worker barrier may release expired owned markers."""
    deadline = time.monotonic() + timeout
    detail = "Chrome quiescence was not verified"
    cleanup_attempted: set[Path] = set()
    while time.monotonic() < deadline:
        paths = _host_paths()
        shell = capture_shell(timeout=min(1, max(.001, deadline - time.monotonic())))
        native_windows = _shell_browser_windows(shell, 'google-chrome')
        if not paths and shell.get('available') and not native_windows:
            return
        idle = bool(paths) and bool(shell.get('available'))
        window_count = 0
        for path in paths:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                idle = False
                break
            try:
                value = _request_path(path, 'ping', timeout=min(1, remaining))
                if not isinstance(value, dict) or 'native_mutation_status' not in value.get('capabilities', []):
                    raise BrowserUnavailable('Chrome companion lacks native mutation status; reload the companion')
                counts = [value.get(key) for key in ('active_mutations', 'active_identifications', 'window_count')]
                if any(type(count) is not int or count < 0 for count in counts):
                    raise BrowserUnavailable('Chrome companion returned invalid mutation status')
                window_count += counts[2]
                if counts[0] or counts[1]:
                    idle = False
                    detail = f"Chrome still has {counts[0]} native mutation(s) and {counts[1]} identification lease(s)"
                expired = value.get('expired_identifications')
                if (release_expired and counts[0] == 0 and type(expired) is int
                        and 0 < expired <= counts[1] and path not in cleanup_attempted
                        and 'identification_lease_lifecycle' in value.get('capabilities', [])):
                    # Cleanup validates ownership, expiry and the exact temporary
                    # URL in the companion. Never close a live or unknown marker,
                    # and never treat a cleanup response as placement/capture proof.
                    cleanup_attempted.add(path)
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        _request_path(path, 'release_expired_identifications', timeout=min(1, remaining))
            except BrowserUnavailable as error:
                idle, detail = False, str(error)
        if idle and window_count == len(native_windows):
            return
        if not shell.get('available'):
            detail = 'GNOME window state is unavailable during Chrome quiescence verification'
        elif not paths or (idle and window_count != len(native_windows)):
            detail = 'Not every native Chrome window has an observable companion'
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.2, remaining))
    raise BrowserUnavailable(f'Chrome did not become quiescent: {detail}')


def browser_companion_info(profile: str, *, timeout: float = 10) -> dict[str, Any]:
    """Wait through a bounded extension reload before permitting restore work."""
    deadline = time.monotonic() + timeout
    detail = "Chrome companion did not become ready"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrowserUnavailable(detail)
        try:
            result = request_browser("ping", profile=profile, timeout=min(2, remaining))
            result = result if isinstance(result, dict) else {}
            if result.get("activation_pending") is not True:
                return result
            detail = (f"Chrome companion for profile {profile!r} has activation pending; "
                      "finish current restoration and reload the companion before browser restore")
        except BrowserUnavailable as error:
            detail = str(error)
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


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


def _browser_session_signature(windows: Any, profile: str) -> str:
    if not isinstance(windows, list) or any(
        not isinstance(window, dict) or not isinstance(window.get("id"), int)
        for window in windows
    ):
        raise BrowserUnavailable(f"Chrome returned an invalid window list for profile {profile!r}")
    # Loading flags, titles, focus and desktop geometry can change indefinitely
    # after Chrome has restored its session. Only membership and ordered tab
    # identity matter here; exact loaded URLs are verified during restore.
    identity = [
        {
            "id": window["id"],
            "signature": window.get("full_signature", window.get("signature")),
            "tabs": window.get("tabs"),
        }
        for window in sorted(windows, key=lambda window: window["id"])
    ]
    return json.dumps(identity, sort_keys=True, separators=(",", ":"))


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
            value = _browser_session_signature(windows, profile)
            if previous.get(profile) != value:
                previous[profile] = value
                stable_since[profile] = now
        if all(now - stable_since.get(profile, now) >= stable_for for profile in profiles):
            return
        if now >= deadline:
            unsettled = sorted(
                profile for profile in profiles
                if now - stable_since.get(profile, now) < stable_for
            )
            raise BrowserUnavailable(
                "Chrome session restoration did not settle for profile(s): "
                + ", ".join(unsettled)
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
    return placement_matches(window, target, tolerance=8)


def _wait_for_native_placement(
    native_id: int,
    target: dict[str, Any],
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
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
            now = time.monotonic()
            stable_since = now if stable_since is None else stable_since
            # Unmaximize and Wayland buffer acknowledgements can briefly
            # expose the requested frame before a late resize changes it.
            if now - stable_since >= 0.4:
                return True
        else:
            stable_since = None
        time.sleep(0.05)
    return False


def _release_window_identification(profile: str, identification: dict[str, Any]) -> None:
    result = request_browser(
        "release_window_identification",
        {**identification, "focus": False},
        profile=profile,
        timeout=2,
    )
    if not isinstance(result, dict) or result.get("released") is not True:
        reason = result.get("reason") if isinstance(result, dict) else None
        raise BrowserUnavailable(
            "Chrome window identification cleanup was not confirmed"
            + (f": {reason}" if reason else "")
        )


def _identify_native_window(
    *,
    profile: str,
    chrome_window_id: int,
    app_id: str,
    window_type: str = "normal",
    timeout: float = NATIVE_WINDOW_TIMEOUT,
    preserve_focus: bool = False,
    authority_guard: Callable[[], None] | None = None,
) -> tuple[int, dict[str, Any]]:
    if authority_guard:
        authority_guard()
    token = uuid.uuid4().hex
    if window_type == "popup":
        # Chrome may silently create a requested tab in a normal window even
        # when tabs.create receives a popup windowId. Never navigate or add a
        # marker to a popup: it may contain a non-recoverable blob/payment
        # document. Focus plus the popup's active title gives us a safe exact
        # native mapping without touching its tab contents.
        if preserve_focus:
            summaries = request_browser("list_windows", profile=profile, timeout=2)
            summary = next((item for item in summaries if item.get("id") == chrome_window_id), None)
            if not summary or not summary.get("focused"):
                raise BrowserUnavailable("Inactive Chrome popup cannot be captured without changing desktop focus; checkpoint preserved")
        else:
            summary = request_browser("focus_window", {"window_id": chrome_window_id}, profile=profile, timeout=2)
        if authority_guard:
            authority_guard()
        if not isinstance(summary, dict):
            raise BrowserUnavailable("Chrome returned an invalid popup summary")
        expected_title = str(summary.get("active_title") or "").casefold()
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
            if authority_guard:
                authority_guard()
            titled = [
                window for window in candidates
                if expected_title and expected_title in str(window.get("title") or "").casefold()
            ]
            active = [window for window in titled if window.get("active")]
            matches = active if len(active) == 1 else []
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
            if attempt % 10 == 0 and not preserve_focus:
                if authority_guard:
                    authority_guard()
                request_browser(
                    "focus_window",
                    {"window_id": chrome_window_id},
                    profile=profile,
                    timeout=2,
                )
                if authority_guard:
                    authority_guard()
            time.sleep(0.05)
        raise BrowserUnavailable("focused Chrome popup did not map to one GNOME window")

    # The caller owns this token before the RPC: Chrome may create its marker
    # even when the reply is lost. Never use an unvalidated reply for cleanup.
    identification = {"window_id": chrome_window_id, "token": token}
    matched = False
    try:
        if authority_guard:
            authority_guard()
        response = request_browser(
            "identify_window",
            {**identification, "focus": not preserve_focus},
            profile=profile,
            timeout=min(2.0, timeout),
        )
        if authority_guard:
            authority_guard()
        if (not isinstance(response, dict) or type(response.get("window_id")) is not int
                or response["window_id"] != chrome_window_id or response.get("token") != token):
            raise BrowserUnavailable("Chrome returned an invalid window identification")
        identification = response
        deadline = time.monotonic() + timeout
        consecutive_id: int | None = None
        consecutive_samples = 0
        attempt = 0
        while time.monotonic() < deadline:
            marked = [
                window for window in _shell_browser_windows(capture_shell(), app_id)
                if token in str(window.get("title") or "")
            ]
            if authority_guard:
                authority_guard()
            active = [window for window in marked if window.get("active")]
            # A private UUID also binds a window on an inactive workspace,
            # where Mutter will not make Chrome's focus request active.
            candidates = active if active else marked
            if len(candidates) == 1:
                window_id = int(candidates[0]["id"])
                if window_id == consecutive_id:
                    consecutive_samples += 1
                else:
                    consecutive_id = window_id
                    consecutive_samples = 1
                if consecutive_samples >= 2:
                    matched = True
                    return window_id, identification
            else:
                consecutive_id = None
                consecutive_samples = 0
            attempt += 1
            if attempt % 10 == 0 and not preserve_focus:
                if authority_guard:
                    authority_guard()
                request_browser(
                    "focus_window",
                    {"window_id": chrome_window_id},
                    profile=profile,
                    timeout=2,
                )
                if authority_guard:
                    authority_guard()
            time.sleep(0.05)
        raise BrowserUnavailable("focused Chrome window did not map to one GNOME window")
    finally:
        if not matched:
            try:
                _release_window_identification(profile, identification)
            except BrowserUnavailable:
                pass  # Identification still fails; the companion retains its lease.


@serialized_placement
def _place_browser_window(
    *,
    profile: str,
    chrome_window_id: int,
    app_id: str,
    placement: dict[str, Any],
    window_type: str = "normal",
    timeout: float = NATIVE_WINDOW_TIMEOUT,
    authority_guard: Callable[[], None] | None = None,
) -> bool:
    identification: dict[str, Any] | None = None
    released = False
    try:
        if authority_guard:
            authority_guard()
        native_id, identification = _identify_native_window(
            profile=profile,
            chrome_window_id=chrome_window_id,
            app_id=app_id,
            window_type=window_type,
            timeout=timeout,
            **({"authority_guard": authority_guard} if authority_guard else {}),
        )
        # The stable native ID now owns the mapping. Restore the original tab
        # before resizing, so marker cleanup cannot change the client's frame
        # after it has been handed to an inactive workspace.
        _release_window_identification(profile, identification)
        released = True
        if authority_guard:
            authority_guard()
        shell_before = capture_shell()
        if authority_guard:
            authority_guard()
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
            if authority_guard:
                authority_guard()
            staged = move_window_result(native_id, staging)
            if authority_guard:
                authority_guard()
            if not placement_accepted(staged):
                return False
            staged_target = staged.get("resolved_target") or staging
            if not _wait_for_native_placement(native_id, staged_target, timeout):
                # A staging receipt proves only this temporary workspace. The
                # read-only observer cannot issue the still-missing handoff,
                # so never publish that token as final placement evidence.
                raise BrowserUnavailable(
                    "Chrome staging did not settle; final workspace placement was not submitted"
                )
        if authority_guard:
            authority_guard()
        result = move_window_result(native_id, placement)
        if authority_guard:
            authority_guard()
        if not placement_accepted(result):
            return False
        resolved = result.get("resolved_target") or placement
        verified = _wait_for_native_placement(native_id, resolved, timeout)
        if authority_guard:
            authority_guard()
        if verified:
            return True
        if result.get("status") in {"accepted", "deferred", "applied"} or result.get("deferred"):
            raise BrowserPlacementPending("Chrome placement is accepted and awaiting compositor verification", result.get("token"))
        return False
    finally:
        if identification is not None and not released:
            try:
                _release_window_identification(profile, identification)
            except BrowserUnavailable:
                pass



@serialized_placement
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
    matches: dict[int, dict[str, Any]] = {}
    used_shell: set[int] = set()
    native_by_id = {window.get("id"): window for window in shell_windows}
    for index, (profile, window) in enumerate(browser_windows):
        runtime_id = window.pop("runtime_window_id", None)
        if type(runtime_id) is not int:
            raise BrowserUnavailable("Chrome companion lacks exact capture identity; reload it before saving")
        identification = None
        try:
            native_id, identification = _identify_native_window(
                profile=str(profile.get("profile") or "Default"), chrome_window_id=runtime_id,
                app_id=str(profile.get("app_id") or "google-chrome"),
                window_type=str(window.get("type") or "normal"), preserve_focus=True,
            )
            if native_id not in native_by_id or native_id in used_shell:
                raise BrowserUnavailable("Chrome capture identity changed or mapped multiple windows to one native window")
            matches[index] = native_by_id[native_id]
            used_shell.add(native_id)
        finally:
            if identification is not None:
                _release_window_identification(str(profile.get("profile") or "Default"), identification)

    if used_shell != set(native_by_id):
        raise BrowserUnavailable("Not every native Chrome window had an exact companion identity; checkpoint preserved")

    for index, (profile, window) in enumerate(browser_windows):
        placement = matches[index]
        window.setdefault("app_id", profile.get("app_id") or "google-chrome")
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


def browser_continuation_guard(owner) -> None:
    """Re-check mutation authority, including suspension, after every blocking reply."""
    from . import operations, login_status
    from .startup import startup_suspended
    owner.check()
    document = json.loads(login_status.status_path().read_text())
    if (owner.mode != "startup" or operations.current() != owner or not owner.matches(document)
            or document.get("operation_state") != "running"
            or startup_suspended(runtime_dir(), owner.boot_id, owner.login_generation)):
        raise BrowserUnavailable("Chrome placement continuation no longer owns the startup operation")


def _continuation_path(token: str) -> Path:
    key = token.removeprefix(CONTENT_PLACEMENT_PREFIX)
    if (not token.startswith(CONTENT_PLACEMENT_PREFIX) or len(key) != 64
            or any(char not in "0123456789abcdef" for char in key)):
        raise BrowserUnavailable("Invalid Chrome placement continuation identity")
    return runtime_dir() / "browser-continuations" / f"{key}.json"


def _defer_browser_placement(*, profile, window, restore_token, catalog) -> str:
    """Persist only an exact reuse-only handoff; this does not submit placement."""
    from . import operations
    from .browser_reconciliation import digest
    from .util import atomic_json
    owner = operations.current()
    if owner is None:
        raise BrowserUnavailable("Chrome content is loading; no owned startup continuation is available")
    browser_continuation_guard(owner)
    payload = {"schema_version": 1, "operation_context": owner.to_dict(), "profile": profile,
               "window": window, "restore_token": restore_token, "catalog": catalog}
    token = CONTENT_PLACEMENT_PREFIX + digest(payload)
    path = _continuation_path(token)
    if not path.exists():
        atomic_json(path, {"payload": payload, "state": "awaiting-content"})
    browser_continuation_guard(owner)
    return token


def continue_browser_placement(token: str, owner, *, budget_seconds: float = 15.0) -> dict[str, Any]:
    """One owned handoff after content verification; never replay restore_window."""
    from .browser_reconciliation import digest, profiles_by_name, window_signature
    from .util import atomic_json
    path = _continuation_path(token)
    record = json.loads(path.read_text())
    payload = record.get("payload", {})
    if (not isinstance(payload, dict) or payload.get("schema_version") != 1
            or payload.get("operation_context") != owner.to_dict()
            or CONTENT_PLACEMENT_PREFIX + digest(payload) != token):
        raise BrowserUnavailable("Chrome continuation evidence changed or belongs to another operation")
    browser_continuation_guard(owner)
    if record.get("result"):
        # A lost publication reply must not repeat an already-submitted move.
        return record["result"]
    end = time.monotonic() + min(15.0, max(0.0, budget_seconds), owner.remaining())
    def guard():
        browser_continuation_guard(owner)
        if time.monotonic() >= end:
            raise TimeoutError("Chrome placement continuation budget expired")
    def request(action, body, profile):
        guard()
        result = request_browser(action, body, profile=profile,
                                 timeout=min(2.0, max(.001, end-time.monotonic())))
        guard()
        return result
    profile, window = payload["profile"], payload["window"]
    window_id = window.get("_reconcile_window_id")
    if type(window_id) is not int:
        raise BrowserUnavailable("Chrome continuation lacks exact native identity")
    status = request("restore_status", {"restore_token": payload["restore_token"]}, profile)
    if (not isinstance(status, dict) or status.get("exists") is not True
            or status.get("window_id") != window_id):
        raise BrowserUnavailable("The claimed Chrome window changed; placement was not submitted")
    if status.get("group_warnings"):
        raise BrowserUnavailable("; ".join(status["group_warnings"]))
    if status.get("urls_restored") is not True:
        if status.get("urls_pending") is True:
            return {"token": token, "status": "waiting", "detail": "Placement awaits exact loaded tab URLs"}
        raise BrowserUnavailable("; ".join(status.get("url_errors") or ["Exact tab URLs changed; placement was not submitted"]))
    # A claim alone cannot prove the complete reconciliation catalog is intact.
    # Re-check every native ID/URL/group before the first placement mutation.
    profiles = profiles_by_name(payload["catalog"])
    if set(connected_profiles()) != set(profiles):
        raise BrowserUnavailable("Native Chrome profile inventory changed; placement was not submitted")
    for name, expected in profiles.items():
        live = request("capture", {}, name)
        if (not isinstance(live, dict) or any(live.get(key) != expected.get(key)
                for key in ("profile", "profile_directory", "app_id"))):
            raise BrowserUnavailable("Native Chrome profile identity changed; placement was not submitted")
        windows = live.get("windows", [])
        by_id = {item.get("runtime_window_id"): item for item in windows}
        if len(by_id) != len(windows) or len(windows) != len(expected["windows"]):
            raise BrowserUnavailable("Native Chrome window inventory changed; placement was not submitted")
        for saved in expected["windows"]:
            actual = by_id.get(saved.get("_reconcile_window_id"))
            if actual is None or window_signature(actual) != window_signature(saved):
                raise BrowserUnavailable("Native Chrome URLs or groups changed; placement was not submitted")
    placement = browser_window_placement(window)
    if placement is None:
        raise BrowserUnavailable("Chrome continuation lacks saved desktop placement")
    guard()
    placement = remap_monitor(remap_workspace(placement))
    guard()
    try:
        placed = _place_browser_window(profile=profile, chrome_window_id=window_id,
            app_id=str(window.get("app_id") or profiles[profile].get("app_id") or "google-chrome"),
            placement=placement, window_type=str(window.get("type") or "normal"),
            timeout=min(NATIVE_WINDOW_TIMEOUT, max(.001, end-time.monotonic())),
            authority_guard=lambda: browser_continuation_guard(owner))
        result = {"token":token, "status":"verified" if placed else "failed", "content_verified":True,
                  "detail":"Exact Chrome native placement verified" if placed else
                           "GNOME rejected the exact Chrome placement request; window preserved"}
    except BrowserPlacementPending as error:
        result = {"token":token, "status":"submitted" if error.request_id else "failed",
                  "placement_request":error.request_id, "content_verified":True,
                  "detail":str(error) if error.request_id else "GNOME accepted Chrome placement without a verifiable request identity"}
    # An accepted move must retain its receipt even if the finite preflight
    # budget elapsed while the compositor settled it. Startup authority is
    # still mandatory; cancellation/deadline always revokes publication.
    browser_continuation_guard(owner)
    record.update(state="placement-submitted", result=result)
    atomic_json(path, record)
    return result


def restore_browser(
    chrome: dict[str, Any],
    *,
    workspace: str | None = None,
    place: bool = True,
    dry_run: bool = False,
    restore_token_prefix: str | None = None,
    restore_catalog: dict[str, Any] | None = None,
    authority_guard: Callable[[], None] | None = None,
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
    catalogs: dict[str, list[dict[str, Any]]] = {}
    for profile, window in browser_windows(restore_catalog if restore_catalog is not None else chrome):
        profile_name = str(profile.get("profile") or "Default")
        label = str(window.get("id") or "window")
        catalogs.setdefault(profile_name, []).append({
            "restore_token": f"{operation_token_prefix}:{profile_name}:{label}",
            "window": window,
        })
    for profile, window in selected:
        profile_name = str(profile.get("profile") or "Default")
        tab_count = len(window.get("tabs", []))
        label = str(window.get("id") or "window")
        if dry_run:
            actions.append(BrowserRestoreResult(
                f"restore Chrome {profile_name}/{label} ({tab_count} tabs)",
            ))
            continue

        if authority_guard:
            authority_guard()
        placement = browser_window_placement(window)
        app_id = str(window.get("app_id") or profile.get("app_id") or "google-chrome")
        expectation = None
        creation_token = None
        if place and placement:
            placement = remap_monitor(remap_workspace(placement))
            creation_token = uuid.uuid4().hex
            if authority_guard:
                authority_guard()
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
            if authority_guard:
                authority_guard()
            result = request_browser(
                "restore_window",
                {
                    "window": window,
                    "place_expected": bool(expectation),
                    "restore_token": restore_token,
                    "restore_catalog": catalogs[profile_name],
                    "creation_token": creation_token,
                    **({"reuse_only": True, "expected_window_id": window["_reconcile_window_id"]}
                       if "_reconcile_window_id" in window else {}),
                },
                profile=profile_name,
            )
            if authority_guard:
                authority_guard()
        except Exception:
            if expectation:
                cancel_expected_window(expectation)
            raise

        if not isinstance(result, dict) or not isinstance(result.get("window_id"), int):
            if expectation:
                cancel_expected_window(expectation)
            raise BrowserUnavailable("Chrome did not identify the restored browser window")
        urls_verified = result.get("urls_restored") is True
        urls_waiting = not urls_verified and result.get("urls_pending") is True
        reused = bool((result or {}).get("reused"))
        expectation_placed = not expectation or reused
        if expectation and (reused or not urls_verified):
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
        placement_waiting = False
        pending_request = None
        placement_error = None
        if place and placement and ("_reconcile_window_id" not in window or
                                    (urls_verified and not result.get("group_warnings"))):
            try:
                placed = _place_browser_window(
                    profile=profile_name,
                    chrome_window_id=result["window_id"],
                    app_id=app_id,
                    placement=placement,
                    window_type=str(window.get("type") or "normal"),
                    **({"authority_guard": authority_guard} if authority_guard else {}),
                )
            except BrowserPlacementPending as error:
                placement_waiting = True
                pending_request = error.request_id
                placed = False
            except BrowserUnavailable as error:
                placed = False
                placement_error = str(error)
        elif place and placement and "_reconcile_window_id" in window:
            if urls_waiting and not result.get("group_warnings"):
                try:
                    pending_request = _defer_browser_placement(profile=profile_name, window=window,
                        restore_token=restore_token, catalog=restore_catalog or chrome)
                    placement_waiting = True
                    placement_error = "Native placement awaits exact content verification; no move submitted yet"
                except BrowserUnavailable as error:
                    placement_error = str(error)
            else:
                placement_error = "Native placement was not submitted because exact tab URLs or groups are unverified"
        if urls_verified and place and not placed and not placement_waiting and result.get("created"):
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
        if authority_guard:
            authority_guard()
        group_warnings = tuple(str(item) for item in result.get("group_warnings", []))
        warning_count = len((result or {}).get("warnings", []))
        suffix = ""
        content_gated = bool(pending_request and pending_request.startswith(CONTENT_PLACEMENT_PREFIX))
        if content_gated:
            suffix = "; placement awaits exact content verification"
        elif placement_waiting:
            suffix = "; placement awaiting compositor verification"
        elif place and not placed:
            suffix = "; placement failed"
        elif expectation and not expectation_placed and placed:
            suffix = "; placed after expectation retry"
        if warning_count:
            suffix += f"; {warning_count} tab warning(s)"
        if group_warnings:
            suffix += "; " + "; ".join(group_warnings)
        evidence = ProviderItemResult(
            "chrome", f"{profile_name}/{label}",
            PhaseEvidence(EvidenceState.VERIFIED, f"Chrome window {result['window_id']}"),
            PhaseEvidence(EvidenceState.VERIFIED if urls_verified else
                          EvidenceState.WAITING if urls_waiting else EvidenceState.FAILED,
                          "Exact loaded tab URLs" if urls_verified else "; ".join(result.get("url_errors") or ["Exact tab URLs are unverified"]),
                          urls_waiting,
                          json.dumps([profile_name, restore_token, result["window_id"]], separators=(",", ":"))
                          if urls_waiting else None),
            PhaseEvidence(EvidenceState.SKIPPED if not place else EvidenceState.VERIFIED if placed else
                          EvidenceState.WAITING if placement_waiting else EvidenceState.FAILED,
                          placement_error or "Native placement" + (" pending verification" if placement_waiting else ""), not placed,
                          pending_request),
            attention=group_warnings + (() if urls_verified or urls_waiting else ("Inspect redirected or unavailable tabs",)),
            created=bool(result.get("created")), reused=reused,
        )
        if not urls_verified:
            errors = result.get("url_errors") or []
            detail = f": {errors[0]}" if isinstance(errors, list) and errors else ""
            placement_detail = ("; window placed" if place and placed else
                                "; placement awaits exact content verification" if content_gated else
                                "; placement awaiting compositor verification" if placement_waiting else
                                "; placement failed" if place else "")
            actions.append(BrowserRestoreResult(
                f"Chrome {profile_name}/{label}: exact tab URLs "
                f"{'are still loading' if urls_waiting else 'could not be verified'}{detail}"
                f"{placement_detail}; window preserved for inspection",
                False,
                evidence,
            ))
            continue
        verb = "reused open" if reused else "restored"
        actions.append(BrowserRestoreResult(
            f"{verb} Chrome {profile_name}/{label} ({tab_count} tabs{suffix})",
            (not place or placed) and not group_warnings,
            evidence,
        ))
    return actions
