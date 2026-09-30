"""VS Code project recipes and verified native GNOME placement.

The local UI companion supplies project URIs, never editor contents. Short-lived
read-only identification panels bind a companion instance to a native window;
neither a shared Electron PID nor a human-readable project title is an identity.
VS Code remains the owner of editor state and Hot Exit backups.
"""
from __future__ import annotations

import copy
import errno
import json
import os
import re
import socket
import stat
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Callable, Any
from urllib.parse import unquote, urlsplit

from .desktop import (capture_shell, serialized_placement, remap_monitor,
                      remap_workspace, move_window_result)
from .file_manager import DRIVES, DRIVE_ROOT, PLACEMENT_KEYS
from .util import CommandError, launch_graphical_service
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount, ProviderRestoreError, placement_matches, placement_accepted, placement_pending, PlacementPending)
from .provider_results import waiting_only

MAX_WINDOWS = 32
MAX_MESSAGE = 1024 * 1024
ALIASES = {"code", "code.desktop", "visual-studio-code", "com.visualstudio.code", "com.microsoft.vscode"}
Reporter = Callable[[str, str, int, int], None]


class UnsafeEditorState(CommandError):
    """A stale project recipe cannot protect live unsaved editor contents."""


class CompanionNotReady(CommandError):
    """A native Code window is not yet discoverable through its companion."""


def _remaining(deadline: float | None, limit: float) -> float:
    remaining = limit if deadline is None else min(limit, deadline - time.monotonic())
    if remaining <= 0:
        raise CommandError("VS Code restoration deadline exceeded")
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


def _request(endpoint: Path, method: str, token: str | None = None, *, timeout: float = 1.0) -> dict:
    try:
        return _request_once(endpoint, method, token, timeout=timeout)
    except TimeoutError as error:
        # A listening extension host can still be busy activating other
        # extensions. Its short RPC budget must not bypass the longer startup
        # readiness deadline, or make an unknown native window look absent.
        raise CompanionNotReady("VS Code companion is busy or still starting") from error


def _request_once(endpoint: Path, method: str, token: str | None = None, *, timeout: float = 1.0) -> dict:
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
        request = {"version": 1, "method": method}
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
        if method == "state" and (result.get("pid") != peer_pid or result.get("instance") != endpoint.stem or result.get("version") != 1):
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


def _project(state: dict) -> dict:
    workspace = state.get("workspace_file")
    folders = copy.deepcopy(state.get("folders", []))
    kind = ("untitled" if str(workspace).startswith("untitled:") else "workspace") if workspace else "folder" if folders else "empty"
    item = {key: copy.deepcopy(state.get(key)) for key in (
        "profile", "user_data_dir", "remote_name", "window_key", "hot_exit",
    )}
    item.update(kind=kind, workspace_file=workspace, folders=folders,
                editor_uris=state.get("editor_uris", []), dirty_count=state.get("dirty_count", 0))
    _validate_project(item)
    return item


def _identity(item: dict) -> tuple:
    profile = item.get("profile") or {}
    identity = (item["user_data_dir"], profile.get("id"), item.get("remote_name"), item["kind"], item.get("workspace_file"),
                tuple(folder["uri"] for folder in item["folders"]))
    # Empty editor windows have no project URI. Match native recovery metadata;
    # never copy buffer contents or guess that an arbitrary blank window is it.
    if item["kind"] == "empty":
        identity += (tuple(sorted(item.get("editor_uris", []))), item.get("dirty_count", 0))
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
            result.append({**item, "instance": instance, "endpoint": state["endpoint"], "shell": native})
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

    @serialized_placement
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


def capture_vscode(shell: dict | None = None) -> dict:
    shell = shell if shell is not None else capture_shell()
    if not shell.get("available"):
        raise CommandError("Cannot capture VS Code without GNOME window state")
    record = {"provider": "vscode", "version": 1, "windows": []}
    before = matching_windows(shell)
    if not before:
        return record
    captured = _LiveWindows().get(time.monotonic() + 15)
    if {item["shell"]["id"] for item in captured} != {window["id"] for window in before}:
        raise CommandError("VS Code windows changed while capturing; checkpoint not replaced")
    workspaces = {item["index"]: item["name"] for item in shell.get("workspaces", [])}
    # Identification is temporary; original placement/title precedes any panel.
    originals = {window["id"]: window for window in before}
    for live in captured:
        window = originals[live["shell"]["id"]]
        item = _project(live)
        item["placement"] = {key: copy.deepcopy(window[key]) for key in PLACEMENT_KEYS if key in window}
        item["placement"]["workspace_name"] = workspaces.get(window.get("workspace"), "")
        if item["dirty_count"] and item.get("hot_exit") not in {"onExit", "onExitAndWindowClose"}:
            raise UnsafeEditorState("VS Code has unsaved edits without verified Hot Exit recovery; save them or enable native recovery before shutdown")
        record["windows"].append(item)
    validate_vscode(record)
    return record


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
    elif not native and (item["kind"] == "untitled" or item.get("editor_uris") or item.get("dirty_count")):
        raise CommandError("VS Code did not recover the saved untitled/empty editor window; refusing to replace unsaved state")
    return argv


def _target(placement: dict) -> dict:
    shell = capture_shell()
    names = [item.get("name") for item in shell.get("workspaces", [])]
    if not shell.get("available") or names.count(placement["workspace_name"]) != 1:
        raise CommandError("Saved VS Code GNOME workspace is unavailable or ambiguous")
    return remap_monitor(remap_workspace(placement), require_identity=True)


@serialized_placement
def _place(wid: int, target: dict, deadline: float) -> None:
    stable_since, staging = None, None
    pending = False
    request_id = None
    last_move = float("-inf")
    while time.monotonic() < deadline:
        shell = capture_shell()
        window = next((item for item in matching_windows(shell) if item["id"] == wid), None)
        if not window:
            raise CommandError("VS Code window closed before placement was verified")
        expected = staging or target
        correct = placement_matches(window, expected)
        if correct:
            stable_since = stable_since if stable_since is not None else time.monotonic()
            if time.monotonic() - stable_since >= .4:
                if staging is None:
                    return
                minimize = target["state"] == "minimized" and staging["state"] != "minimized"
                destination = dict(staging, state="minimized") if minimize else target
                result = move_window_result(wid, destination)
                request_id = result.get("token")
                pending = placement_pending(result)
                if not placement_accepted(result):
                    raise CommandError("GNOME rejected final VS Code placement")
                staging = destination if minimize else None
                stable_since, last_move = None, time.monotonic()
        else:
            stable_since = None
            if time.monotonic() - last_move >= 1:
                active = shell.get("active_workspace")
                if type(active) is not int:
                    raise CommandError("GNOME active workspace is unavailable")
                staging = dict(target, workspace=active) if active != target["workspace"] or target["state"] == "minimized" else None
                if staging:
                    staging.pop("workspace_name", None)
                    if staging["state"] == "minimized":
                        staging["state"] = "normal"
                result = move_window_result(wid, staging or target)
                request_id = result.get("token")
                pending = placement_pending(result)
                if not placement_accepted(result):
                    raise CommandError("GNOME rejected VS Code placement")
                last_move = time.monotonic()
        time.sleep(.15)
    if staging is not None:
        # A staging receipt cannot finish the unrequested final placement.
        raise CommandError("VS Code placement timed out before requesting its final saved placement; retry restoration")
    if pending:
        raise PlacementPending("VS Code placement accepted; awaiting compositor verification", request_id)
    raise CommandError("VS Code window did not settle on its saved workspace/display/geometry")


def restore_vscode(record: dict | None, *, dry_run: bool = False, no_place: bool = False,
                   workspace: str | None = None, reporter: Reporter | None = None,
                   timeout: float = 30) -> int:
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
    evidence_results = []
    restored = 0
    for index, item in enumerate(windows):
        identity_evidence = PhaseEvidence()
        content_evidence = PhaseEvidence()
        placement_evidence = PhaseEvidence(EvidenceState.SKIPPED if no_place else EvidenceState.UNKNOWN)
        attention = ()
        reused = False
        try:
            if time.monotonic() >= deadline:
                raise CommandError("VS Code restoration deadline exceeded")
            target = item["placement"] if no_place else _target(item["placement"])
            identity = _identity(item)
            label = item["workspace_file"] or (item["folders"][0]["uri"] if item["folders"] else "empty editor")
            report("running", f"VS Code window {index + 1}: checking {label}", index)
            def candidates():
                return [window for window in current if window["instance"] not in claimed and _identity(window) == identity]
            matches = candidates()
            scope = item["user_data_dir"]
            if not matches and not any(window["user_data_dir"] == scope for window in current) and scope not in started:
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
                _verify_profile(item)
                argv = _command(item)
                for folder in item["folders"]:
                    _check_resource(folder["uri"], folder=True, deadline=deadline)
                if item["kind"] == "workspace":
                    _check_resource(item["workspace_file"], folder=False, deadline=deadline)
                launch_graphical_service(argv, "vscode-project")
                while time.monotonic() < deadline:
                    try:
                        current = live.get(deadline, strict=False)
                    except CompanionNotReady:
                        time.sleep(min(.2, max(0, deadline - time.monotonic())))
                        continue
                    matches = candidates()
                    if matches:
                        break
                    time.sleep(.2)
                if not matches:
                    raise CommandError("VS Code did not acknowledge the exact project/profile before timeout")
            # Same-project windows are distinct occurrences. Prefer an already
            # correctly placed occurrence; claim before placement even if it fails.
            matches.sort(key=lambda window: (window["shell"].get("workspace") != target["workspace"], window["shell"].get("monitor") != target.get("monitor")))
            selected = matches[0]
            reused = selected["instance"] in initial_instances
            claimed.add(selected["instance"])
            if _identity(_project(_request(selected["endpoint"], "state", timeout=_remaining(deadline, 1)))) != identity:
                raise CommandError("VS Code project changed before placement; window left untouched")
            identity_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Exact project/profile and companion instance")
            probe = _request(selected["endpoint"], "probe", timeout=_remaining(deadline, 2))
            content_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Project storage is ready; editor buffers remain owned by VS Code")
            if probe.get("ready") is not True:
                content_evidence = PhaseEvidence(EvidenceState.WAITING, "VS Code project storage or remote connection is not ready", True)
                raise CommandError(content_evidence.detail)
            latest = _request(selected["endpoint"], "state", timeout=_remaining(deadline, 1))
            if _identity(_project(latest)) != identity:
                raise CommandError("VS Code project changed during restoration")
            missing_editors = set(item.get("editor_uris", [])) - set(latest.get("editor_uris", []))
            if missing_editors or latest.get("dirty_count", 0) < item.get("dirty_count", 0):
                content_evidence = PhaseEvidence(EvidenceState.UNKNOWN, "Native editor/dirty recovery is unverified; project reopening alone is insufficient")
                attention = (content_evidence.detail,)
                raise CommandError(content_evidence.detail)
            content_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Project and saved editor metadata verified; buffer contents not inspected")
            if not no_place:
                _place(selected["shell"]["id"], target, deadline)
                placement_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Observed saved native placement")
            restored += 1
            report("running", f"VS Code window {index + 1}: project and placement verified", restored)
        except (CommandError, OSError, ValueError) as error:
            errors.append(f"VS Code window {index + 1}: {error}")
            if identity_evidence.state == EvidenceState.UNKNOWN:
                identity_evidence = PhaseEvidence(EvidenceState.FAILED, str(error))
            elif placement_evidence.state == EvidenceState.UNKNOWN and content_evidence.state == EvidenceState.VERIFIED:
                placement_evidence = PhaseEvidence(EvidenceState.WAITING if isinstance(error, PlacementPending) else EvidenceState.FAILED,
                                                  str(error), True, getattr(error, "request_id", None))
            report("running", errors[-1], restored)
        finally:
            evidence_results.append(ProviderItemResult("vscode", str(index + 1), identity_evidence,
                content_evidence, placement_evidence, attention=attention, reused=reused))
    if errors:
        message = "; ".join(errors)
        report("waiting" if waiting_only(evidence_results) else "failed", message, restored)
        raise ProviderRestoreError(message, evidence_results)
    report("ready", f"Restored {restored} VS Code project window(s) on their saved workspaces and displays", len(windows))
    return ProviderCount(restored, evidence_results)
