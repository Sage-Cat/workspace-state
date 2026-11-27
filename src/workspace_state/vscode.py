"""VS Code project recipes and verified native GNOME placement.

The local UI companion supplies project URIs, never editor contents. Short-lived
read-only identification panels bind a companion instance to a native window;
neither a shared Electron PID nor a human-readable project title is an identity.
VS Code remains the owner of editor state and Hot Exit backups.
"""
from __future__ import annotations

import copy
import errno
import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import time
import types
import uuid
from pathlib import Path
from typing import Callable, Any
from urllib.parse import unquote, urlsplit

from .desktop import (capture_shell, serialized_identification, resolve_placement_target, move_window_result,
                      wait_for_scoped_handoff)
from .file_manager import DRIVES, DRIVE_ROOT, PLACEMENT_KEYS, _marker_directory
from .util import CommandError, launch_graphical_service
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount, ProviderRestoreError, placement_accepted, PlacementPending)
from .provider_results import waiting_only, native_handoff_token, placement_frame_matches

_serialized_identification = serialized_identification()


MAX_WINDOWS = 32
MAX_MESSAGE = 1024 * 1024
ALIASES = {"code", "code.desktop", "visual-studio-code", "com.visualstudio.code", "com.microsoft.vscode"}
Reporter = Callable[[str, str, int, int], None]


class UnsafeEditorState(CommandError):
    """A stale project recipe cannot protect live unsaved editor contents."""


class CompanionNotReady(CommandError):
    """A native Code window is not yet discoverable through its companion."""


class _DeadlineExceeded(CommandError):
    pass


def _remaining(deadline: float | None, limit: float) -> float:
    remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise _DeadlineExceeded("VS Code restoration deadline exceeded")
    return remaining


def runtime_root() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "workspace-state" / "vscode"


def _uri(value: Any, *, untitled: bool = False):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("Invalid VS Code project URI")
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(value)):
        raise ValueError("VS Code URI contains control characters")
    parsed = urlsplit(value)
    schemes = {"file", "vscode-remote"} | ({"untitled"} if untitled else set())
    if parsed.scheme not in schemes or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Unsupported or credential-bearing VS Code project URI")
    if parsed.scheme == "file" and (parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/")):
        raise ValueError("VS Code folder must have an absolute local path")
    if parsed.scheme == "vscode-remote" and (not parsed.netloc or not parsed.path.startswith("/")):
        raise ValueError("VS Code remote URI requires an authority and absolute path")
    return parsed


def _volatile_roots() -> tuple[Path, ...]:
    roots = [Path("/tmp"), Path("/dev/shm")]
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        roots.append(Path(runtime))
    return tuple(roots)


def volatile_project(item: Any) -> bool:
    """Return True when every local project path is removed at reboot.

    A window on a tool's scratch folder in /tmp or the runtime directory cannot
    be reopened after a reboot, so it is neither saved nor kept pending.
    """
    if not isinstance(item, dict) or item.get("remote_name"):
        return False
    if item.get("kind") == "workspace":
        uris = [item.get("workspace_file")]
    elif item.get("kind") == "folder":
        uris = [folder.get("uri") for folder in item.get("folders") or [] if isinstance(folder, dict)]
    else:
        return False
    roots = _volatile_roots()
    paths = []
    for uri in uris:
        try:
            parsed = _uri(uri)
        except ValueError:
            return False
        if parsed.scheme != "file":
            return False
        paths.append(Path(os.path.normpath(unquote(parsed.path))))
    return bool(paths) and all(any(path.is_relative_to(root) for root in roots) for path in paths)


def _text(value: Any, label: str, maximum: int = 512) -> None:
    if not isinstance(value, str) or not value or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise ValueError(f"Invalid VS Code {label}")


def _validate_project(item: dict) -> None:
    if not isinstance(item, dict) or item.get("kind") not in {"folder", "workspace", "untitled", "empty"}:
        raise ValueError("Invalid VS Code window kind")
    folders = item.get("folders")
    if not isinstance(folders, list) or len(folders) > 64:
        raise ValueError("Invalid VS Code folders")
    for folder in folders:
        if not isinstance(folder, dict):
            raise ValueError("Invalid VS Code folder record")
        _uri(folder.get("uri"))
        _text(folder.get("name"), "folder name")
    if len({folder["uri"] for folder in folders}) != len(folders):
        raise ValueError("Duplicate VS Code workspace folder URI")
    project = item.get("workspace_file")
    if project:
        parsed = _uri(project, untitled=True)
        expected = "untitled" if parsed.scheme == "untitled" else "workspace"
        if item["kind"] != expected:
            raise ValueError("VS Code workspace type does not match its URI")
        if expected == "workspace" and not parsed.path.endswith(".code-workspace"):
            raise ValueError("VS Code workspace must be a .code-workspace file")
    elif item["kind"] in {"workspace", "untitled"}:
        raise ValueError("Missing VS Code workspace URI")
    elif item["kind"] == "folder" and len(folders) != 1:
        raise ValueError("Single-folder VS Code window requires exactly one folder")
    elif item["kind"] == "empty" and folders:
        raise ValueError("Empty VS Code window cannot contain folders")
    profile = item.get("profile")
    if not isinstance(profile, dict):
        raise ValueError("VS Code profile could not be identified")
    _text(profile.get("id"), "profile ID")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", profile["id"]):
        raise ValueError("Invalid VS Code profile ID")
    _text(profile.get("name"), "profile name")
    directory = item.get("user_data_dir")
    _text(directory, "user data directory", 8192)
    if not Path(directory).is_absolute():
        raise ValueError("VS Code user data directory must be absolute")
    editors = item.get("editor_uris", [])
    if not isinstance(editors, list) or len(editors) > 512:
        raise ValueError("Invalid VS Code editor identity list")
    for uri in editors:
        _uri(uri, untitled=True)
    if type(item.get("dirty_count", 0)) is not int or not 0 <= item.get("dirty_count", 0) <= 10000:
        raise ValueError("Invalid VS Code dirty editor count")
    if item.get("remote_name") is not None:
        _text(item["remote_name"], "remote name")


def validate_vscode(record: Any) -> None:
    if not isinstance(record, dict) or record.get("provider") != "vscode" or record.get("version", 1) != 1:
        raise ValueError("Unsupported VS Code checkpoint")
    windows = record.get("windows")
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ValueError("Invalid VS Code window list")
    for item in windows:
        _validate_project(item)
        placement = item.get("placement")
        if not isinstance(placement, dict):
            raise ValueError("Missing VS Code placement")
        _text(placement.get("workspace_name"), "GNOME workspace name")
        if type(placement.get("workspace")) is not int or placement["workspace"] < 0:
            raise ValueError("Invalid VS Code GNOME workspace index")
        identity = placement.get("monitor_intent") or placement.get("monitor_identity")
        if not isinstance(identity, dict) or not any(identity.get(key) for key in ("edid_hash", "serial", "connector")):
            raise ValueError("VS Code window has no physical display identity")
        if placement.get("state") not in {"normal", "minimized", "maximized", "fullscreen"}:
            raise ValueError("Invalid VS Code window state")
        for key in ("geometry", "geometry_relative"):
            geometry = placement.get(key)
            if not isinstance(geometry, dict) or any(type(geometry.get(field)) is not int for field in ("x", "y", "width", "height")) or geometry["width"] <= 0 or geometry["height"] <= 0:
                raise ValueError("Invalid VS Code geometry")


def needs_storage(record: dict | None) -> bool:
    """Pure classification: no stat()/resolve() on a possibly blocked mount."""
    for item in (record or {}).get("windows", []):
        uris = [folder["uri"] for folder in item.get("folders", [])]
        if item.get("workspace_file"):
            uris.append(item["workspace_file"])
        for uri in uris:
            parsed = _uri(uri, untitled=True)
            if parsed.scheme == "file" and Path(os.path.normpath(unquote(parsed.path))).is_relative_to(DRIVE_ROOT):
                return True
    return False


def matching_windows(shell: dict) -> list[dict]:
    def matches(window):
        values = set(str(item).lower() for item in window.get("app_ids", []))
        values.update(str(window.get(key) or "").lower() for key in ("app_id", "wm_class", "wm_class_instance"))
        return bool(values & ALIASES)
    return [window for window in shell.get("windows", []) if matches(window)]


def _check_private(path: Path, *, directory: bool = False) -> None:
    entry = path.lstat()
    expected = stat.S_ISDIR(entry.st_mode) if directory else stat.S_ISSOCK(entry.st_mode)
    if not expected or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) & 0o077:
        raise CommandError("Unsafe VS Code companion endpoint permissions")


def _request(endpoint: Path, method: str, token: str | None = None, *, timeout: float = 1.0, payload: dict | None = None) -> dict:
    try:
        return _request_once(endpoint, method, token, timeout=timeout, **({"payload": payload} if payload else {}))
    except TimeoutError as error:
        # A listening extension host can still be busy activating other
        # extensions. Its short RPC budget must not bypass the longer startup
        # readiness deadline, or make an unknown native window look absent.
        raise CompanionNotReady("VS Code companion is busy or still starting") from error


def _request_once(endpoint: Path, method: str, token: str | None = None, *, timeout: float = 1.0, payload: dict | None = None) -> dict:
    end = time.monotonic() + timeout

    def remaining() -> float:
        value = end - time.monotonic()
        if value <= 0:
            raise TimeoutError("VS Code companion request timed out")
        return value

    root = runtime_root()
    if endpoint.parent != root or not re.fullmatch(r"[a-f0-9]{32}\.sock", endpoint.name):
        raise CommandError("Invalid VS Code companion endpoint")
    _check_private(root.parent, directory=True)
    _check_private(root, directory=True)
    _check_private(endpoint)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(remaining())
        connection.connect(str(endpoint))
        peer_pid, peer_uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        if peer_uid != os.getuid():
            raise CommandError("VS Code companion belongs to a different user")
        request = {**(payload or {}), "version": 1, "method": method}
        if token:
            request["token"] = token
        connection.sendall((json.dumps(request) + "\n").encode())
        data = bytearray()
        while b"\n" not in data:
            connection.settimeout(remaining())
            chunk = connection.recv(8192)
            if not chunk:
                raise CommandError("VS Code companion disconnected before acknowledging")
            data.extend(chunk)
            if len(data) > MAX_MESSAGE:
                raise CommandError("Oversized VS Code companion response")
        try:
            reply = json.loads(data.split(b"\n", 1)[0])
        except (ValueError, UnicodeError) as error:
            raise CommandError("Invalid VS Code companion response") from error
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise CommandError(str(reply.get("error", "VS Code companion failed"))[:1000] if isinstance(reply, dict) else "Invalid VS Code companion response")
        result = reply.get("result")
        if not isinstance(result, dict):
            raise CommandError("VS Code companion returned no result")
        if method in {"state", "restore-editors", "drop-launch-marker"} and (
                result.get("pid") != peer_pid or result.get("instance") != endpoint.stem
                or (method == "state" and result.get("version") != 1)):
            raise CommandError("VS Code companion identity changed")
        return result


def _states(deadline: float) -> list[dict]:
    root = runtime_root()
    if not root.exists():
        return []
    _check_private(root, directory=True)
    endpoints = sorted(root.glob("*.sock"))
    if len(endpoints) > 128:
        raise CommandError("Too many VS Code companion endpoints; check stale instances")
    results = []
    for endpoint in endpoints:
        if time.monotonic() >= deadline:
            raise CommandError("VS Code companion discovery timed out")
        try:
            state = _request(endpoint, "state", timeout=min(.5, deadline - time.monotonic()))
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                continue  # dead processes are never evidence of live windows
            raise CommandError("VS Code companion did not respond") from error
        state["endpoint"] = endpoint
        results.append(state)
    if len(results) > MAX_WINDOWS:
        raise CommandError("Too many VS Code windows")
    return results


BLOCKER_KINDS = {"unsaved", "untitled", "terminal"}


def switch_blockers(state: dict) -> list[dict] | None:
    """Return what keeps a window from being closed for a workspace switch.

    None means the companion predates the switch policy; the caller then keeps
    the older rule (any unsaved edit or non-text editor blocks). A malformed
    reply blocks conservatively instead of failing an ordinary capture.
    """
    if "switch_policy" not in state.get("capabilities", ()):
        return None
    items = state.get("switch_blockers")
    result = []
    if isinstance(items, list) and len(items) <= 64:
        for item in items:
            if (not isinstance(item, dict) or item.get("kind") not in BLOCKER_KINDS
                    or not isinstance(item.get("label"), str) or not isinstance(item.get("reason", ""), str)):
                break
            result.append({"kind": item["kind"], "label": item["label"][:60], "reason": item.get("reason", "")[:40]})
        else:
            return result
    return [{"kind": "unverified", "label": "", "reason": "the companion returned an invalid reply"}]


def _project(state: dict) -> dict:
    workspace = state.get("workspace_file")
    folders = copy.deepcopy(state.get("folders", []))
    kind = ("untitled" if str(workspace).startswith("untitled:") else "workspace") if workspace else "folder" if folders else "empty"
    item = {key: copy.deepcopy(state.get(key)) for key in (
        "profile", "user_data_dir", "remote_name", "window_key", "hot_exit",
    )}
    item.update(kind=kind, workspace_file=workspace, folders=folders,
                editor_uris=state.get("editor_uris", []), dirty_count=state.get("dirty_count", 0))
    if isinstance(state.get("text_editor_uris"), list):
        item["_text_editor_uris"] = list(state["text_editor_uris"])
    _validate_project(item)
    return item


def _identity(item: dict, *, wide: bool = False) -> tuple:
    profile = item.get("profile") or {}
    identity = (item["user_data_dir"], profile.get("id"), item.get("remote_name"), item["kind"], item.get("workspace_file"),
                tuple(folder["uri"] for folder in item["folders"]))
    # Empty editor windows have no project URI. Match native recovery metadata;
    # never copy buffer contents or guess that an arbitrary blank window is it.
    if item["kind"] == "empty":
        # Text files only: diffs, previews and untitled tabs are not recovered
        # and must not decide which empty window is which. A checkpoint saved
        # before the text list existed recorded the wider list, which the live
        # companion still reports, so `wide` compares that on both sides.
        editors = item.get("editor_uris", []) if wide else saved_text_editors(item)
        identity += (tuple(sorted(editors)), item.get("dirty_count", 0))
    return identity


def _profile_registry(user_data_dir: str) -> list[dict]:
    root = Path(user_data_dir)
    result = []
    for filename in (root / "User/globalStorage/storage.json", root / "storage.json"):
        try:
            if filename.stat().st_size > MAX_MESSAGE * 8:
                raise CommandError("VS Code profile registry is unexpectedly large")
            document = json.loads(filename.read_text())
        except (FileNotFoundError, ValueError):
            continue
        profiles = document.get("userDataProfiles", [])
        if not isinstance(profiles, list):
            continue
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            location = profile.get("location")
            location = location.get("path", "") if isinstance(location, dict) else location
            if isinstance(location, str):
                identifier = Path(unquote(urlsplit(location).path)).name
                result.append({"id": profile.get("id") or identifier, "name": profile.get("name")})
    return result


def _verify_profile(item: dict) -> None:
    profile = item["profile"]
    root = Path(item["user_data_dir"])
    if not root.is_dir():
        raise CommandError("Saved VS Code user-data directory is missing; refusing to create an empty replacement")
    if profile["id"] == "default":
        return
    matches = [entry for entry in _profile_registry(str(root)) if entry == profile]
    if len(matches) != 1 or not (root / "User/profiles" / profile["id"]).is_dir():
        raise CommandError("Saved VS Code profile is missing or renamed; refusing to create a new profile")


_JSONC_STRING = r'"(?:[^"\\]|\\.)*"'
_JSONC_COMMENTS = re.compile(_JSONC_STRING + r'|//[^\r\n]*|/\*[\s\S]*?\*/')
_JSONC_TRAILING = re.compile(_JSONC_STRING + r'|,(?=\s*[}\]])')


def _settings_object(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate VS Code settings key")
        result[key] = value
    return result


def _read_user_settings(path: Path) -> dict:
    # Also allow a symlink to a regular settings file. Nonblocking open avoids
    # hanging the desktop operation if a settings path is an unexpected FIFO.
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8-sig") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MESSAGE:
            raise ValueError("Unexpected VS Code settings file")
        raw = stream.read(MAX_MESSAGE + 1)
    if len(raw) > MAX_MESSAGE:
        raise ValueError("VS Code settings are too large")
    # Preserve quoted keys/values, including URLs and literal comment markers.
    text = _JSONC_COMMENTS.sub(lambda match: match[0] if match[0].startswith('"') else ' ', raw)

    def trailing(match: re.Match) -> str:
        if match[0] != ",":
            return match[0]
        before = match.start() - 1
        while before >= 0 and text[before].isspace():
            before -= 1
        return "" if before >= 0 and text[before] not in "{[,:" else ","

    settings = json.loads(_JSONC_TRAILING.sub(trailing, text), object_pairs_hook=_settings_object)
    if not isinstance(settings, dict):
        raise ValueError("VS Code settings must be an object")
    return settings


def _verify_scoped_cold_start(scope: str) -> None:
    # Application-scoped settings remain in the default profile even when
    # this particular window opens under a different editor profile.
    unverified = "Cannot verify VS Code window.restoreWindows; scoped cold launch was deferred"
    path = Path(scope) / "User" / "settings.json"
    try:
        settings = _read_user_settings(path)
    except FileNotFoundError as error:
        if path.is_symlink():
            raise CommandError(unverified) from error
        return  # VS Code's default is "all".
    except (OSError, ValueError, RecursionError) as error:
        raise CommandError(unverified) from error
    values = [settings["window.restoreWindows"]] if "window.restoreWindows" in settings else []
    window = settings.get("window")
    if isinstance(window, dict) and "restoreWindows" in window:
        values.append(window["restoreWindows"])
    if "preserve" in values:
        raise CommandError("VS Code window.restoreWindows is 'preserve'; a scoped cold launch could reopen other workspaces")
    if any(not isinstance(value, str) or value not in {"all", "folders", "one", "none"} for value in values):
        raise CommandError(unverified)


class _LiveWindows:
    def __init__(self):
        self.ids: dict[str, int] = {}

    def get(self, deadline: float, *, strict: bool = True) -> list[dict]:
        shell = capture_shell()
        if not shell.get("available"):
            raise CommandError("GNOME window state is unavailable for VS Code")
        windows = matching_windows(shell)
        states = _states(deadline)
        result = []
        for state in states:
            instance = state["instance"]
            wid = self.ids.get(instance)
            native = next((window for window in windows if window["id"] == wid), None)
            if native is None:
                try:
                    native = self._identify(state, deadline)
                except CommandError:
                    if strict:
                        raise
                    continue
                self.ids[instance] = native["id"]
            item = _project(state)
            result.append({**item, "instance": instance, "endpoint": state["endpoint"], "shell": native,
                           "unsupported_editors": state.get("unsupported_editors"),
                           "switch_blockers": switch_blockers(state),
                           "text_editor_uris": state.get("text_editor_uris"),
                           "capabilities": state.get("capabilities", []),
                           "workspace_launch_markers": state.get("workspace_launch_markers", [])})
        if len({item["shell"]["id"] for item in result}) != len(result):
            raise CommandError("Several VS Code companions identified the same native window")
        if strict:
            # Identification yields to the compositor. Native recovery may
            # create another window meanwhile, before its extension activates.
            latest = capture_shell()
            if not latest.get("available"):
                raise CommandError("GNOME window state is unavailable for VS Code")
            if {item["shell"]["id"] for item in result} != {window["id"] for window in matching_windows(latest)}:
                raise CompanionNotReady("VS Code companion is not ready for every open window; if this persists, install/enable it in each profile")
        return result

    @_serialized_identification
    def _identify(self, state: dict, deadline: float) -> dict:
        token = uuid.uuid4().hex
        endpoint = state["endpoint"]
        try:
            response = _request(endpoint, "identify", token, timeout=_remaining(deadline, 1))
            if response.get("token") != token:
                raise CommandError("VS Code identification was not acknowledged")
            end = min(deadline, time.monotonic() + 2)
            last_id, samples = None, 0
            while time.monotonic() < end:
                windows = [window for window in matching_windows(capture_shell()) if f"wsctl-identify-{token}" in str(window.get("title", ""))]
                if len(windows) == 1:
                    wid = windows[0]["id"]
                    samples = samples + 1 if wid == last_id else 1
                    last_id = wid
                    if samples >= 2:
                        return windows[0]
                else:
                    last_id, samples = None, 0
                time.sleep(.05)
            raise CompanionNotReady("VS Code window could not be identified; check companion activation and window.title includes the active editor")
        finally:
            try:
                _request(endpoint, "release", token)
                until = min(deadline, time.monotonic() + .7)
                marker = f"wsctl-identify-{token}"
                while (remaining := until - time.monotonic()) > 0:
                    shell = capture_shell(timeout=remaining)
                    if not any(marker in str(window.get("title", "")) for window in matching_windows(shell)):
                        break
                    time.sleep(min(.05, max(0, until - time.monotonic())))
            except (OSError, CommandError):
                pass  # companion additionally expires the panel after 3 seconds


def _wait_for_live_windows(live: _LiveWindows, deadline: float, *,
                          scope: str | None = None,
                          reporter: Callable[[str], None] | None = None) -> list[dict]:
    """Wait for startup acknowledgement, never launch around unknown windows.

    An extension's onStartupFinished event can arrive after the native window.
    Only that transient readiness failure is retried; permission, protocol and
    identity errors remain fatal. A bootstrap also needs a fully represented,
    stable native window set, not merely one fast extension host.
    """
    stable_since, signature = None, None
    last_report = float("-inf")
    reason = "Waiting for VS Code to open its native recovery window"
    while time.monotonic() < deadline:
        try:
            current = live.get(deadline)
        except CompanionNotReady as error:
            reason = str(error)
            stable_since, signature = None, None
        else:
            if scope is None:
                return current
            if any(window["user_data_dir"] == scope for window in current):
                now = tuple(sorted((window["instance"], window["shell"]["id"]) for window in current))
                stable_since = stable_since if now == signature else time.monotonic()
                signature = now
                if time.monotonic() - stable_since >= 1:
                    return current
                reason = "Waiting for VS Code native recovery windows to settle"
            else:
                stable_since, signature = None, None
                reason = "Waiting for VS Code to open its native recovery window"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if reporter and time.monotonic() - last_report >= 2:
            reporter(reason)
            last_report = time.monotonic()
        time.sleep(min(.2, remaining))
    raise CommandError(f"VS Code startup readiness timed out: {reason}; no extra window was launched")


def capture_vscode(shell: dict | None = None, *, include_native_ids: bool = False,
                   native_window_ids: frozenset[int] | None = None) -> dict:
    shell = shell if shell is not None else capture_shell()
    if not shell.get("available"):
        raise CommandError("Cannot capture VS Code without GNOME window state")
    record = {"provider": "vscode", "version": 1, "windows": []}
    before = matching_windows(shell)
    if native_window_ids is not None:
        before = [item for item in before if item["id"] in native_window_ids]
    if not before:
        return record
    deadline = time.monotonic() + 15
    live = _LiveWindows()
    if native_window_ids is None:
        captured = live.get(deadline)
    else:
        captured = [item for item in live.get(deadline, strict=False) if item["shell"]["id"] in native_window_ids]
    if {item["shell"]["id"] for item in captured} != {window["id"] for window in before}:
        raise CommandError("VS Code windows changed while capturing; checkpoint not replaced")
    workspaces = {item["index"]: item["name"] for item in shell.get("workspaces", [])}
    # Identification is temporary; original placement/title precedes any panel.
    originals = {window["id"]: window for window in before}
    for live in captured:
        window = originals[live["shell"]["id"]]
        item = _project(live)
        if live.get("workspace_launch_markers"):
            raise CommandError("A scoped VS Code restoration is still in progress")
        item["placement"] = {key: copy.deepcopy(window[key]) for key in PLACEMENT_KEYS if key in window}
        item["placement"]["workspace_name"] = workspaces.get(window.get("workspace"), "")
        if item["dirty_count"] and item.get("hot_exit") not in {"onExit", "onExitAndWindowClose"}:
            raise UnsafeEditorState("VS Code has unsaved edits without verified Hot Exit recovery; save them or enable native recovery before shutdown")
        if volatile_project(item):
            continue
        if include_native_ids:
            item["_native_id"] = window["id"]
            blockers = live.get("switch_blockers")
            item["_unsupported_editors"] = None if blockers is not None else live.get("unsupported_editors")
            item["_switch_blockers"] = blockers
            item["_supports_workspace_switch"] = "workspace_switch" in live.get("capabilities", ())
            item["_vscode_instance"] = live["instance"]
        record["windows"].append(item)
    validate_vscode(record)
    return record


def saved_text_editors(item: dict) -> list[str]:
    """Plain text files a saved window can reopen by URI.

    `editor_uris` keeps its original, wider meaning. A record captured by a
    companion that reports no separate list can only hold text files, because
    that companion refused to close a window with any other editor.
    """
    value = item.get("_text_editor_uris")
    return list(item.get("editor_uris", [])) if value is None else list(value)


def _live_text_editors(state: dict) -> set[str]:
    return set(state.get("text_editor_uris", state.get("editor_uris", [])))


def _unsafe_to_reuse(state: dict) -> bool:
    """An existing window may be reused for On unless it holds unsaved work."""
    blockers = switch_blockers(state)
    if blockers is None:
        return state.get("dirty_count") != 0 or state.get("unsupported_editors") != 0
    return any(item["kind"] != "terminal" for item in blockers)


def verify_switch_close(saved: dict) -> None:
    endpoint = runtime_root() / (saved["_vscode_instance"] + ".sock")
    state = _request(endpoint, "state")
    blockers = switch_blockers(state)
    blocked = (state.get("dirty_count") != 0 or state.get("unsupported_editors") != 0) if blockers is None else bool(blockers)
    if (_identity(_project(state)) != _identity(saved) or blocked
            or _live_text_editors(state) != set(saved_text_editors(saved))):
        raise CommandError("VS Code editors changed; the window was left open")


def _check_resource(uri: str, *, folder: bool, deadline: float | None = None) -> None:
    parsed = _uri(uri)
    if parsed.scheme != "file":
        return  # the companion acknowledges the remote project after launch
    local = Path(os.path.normpath(unquote(parsed.path)))
    if local.is_relative_to(DRIVE_ROOT):
        mounts = [(mount, unit) for mount, unit in DRIVES.items() if local.is_relative_to(mount)]
        if not mounts:
            raise CommandError("VS Code project is under an unmanaged drive")
        mount, unit = max(mounts, key=lambda pair: len(pair[0].parts))
        for argv in (["/usr/bin/systemctl", "--user", "is-active", "--quiet", unit],
                     ["/usr/bin/findmnt", "--mountpoint", str(mount), "--noheadings"]):
            try:
                if subprocess.run(argv, capture_output=True, timeout=_remaining(deadline, 2)).returncode:
                    raise CommandError(f"VS Code is waiting for mounted storage: {unit}")
            except subprocess.TimeoutExpired as error:
                raise CommandError(f"VS Code storage check timed out: {unit}") from error
    try:
        result = subprocess.run(["/usr/bin/gio", "info", "--attributes=standard::type", "--", uri],
                                capture_output=True, text=True, timeout=_remaining(deadline, 3), env={**os.environ, "LC_ALL": "C"})
    except subprocess.TimeoutExpired as error:
        raise CommandError("VS Code project availability check timed out") from error
    expected = f"standard::type: {2 if folder else 1}"
    if result.returncode or not any(line.strip() == expected for line in result.stdout.splitlines()):
        raise CommandError("Saved VS Code folder/workspace is missing or has the wrong type")


def _command(item: dict, *, native: bool = False) -> list[str]:
    argv = ["/usr/bin/code", "--user-data-dir", item["user_data_dir"]]
    profile = item["profile"]
    if profile["id"] != "default":
        argv += ["--profile", profile["name"]]
    # Supplying a regular project at bootstrap avoids creating an unwanted
    # blank window when native window.restoreWindows is set to none. With no
    # --new-window Code retains its own native session/Hot Exit recovery policy.
    if not native:
        argv += ["--new-window"]
    if item["kind"] == "folder":
        argv += ["--folder-uri", item["folders"][0]["uri"]]
    elif item["kind"] == "workspace":
        argv += ["--file-uri", item["workspace_file"]]
    elif not native and (item["kind"] == "untitled" or saved_text_editors(item) or item.get("dirty_count")):
        raise CommandError("VS Code did not recover the saved untitled/empty editor window; refusing to replace unsaved state")
    return argv


def _marker_for(switch_token: str, native_id: int) -> tuple[str, Path]:
    token = hashlib.sha256(f"{switch_token}:vscode:{native_id}".encode()).hexdigest()[:32]
    return token, _marker_directory() / f"vscode-{token}.txt"


def _project_request(state: dict, instance: str) -> dict:
    project = json.dumps([state["user_data_dir"], state["profile"]["id"], state.get("remote_name"),
                          state.get("workspace_file"), [folder["uri"] for folder in state["folders"]]],
                         ensure_ascii=False, separators=(",", ":"))
    return {"instance": instance, "project": project}


def _target(placement: dict) -> dict:
    return resolve_placement_target(placement, provider="VS Code GNOME", shell=capture_shell())


def _place(wid: int, target: dict, deadline: float, *, require_local: bool = False,
           scoped: bool = False) -> None:
    shell = capture_shell(timeout=_remaining(deadline, 10))
    window = next((item for item in matching_windows(shell) if item["id"] == wid), None)
    if not window:
        raise CommandError("VS Code window closed before placement was verified")
    if require_local and (window.get("workspace") != target["workspace"] or window.get("sticky")):
        raise CommandError("VS Code window moved to another workspace before placement")
    if scoped and placement_frame_matches(window, target):
        return
    if "staged_placement_handoff" not in shell.get("capabilities", []):
        raise CommandError("GNOME staged placement handoff capability is unavailable")
    result = move_window_result(wid, dict(target, stage_before_handoff=True), timeout=_remaining(deadline, 10))
    if not placement_accepted(result):
        raise CommandError("GNOME rejected final VS Code placement")
    if result.get("status") == "verified":
        return
    token = native_handoff_token(result)
    if not token:
        raise CommandError("GNOME did not retain the final VS Code placement handoff")
    if scoped and wait_for_scoped_handoff(token, deadline):
        return
    raise PlacementPending("Saved VS Code placement is owned by GNOME; awaiting compositor verification", token)


SETTLE_SECONDS = 1.5
SETTLED_SECONDS = 0.5


def _keep_placements(requests: dict, deadline: float) -> dict:
    """Hold maximized or fullscreen targets until the editors stop changing them.

    `requests` maps a key to (window id, target, _place options); the result
    maps the same keys to None or the error that ended the observation.

    The first placement runs as soon as the companion answers, which can be
    before the editor has applied its own saved window state, so a verified
    state at that moment is not final. All windows are observed together in
    one pass, so the added delay does not grow with their number.
    """
    results = {key: None for key in requests}
    pending = {key: item for key, item in requests.items()
               if item[1].get("state") in {"maximized", "fullscreen"}}
    stable, repairs = {}, dict.fromkeys(pending, 0)
    # The restore deadline can already be spent while other windows waited; this
    # check has its own short allowance so it still observes every window once.
    start = time.monotonic()
    limit = max(deadline, start) + SETTLE_SECONDS
    end = start + SETTLE_SECONDS

    def unsettled(error):
        for key in pending:
            if key not in stable:
                results[key] = error

    while pending:
        try:
            shell = capture_shell(timeout=_remaining(limit, 10))
        except (CommandError, OSError) as error:
            unsettled(error)  # A window that already matched stays verified.
            break
        windows = {item["id"]: item for item in matching_windows(shell)}
        now = time.monotonic()
        for key, (wid, target, options) in list(pending.items()):
            window = windows.get(wid)
            if window is None:
                results[key] = CommandError("VS Code window closed before placement was verified")
            elif placement_frame_matches(window, target):
                stable.setdefault(key, now)
                if now - stable[key] >= SETTLED_SECONDS:
                    del pending[key]
                continue
            else:
                stable.pop(key, None)
                if repairs[key] == 2:
                    results[key] = CommandError("VS Code changed its window state after placement")
                else:
                    repairs[key] += 1
                    try:
                        _place(wid, target, limit, **options)
                    except (CommandError, OSError) as error:
                        results[key] = error
                    else:
                        end = max(end, min(limit, time.monotonic() + SETTLE_SECONDS))
                        continue
            del pending[key]
        if pending and now >= end:
            unsettled(CommandError("VS Code changed its window state after placement"))
            break
        if pending:
            time.sleep(min(.1, max(0, end - now)))
    return results


def _keep_placement(wid: int, target: dict, deadline: float, **options) -> None:
    error = _keep_placements({0: (wid, target, options)}, deadline)[0]
    if error is not None:
        raise error


def restore_vscode(record: dict | None, *, dry_run: bool = False, no_place: bool = False,
                   workspace: str | None = None, reporter: Reporter | None = None,
                   timeout: float = 30, workspace_switch: bool = False,
                   excluded_windows: frozenset[int] = frozenset(), switch_token: str = "",
                   blocked_native_scopes: frozenset[str] = frozenset()) -> int:
    if record is not None:
        validate_vscode(record)
    windows = [item for item in (record or {}).get("windows", []) if not workspace or item["placement"]["workspace_name"] == workspace]
    def report(state, message, current):
        print(message, flush=True)
        if reporter:
            reporter(state, message, current, max(1, len(windows)))
    if not windows:
        report("skipped", "No saved VS Code windows; not launching the editor", 1)
        return 0
    if dry_run:
        report("ready", f"Would restore {len(windows)} VS Code project window(s) and placement", len(windows))
        return len(windows)
    deadline = time.monotonic() + timeout
    live = _LiveWindows()
    # A running but uninstrumented Code is not a reason to start duplicates.
    current = _wait_for_live_windows(live, deadline,
                                    reporter=lambda message: report("running", message, 0))
    initial_instances = {window["instance"] for window in current}
    started = set()
    errors, claimed = [], set()
    evidence_results = [None] * len(windows)
    restored = 0

    def conclude(slot, error=None):
        """Record the final evidence of one saved window; error None means verified."""
        nonlocal restored
        error = error or slot.place_error
        if error is None:
            restored += 1
            report("running", f"VS Code window {slot.index + 1}: project and placement verified", restored)
        else:
            errors.append(f"VS Code window {slot.index + 1}: {error}")
            if slot.identity_evidence.state == EvidenceState.UNKNOWN:
                slot.identity_evidence = PhaseEvidence(EvidenceState.FAILED, str(error))
            report("running", errors[-1], restored)
        evidence_results[slot.index] = ProviderItemResult(
            "vscode", str(slot.index + 1), slot.identity_evidence, slot.content_evidence,
            slot.placement_evidence, attention=slot.attention, reused=slot.reused,
            native_window_id=slot.selected["shell"]["id"] if slot.selected else None)

    settling = []

    def finish(slot, error=None):
        """Conclude a window, or hold a maximized one for the shared placement check."""
        if (error is None and not no_place and slot.place_error is None
                and slot.target.get("state") in {"maximized", "fullscreen"}):
            settling.append(slot)
        else:
            conclude(slot, error)

    def advance(slot):
        """True when verified, False while the companion is transiently busy; fatal errors raise."""
        endpoint = slot.selected["endpoint"]
        try:
            if not slot.identified:
                state = _request(endpoint, "state", timeout=_remaining(deadline, 1))
                if _identity(_project(state), wide=slot.wide) != slot.identity:
                    raise CommandError("VS Code project changed before placement; window left untouched")
                slot.identity_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Exact project/profile and companion instance")
                if workspace_switch and _unsafe_to_reuse(state):
                    raise CommandError("VS Code window contains unsaved edits or editors that cannot be reopened")
                if slot.launch_owned and state.get("workspace_launch_markers") != [slot.marker[0]]:
                    raise CommandError("VS Code no longer proves the exact scoped launch window")
                slot.identified = True
                if not no_place:
                    # Place as soon as the window is identified, so a busy
                    # extension host cannot leave it on the wrong workspace.
                    try:
                        _place(slot.selected["shell"]["id"], slot.target, deadline,
                               **({"require_local": not slot.launch_owned, "scoped": True} if workspace_switch else {}))
                        slot.placement_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Observed saved native placement")
                    except (CommandError, OSError) as error:
                        slot.place_error = error
                        slot.placement_evidence = PhaseEvidence(
                            EvidenceState.WAITING if isinstance(error, PlacementPending) else EvidenceState.FAILED,
                            str(error), True, getattr(error, "request_id", None))
            probe = _request(endpoint, "probe", timeout=_remaining(deadline, 2))
            if probe.get("ready") is not True:
                slot.reason = "VS Code project storage or remote connection is not ready"
                return False
            latest = _request(endpoint, "state", timeout=_remaining(deadline, 1))
        except CompanionNotReady as error:
            slot.reason = str(error)
            return False
        except _DeadlineExceeded as error:
            # Keep the last observed busy reason; without one, say what happened.
            slot.reason = slot.reason or str(error)
            return False
        if _identity(_project(latest), wide=slot.wide) != slot.identity:
            raise CommandError("VS Code project changed during restoration")
        # A workspace switch reopens plain text files only; diffs, previews and
        # webviews are not part of what On has to bring back.
        saved_list = saved_text_editors(slot.item) if workspace_switch else slot.item.get("editor_uris", [])
        live_editors = _live_text_editors if workspace_switch else (lambda state: set(state.get("editor_uris", [])))
        saved_editors = set(saved_list)
        missing_editors = saved_editors - live_editors(latest)
        if workspace_switch and slot.place_error is not None:
            raise slot.place_error
        if workspace_switch:
            verified_binding = _project_request(latest, slot.selected["instance"])
        if workspace_switch and missing_editors:
            if "workspace_switch" not in latest.get("capabilities", ()):
                raise CommandError("Reload the VS Code companion to reopen the saved editors")
            latest = _request(endpoint, "restore-editors", timeout=_remaining(deadline, 8),
                              payload={**verified_binding,
                                       "editors": [uri for uri in saved_list if uri in missing_editors]})
            missing_editors = saved_editors - live_editors(latest)
        if missing_editors or latest.get("dirty_count", 0) < slot.item.get("dirty_count", 0):
            slot.content_evidence = PhaseEvidence(EvidenceState.UNKNOWN, "Native editor/dirty recovery is unverified; project reopening alone is insufficient")
            slot.attention = (slot.content_evidence.detail,)
            raise CommandError(slot.content_evidence.detail)
        if slot.launch_owned:
            marker, file = slot.marker
            outcome = _request(endpoint, "drop-launch-marker", marker, timeout=_remaining(deadline, 2),
                               payload=verified_binding)
            if outcome.get("removed") is not True:
                raise CommandError("VS Code did not safely remove the scoped launch tab")
            try:
                if file.stat().st_size == 0:
                    file.unlink()
            except FileNotFoundError:
                pass
        slot.content_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Project and saved editor metadata verified; buffer contents not inspected")
        return True

    deferred = []
    for index, item in enumerate(windows):
        slot = types.SimpleNamespace(
            index=index, item=item, identity=None, wide=False, target=None, selected=None, reused=False,
            identified=False, reason="", place_error=None, attention=(), marker=None, launch_owned=False,
            identity_evidence=PhaseEvidence(), content_evidence=PhaseEvidence(),
            placement_evidence=PhaseEvidence(EvidenceState.SKIPPED if no_place else EvidenceState.UNKNOWN))
        try:
            if time.monotonic() >= deadline:
                raise CommandError("VS Code restoration deadline exceeded")
            slot.target = item["placement"] if no_place else _target(item["placement"])
            # Only a login restore meets records saved without the text list.
            slot.wide = not workspace_switch and "_text_editor_uris" not in item
            slot.identity = _identity(item, wide=slot.wide)
            identity = slot.identity
            target = slot.target
            scope = item["user_data_dir"]
            block_native = scope in blocked_native_scopes
            label = item["workspace_file"] or (item["folders"][0]["uri"] if item["folders"] else "empty editor")
            report("running", f"VS Code window {index + 1}: checking {label}", index)
            if workspace_switch:
                slot.marker = _marker_for(switch_token, item["_native_id"])

            def owns_launch(window):
                return slot.marker and window.get("workspace_launch_markers") == [slot.marker[0]]

            def candidates(*, only_owned=False):
                return [window for window in current if window["instance"] not in claimed
                        and _identity(window, wide=slot.wide) == identity
                        and (not block_native or window["instance"] not in initial_instances
                             or window["shell"]["workspace"] == target["workspace"])
                        and (not workspace_switch or (
                            window["shell"]["id"] not in excluded_windows and not window["shell"].get("sticky")
                            and (owns_launch(window) or (
                                 not only_owned and window["shell"]["workspace"] == target["workspace"]
                                 and not window.get("workspace_launch_markers")
                                 and _live_text_editors(window) == set(saved_text_editors(item))
                                 and window.get("dirty_count") == 0))))]
            matches = candidates()
            if (not workspace_switch and not block_native and not matches and scope not in started
                    and not any(window["user_data_dir"] == scope for window in current)):
                bootstrap = next((saved for saved in windows if saved["user_data_dir"] == scope and saved["kind"] in {"empty", "untitled"}), item)
                _verify_profile(bootstrap)
                for folder in bootstrap["folders"]:
                    _check_resource(folder["uri"], folder=True, deadline=deadline)
                if bootstrap["kind"] == "workspace":
                    _check_resource(bootstrap["workspace_file"], folder=False, deadline=deadline)
                # Empty/untitled windows need native backup recovery. Prefer a
                # no-target bootstrap for that scope, even if the first saved
                # occurrence happens to be an ordinary folder window.
                launch_graphical_service(_command(bootstrap, native=True), "vscode-native-recovery")
                started.add(scope)
                current = _wait_for_live_windows(
                    live, deadline, scope=scope,
                    reporter=lambda message: report("running", f"VS Code window {index + 1}: {message}", index),
                )
                matches = candidates()
            if not matches:
                # Native recovery can add windows after the earlier sample.
                # Reconcile those before launching another project, including
                # companions still activating after a previous saved window.
                current = _wait_for_live_windows(
                    live, deadline,
                    reporter=lambda message: report("running", f"VS Code window {index + 1}: {message}", index),
                )
                matches = candidates()
            if not matches:
                if block_native and item["kind"] not in {"folder", "workspace"}:
                    raise CommandError("VS Code needs native recovery for this window; enable the parked workspace or finish its pending switch first")
                _verify_profile(item)
                argv = _command(item)
                for folder in item["folders"]:
                    _check_resource(folder["uri"], folder=True, deadline=deadline)
                if item["kind"] == "workspace":
                    _check_resource(item["workspace_file"], folder=False, deadline=deadline)
                if (workspace_switch or block_native) and not any(window["user_data_dir"] == scope for window in current):
                    _verify_scoped_cold_start(scope)
                if slot.marker:
                    file = slot.marker[1]
                    file.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
                    file.parent.chmod(0o700)
                    try:
                        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
                    except FileExistsError:
                        if file.is_symlink() or not file.is_file() or file.stat().st_size:
                            raise CommandError("VS Code launch marker is no longer an empty file")
                    else:
                        os.close(fd)
                    argv += ["--file-uri", file.as_uri()]
                launch_graphical_service(argv, "vscode-project")
                while time.monotonic() < deadline:
                    try:
                        current = live.get(deadline, strict=False)
                    except CompanionNotReady:
                        time.sleep(min(.2, max(0, deadline - time.monotonic())))
                        continue
                    matches = candidates(only_owned=workspace_switch)
                    if workspace_switch and len(matches) > 1:
                        raise CommandError("Multiple VS Code windows match the scoped launch")
                    if matches:
                        break
                    time.sleep(.2)
                if not matches:
                    raise CommandError("VS Code did not acknowledge the exact project/profile before timeout")
            # Same-project windows are distinct occurrences. Prefer an already
            # correctly placed occurrence; claim before placement even if it fails.
            matches.sort(key=lambda window: (window["shell"].get("workspace") != target["workspace"], window["shell"].get("monitor") != target.get("monitor")))
            selected = matches[0]
            slot.reused = selected["instance"] in initial_instances
            slot.launch_owned = bool(owns_launch(selected))
            slot.selected = selected
            claimed.add(selected["instance"])
            if advance(slot):
                finish(slot)
            else:
                report("running", f"VS Code window {index + 1}: {slot.reason}; retrying after the other windows", restored)
                deferred.append(slot)
        except (CommandError, OSError, ValueError) as error:
            conclude(slot, error)
    # Busy companions are retried round-robin so one window cannot starve the others.
    last_report = time.monotonic()
    while deferred and time.monotonic() < deadline:
        for slot in list(deferred):
            try:
                finished, error = advance(slot), None
            except (CommandError, OSError, ValueError) as caught:
                finished, error = True, caught
            if finished:
                deferred.remove(slot)
                finish(slot, error)
        if deferred and time.monotonic() - last_report >= 2:
            last_report = time.monotonic()
            for slot in deferred:
                report("running", f"VS Code window {slot.index + 1}: {slot.reason}; still retrying", restored)
        if deferred:
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
    for slot in deferred:
        # Nothing observes VS Code content after this stage, so a waiting phase
        # would hold login finalization until the operation deadline. A busy
        # companion ends as a retryable failure; its placement stays tracked.
        busy = PhaseEvidence(EvidenceState.FAILED, slot.reason, True)
        if slot.identified:
            slot.content_evidence = busy
        else:
            slot.identity_evidence = busy
        conclude(slot, CommandError(slot.reason))
    if settling:
        options = {slot.index: (slot.selected["shell"]["id"], slot.target, (
            {"require_local": not slot.launch_owned, "scoped": True} if workspace_switch else {})) for slot in settling}
        try:
            outcome = _keep_placements(options, deadline)
        except (CommandError, OSError) as error:
            outcome = dict.fromkeys(options, error)
        for slot in settling:
            error = outcome[slot.index]
            if error is not None:
                slot.place_error = error
                slot.placement_evidence = PhaseEvidence(
                    EvidenceState.WAITING if isinstance(error, PlacementPending) else EvidenceState.FAILED,
                    str(error), True, getattr(error, "request_id", None))
            conclude(slot)
    if errors:
        message = "; ".join(errors)
        report("waiting" if waiting_only(evidence_results) else "failed", message, restored)
        raise ProviderRestoreError(message, evidence_results)
    report("ready", f"Restored {restored} VS Code project window(s) on their saved workspaces and displays", len(windows))
    return ProviderCount(restored, evidence_results)
