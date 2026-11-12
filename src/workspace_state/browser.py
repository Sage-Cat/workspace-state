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
    serialized_identification,
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
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, placement_accepted,
                              native_handoff_token)


_serialized_identification = serialized_identification()


class BrowserUnavailable(RuntimeError):
    pass


class BrowserTransportUnavailable(BrowserUnavailable):
    """A native-host transport failed without an authoritative response."""


class BrowserPlacementDeferred(BrowserUnavailable):
    """Native-host transport failed before any placement was submitted."""


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
    "preserve_native_fragment_navigation",
    "asynchronous_restore_verification",
    "cooperative_restore_abort",
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
    check_operation: bool = True,
) -> Any:
    from . import operations
    inherited = operations.current() if check_operation else None
    # Startup cancellation applies to its work/polling, not a later explicit
    # checkpoint's read-only observation and scoped identification cleanup.
    context = inherited if action in {"restore_window", "restore_status"} else None
    if context is not None and context.mode == "startup":
        from .startup_control import check_abort
        check_abort(context)
        if action == "restore_window":
            payload = {**(payload or {}), "operation_context": context.to_dict()}
    request = json.dumps({"action": action, "payload": payload or {}}, separators=(",", ":"))
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + timeout
    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("native host request deadline exceeded")
        if context is not None and context.mode == "startup":
            check_abort(context)
        connection.settimeout(min(left, 0.25))
    try:
        remaining()
        connection.connect(str(path))
        remaining()
        connection.sendall(request.encode("utf-8") + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            remaining()
            try:
                chunk = connection.recv(65536)
            except socket.timeout:
                continue
            if not chunk:
                break
            chunks.extend(chunk)
            if len(chunks) > 64 * 1024 * 1024:
                raise BrowserUnavailable("native host response is too large")
    except (OSError, TimeoutError) as error:
        raise BrowserTransportUnavailable(f"Chrome native host is unavailable: {error}") from error
    finally:
        connection.close()
    if not chunks:
        raise BrowserTransportUnavailable("Chrome native host closed without a response")
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


def connected_profiles(*, strict_transport: bool = False) -> list[str]:
    profiles = []
    for path in _host_paths():
        try:
            result = _request_path(path, "ping", timeout=2)
            profile = str((result or {}).get("profile") or "Default")
            profiles.append(profile)
        except BrowserTransportUnavailable:
            if strict_transport:
                raise
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
                    # Owned startup already requests native recovery; do not
                    # wait behind a crash bubble while creating replacements.
                    "--hide-crash-restore-bubble",
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
        identification = {"window_id": chrome_window_id, "marker_tab_id": None,
                          "previous_active_tab_id": None, "token": token,
                          "strategy": "focused_popup"}
        matched = False
        try:
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
                summary = request_browser("focus_window", {"window_id": chrome_window_id, "token": token}, profile=profile, timeout=2)
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
                        {"window_id": chrome_window_id, "token": token},
                        profile=profile,
                        timeout=2,
                    )
                    if authority_guard:
                        authority_guard()
                time.sleep(0.05)
            raise BrowserUnavailable("focused Chrome popup did not map to one GNOME window")

        finally:
            if not matched and not preserve_focus:
                try:
                    _release_window_identification(profile, identification)
                except BrowserUnavailable:
                    pass

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
                    {"window_id": chrome_window_id, "token": token},
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


@_serialized_identification
def _place_browser_window(
    *,
    profile: str,
    chrome_window_id: int,
    app_id: str,
    placement: dict[str, Any],
    window_type: str = "normal",
    timeout: float = NATIVE_WINDOW_TIMEOUT,
    authority_guard: Callable[[], None] | None = None,
    content_guard: Callable[[], None] | None = None,
) -> bool:
    from . import operations
    owner = operations.current()
    if owner is not None and owner.mode == "startup":
        from .startup_control import check_abort
        check_abort(owner)
    identification: dict[str, Any] | None = None
    released = False
    try:
        if authority_guard:
            authority_guard()
        try:
            native_id, identification = _identify_native_window(
                profile=profile,
                chrome_window_id=chrome_window_id,
                app_id=app_id,
                window_type=window_type,
                timeout=timeout,
                # UUID title markers bind normal windows on inactive workspaces.
                # Focusing one here would interrupt another native configure.
                preserve_focus=window_type != "popup",
                **({"authority_guard": authority_guard} if authority_guard else {}),
            )
            # The stable native ID now owns the mapping. Restore the original tab
            # before resizing, so marker cleanup cannot change the client's frame
            # after it has been handed to an inactive workspace.
            _release_window_identification(profile, identification)
        except BrowserTransportUnavailable as error:
            raise BrowserPlacementDeferred(str(error)) from error
        released = True
        if authority_guard:
            authority_guard()
        shell_before = capture_shell()
        if authority_guard:
            authority_guard()
        if "staged_placement_handoff" not in shell_before.get("capabilities", []):
            raise BrowserUnavailable("GNOME staged placement handoff capability is unavailable")
        if content_guard:
            try:
                content_guard()  # Revalidate after the transient identification tab is gone.
            except BrowserTransportUnavailable as error:
                raise BrowserPlacementDeferred(str(error)) from error
        result = move_window_result(native_id, dict(placement, stage_before_handoff=True))
        if authority_guard:
            authority_guard()
        if not placement_accepted(result):
            return False
        if result.get("status") == "verified":
            return True
        token = native_handoff_token(result)
        if not token:
            raise BrowserUnavailable("GNOME accepted placement without owning its final workspace handoff")
        raise BrowserPlacementPending("Saved placement is owned by GNOME; awaiting compositor verification after unlock", token)
    finally:
        if identification is not None and not released:
            try:
                _release_window_identification(profile, identification)
            except BrowserUnavailable:
                pass



@_serialized_identification
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
    _apply_browser_placements(browser_windows, matches, names)


CHROME_TITLE_SUFFIX = " - Google Chrome"


def _chrome_shell_windows(profiles: list[dict[str, Any]], shell: dict[str, Any]) -> list[dict[str, Any]]:
    configured_ids = {
        _normalized_app_id(str(profile.get("app_id") or "google-chrome"))
        for profile in profiles
    }
    return [
        window for window in shell.get("windows", [])
        if _shell_app_ids(window).intersection(configured_ids) or _looks_like_chrome(window)
    ]


def _active_title(window: dict[str, Any]) -> str:
    return next((str(tab.get("title") or "") for tab in window.get("tabs", [])
                 if isinstance(tab, dict) and tab.get("active")), "")


def _title_matches(chrome_windows: list[dict[str, Any]],
                   shell_windows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """Map each Chrome window to the one native window titled by its active tab.

    Chrome titles a window "<active tab title> - Google Chrome", possibly with
    a profile or media note before the suffix. Any window without exactly one
    candidate, or a native window claimed twice or not at all, is ambiguous.
    """
    matches: dict[int, dict[str, Any]] = {}
    claimed: set[Any] = set()
    for index, window in enumerate(chrome_windows):
        active = _active_title(window)
        titles = [(native, str(native.get("title") or "")) for native in shell_windows]
        exact = [native for native, title in titles if active and title == active + CHROME_TITLE_SUFFIX]
        noted = [native for native, title in titles if active and title.startswith(active + " - ")
                 and title.endswith(CHROME_TITLE_SUFFIX)]
        candidates = exact or noted
        if len(candidates) != 1 or candidates[0].get("id") in claimed:
            raise BrowserUnavailable("Chrome window titles do not identify each window once; checkpoint preserved")
        claimed.add(candidates[0].get("id"))
        matches[index] = candidates[0]
    if claimed != {window.get("id") for window in shell_windows}:
        raise BrowserUnavailable("Not every native Chrome window matched a companion window by title; checkpoint preserved")
    return matches


def _attach_title_placements(profiles: list[dict[str, Any]], names: list[str]) -> None:
    """Read placement without markers, focus or tab changes.

    Two compositor samples around a fresh companion window list must give the
    same mapping, so a title that changes during the capture fails it.
    """
    browser_windows = [
        (profile, window)
        for profile in profiles for window in profile.get("windows", [])
    ]
    chrome_windows = [window for _profile, window in browser_windows]
    first = _title_matches(chrome_windows, _chrome_shell_windows(profiles, capture_shell()))
    for profile in profiles:
        summaries = request_browser("list_windows", {}, profile=str(profile.get("profile") or "Default"),
                                    timeout=2)
        current = {item.get("id"): str(item.get("active_title") or "")
                   for item in summaries if isinstance(item, dict)} if isinstance(summaries, list) else {}
        if any(current.get(window.get("runtime_window_id")) != _active_title(window)
               for window in profile.get("windows", [])):
            raise BrowserUnavailable("Chrome windows changed while matching titles; checkpoint preserved")
    second = _title_matches(chrome_windows, _chrome_shell_windows(profiles, capture_shell()))
    if {index: native.get("id") for index, native in first.items()} != {
            index: native.get("id") for index, native in second.items()}:
        raise BrowserUnavailable("Chrome windows changed while matching titles; checkpoint preserved")
    for window in chrome_windows:
        window.pop("runtime_window_id", None)
    _apply_browser_placements(browser_windows, second, names)


def _apply_browser_placements(browser_windows, matches: dict[int, dict[str, Any]], names: list[str]) -> None:
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
    by_title: bool = False,
) -> dict[str, Any]:
    """Capture Chrome windows with their desktop placement.

    `by_title` identifies windows by title only, for periodic checkpoints that
    must not touch the user's windows; an ambiguous title fails the capture.
    """
    profiles: list[dict[str, Any]] = []
    errors = []
    for path in _host_paths():
        try:
            captured = _request_path(path, "capture")
            if isinstance(captured, dict):
                profiles.append(captured)
        except BrowserUnavailable as error:
            errors.append(str(error))
    if names is None:
        names = workspace_names()
    if by_title:
        _attach_title_placements(profiles, names)
    else:
        _attach_desktop_placements(profiles, shell if shell is not None else capture_shell(), names)
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
    owner.check()
    document = json.loads(login_status.status_path().read_text())
    if (owner.mode != "startup" or operations.current() != owner or not owner.matches(document)
            or document.get("operation_state") != "running"):
        raise BrowserUnavailable("Chrome placement continuation no longer owns the startup operation")


def _continuation_path(token: str) -> Path:
    key = token.removeprefix(CONTENT_PLACEMENT_PREFIX)
    if (not token.startswith(CONTENT_PLACEMENT_PREFIX) or len(key) != 64
            or any(char not in "0123456789abcdef" for char in key)):
        raise BrowserUnavailable("Invalid Chrome placement continuation identity")
    return runtime_dir() / "browser-continuations" / f"{key}.json"


def _defer_browser_placement(*, profile, window, restore_token, catalog, claimed_window_id=None) -> str:
    """Retain an exact claimed window and its recipe without submitting placement."""
    from . import operations
    from .browser_reconciliation import digest
    from .util import atomic_json
    owner = operations.current()
    if owner is None:
        raise BrowserUnavailable("Chrome content is loading; no owned startup continuation is available")
    browser_continuation_guard(owner)
    window_id = window.get("_reconcile_window_id", claimed_window_id)
    if (type(window_id) is not int or window.get("_preserve_current_content")
            or (claimed_window_id is not None and claimed_window_id != window_id)):
        raise BrowserUnavailable("Chrome continuation requires an exact strict-content window claim")
    payload = {"schema_version": 1, "operation_context": owner.to_dict(), "profile": profile,
               "window": window, "window_id": window_id,
               "restore_token": restore_token, "catalog": catalog}
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
    window_id = payload.get("window_id", window.get("_reconcile_window_id"))
    if (type(window_id) is not int or window.get("_preserve_current_content")
            or ("_reconcile_window_id" in window and window["_reconcile_window_id"] != window_id)):
        raise BrowserUnavailable("Chrome continuation lacks exact native identity")
    profiles = profiles_by_name(payload["catalog"])
    def verify_status():
        status = request("restore_status", {"restore_token": payload["restore_token"]}, profile)
        if (not isinstance(status, dict) or status.get("exists") is not True
                or type(status.get("window_id")) is not int or status["window_id"] != window_id):
            raise BrowserUnavailable("The claimed Chrome window changed; placement was not submitted")
        if status.get("group_warnings"):
            raise BrowserUnavailable("; ".join(status["group_warnings"]))
        if status.get("urls_restored") is not True:
            if status.get("urls_pending") is True:
                return False
            raise BrowserUnavailable("; ".join(status.get("url_errors") or ["Exact tab URLs changed; placement was not submitted"]))
        return True

    def verify_content():
        # Reconciled claims retain their complete-catalog contract. A strict
        # startup claim authorizes only its exact window, not unrelated windows.
        reconciled = "_reconcile_window_id" in window
        if reconciled and set(connected_profiles(strict_transport=True)) != set(profiles):
            raise BrowserUnavailable("Native Chrome profile inventory changed; placement was not submitted")
        expected_profiles = profiles if reconciled else {profile: profiles[profile]}
        for name, expected in expected_profiles.items():
            live = request("capture", {}, name)
            if (not isinstance(live, dict) or any(live.get(key) != expected.get(key)
                    for key in ("profile", "profile_directory", "app_id"))):
                raise BrowserUnavailable("Native Chrome profile identity changed; placement was not submitted")
            windows = live.get("windows", [])
            by_id = {item.get("runtime_window_id"): item for item in windows}
            if len(by_id) != len(windows) or (reconciled and len(windows) != len(expected["windows"])):
                raise BrowserUnavailable("Native Chrome window inventory changed; placement was not submitted")
            for saved in expected["windows"] if reconciled else [window]:
                actual = by_id.get(saved.get("_reconcile_window_id") if reconciled else window_id)
                # Chrome can expand a saved group while loading its tabs. That
                # changes its presentation, not the exact tabs or membership
                # that authorize moving this already claimed native window.
                if (actual is None or
                        window_signature(actual, ignore_group_collapse=True) !=
                        window_signature(saved, ignore_group_collapse=True)):
                    raise BrowserUnavailable("Native Chrome URLs or groups changed; placement was not submitted")

    if not verify_status():
        return {"token": token, "status": "waiting", "detail": "Placement awaits exact saved tab URLs"}
    verify_content()
    def content_guard():
        if not verify_status():
            raise BrowserUnavailable("Exact tab URLs are unverified; placement was not submitted")
        verify_content()
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
            authority_guard=lambda: browser_continuation_guard(owner), content_guard=content_guard)
        result = {"token":token, "status":"verified" if placed else "failed", "content_verified":True,
                  "detail":"Exact Chrome native placement verified" if placed else
                           "GNOME rejected the exact Chrome placement request; window preserved"}
    except BrowserPlacementPending as error:
        result = {"token":token, "status":"submitted" if error.request_id else "failed",
                  "placement_request":error.request_id, "content_verified":True,
                  "detail":str(error) if error.request_id else "GNOME accepted Chrome placement without a verifiable request identity"}
    except BrowserPlacementDeferred as error:
        browser_continuation_guard(owner)
        return {"token":token, "status":"waiting", "content_verified":True,
                "detail":f"Chrome companion is unavailable before saved placement: {error}"}
    except (BrowserUnavailable, TimeoutError) as error:
        # Any other placement failure can be after native submission. Retain a
        # terminal receipt so a lost publication cannot authorize another move.
        result = {"token":token, "status":"failed", "content_verified":True, "detail":str(error)}
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
                    **({"preserve_current_content": True} if window.get("_preserve_current_content") else {}),
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
        content_preserved = (result.get("content_preserved") is True
                             and result.get("content_basis") == "current-native-session"
                             and result.get("created") is False and result.get("reused") is True)
        content_verified = urls_verified or content_preserved
        urls_waiting = not urls_verified and result.get("urls_pending") is True
        reused = bool((result or {}).get("reused"))
        expectation_placed = not expectation or reused
        if expectation and (reused or not content_verified):
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
        def preservation_guard():
            from .browser_reconciliation import verify_navigation_reconciliation, ReconciliationRequired
            if authority_guard:
                authority_guard()
            if window.get("_preserve_current_content"):
                try:
                    verify_navigation_reconciliation(restore_catalog or chrome, {
                        name: request_browser("capture", {"runtime_identity": True}, profile=name, timeout=2)
                        for name in sorted(connected_profiles())})
                except ReconciliationRequired as error:
                    raise BrowserUnavailable(str(error)) from error
            status = request_browser("restore_status", {"restore_token": restore_token}, profile=profile_name, timeout=2)
            if authority_guard:
                authority_guard()
            if (not isinstance(status, dict) or status.get("exists") is not True
                    or status.get("window_id") != result["window_id"]
                    or status.get("content_preserved") is not True
                    or status.get("content_basis") != "current-native-session"):
                raise BrowserUnavailable("Current Chrome content changed; placement was not submitted")
        if place and placement and ("_reconcile_window_id" not in window or
                                    (content_verified and not result.get("group_warnings"))):
            try:
                placed = _place_browser_window(
                    profile=profile_name,
                    chrome_window_id=result["window_id"],
                    app_id=app_id,
                    placement=placement,
                    window_type=str(window.get("type") or "normal"),
                    **({"authority_guard": authority_guard} if authority_guard else {}),
                    **({"content_guard": preservation_guard} if content_preserved else {}),
                )
            except BrowserPlacementDeferred as error:
                placed = False
                placement_error = str(error)
                if (not content_preserved and not window.get("_preserve_current_content")
                        and (urls_verified or urls_waiting) and not result.get("group_warnings")):
                    try:
                        pending_request = _defer_browser_placement(profile=profile_name, window=window,
                            restore_token=restore_token, catalog=restore_catalog or chrome,
                            claimed_window_id=result["window_id"])
                        placement_waiting = True
                        placement_error = "Chrome companion is unavailable before saved placement; no move submitted yet"
                    except BrowserUnavailable as defer_error:
                        placement_error = str(defer_error)
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
            PhaseEvidence(EvidenceState.VERIFIED if content_verified else
                          EvidenceState.WAITING if urls_waiting else EvidenceState.FAILED,
                          "Current native tabs and groups preserved; saved tab URLs were not restored" if content_preserved else
                          "Exact saved tab URLs" if urls_verified else "; ".join(result.get("url_errors") or ["Exact tab URLs are unverified"]),
                          urls_waiting,
                          json.dumps([profile_name, restore_token, result["window_id"]], separators=(",", ":"))
                          if urls_waiting else None),
            PhaseEvidence(EvidenceState.SKIPPED if not place else EvidenceState.VERIFIED if placed else
                          EvidenceState.WAITING if placement_waiting else EvidenceState.FAILED,
                          placement_error or "Native placement" + (" pending verification" if placement_waiting else ""), not placed,
                          pending_request),
            attention=group_warnings + (() if content_verified or urls_waiting else ("Inspect redirected or unavailable tabs",)),
            created=bool(result.get("created")), reused=reused,
        )
        if not content_verified:
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
        verb = "preserved current" if content_preserved else "reused open" if reused else "restored"
        actions.append(BrowserRestoreResult(
            f"{verb} Chrome {profile_name}/{label} ({tab_count} tabs{suffix})",
            (not place or placed) and not group_warnings,
            evidence,
        ))
    return actions
