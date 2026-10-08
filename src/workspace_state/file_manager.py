"""Save and restore the default file manager, using Nemo's opt-in bridge.

Folder titles are not paths. The bridge exposes actual tab URIs and leases
temporary title markers solely to correlate GTK windows with GNOME window IDs.
"""
from __future__ import annotations

import ast
import json
import os
import posixpath
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlsplit

from . import operations
from .desktop import (
    placement_lock,
    cancel_expected_window, capture_shell, expect_window, move_window_result,
    resolve_placement_target,
)
from .util import CommandError, launch_graphical_service
from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, ProviderCount,
                              ProviderRestoreError, PlacementPending, placement_accepted,
                              placement_pending, placement_matches, waiting_only)

BUS_NAME = "org.sagecat.WorkspaceState.Nemo1"
OBJECT_PATH = "/org/sagecat/WorkspaceState/Nemo1"
ALIASES = {"nemo", "nemo.desktop", "org.nemo"}
MAX_WINDOWS = 32
MAX_TABS = 32
PLACEMENT_KEYS = (
    "workspace", "monitor", "monitor_identity", "monitor_intent",
    "monitor_geometry", "geometry", "geometry_relative", "state",
)
DRIVE_ROOT = Path("/home/sagecat/Drives")
DRIVES = {
    DRIVE_ROOT / "gdrive": "rclone-gdrive.service",
    DRIVE_ROOT / "pdrive": "protondrive-mount-pdrive.service",
    DRIVE_ROOT / "sagecat-serv-drive": "rclone-sagecat-serv-drive.service",
    DRIVE_ROOT / "windows_vm/Desktop": "windows-vm-desktop-mount.service",
}
SCHEMES = {"file", "trash", "computer", "network", "recent", "smb", "sftp",
           "dav", "davs", "ftp", "afp", "mtp", "gphoto2"}
Reporter = Callable[[str, str, int, int], None]


def _uri(uri: Any):
    if not isinstance(uri, str) or not uri or len(uri) > 8192:
        raise ValueError("Invalid file manager folder URI")
    if any(ord(char) < 32 or ord(char) == 127 for char in unquote(uri)):
        raise ValueError("File manager folder URI contains control characters")
    parsed = urlsplit(uri)
    if parsed.scheme not in SCHEMES or parsed.password is not None:
        raise ValueError("Unsupported or credential-bearing file manager folder URI")
    if parsed.query or parsed.fragment:
        raise ValueError("File manager folder URI must not contain a query or fragment")
    if parsed.scheme == "file" and (parsed.netloc not in {"", "localhost"} or not parsed.path.startswith("/")):
        raise ValueError("File manager local folder URI must be absolute")
    return parsed


def _local_folder(uri: str) -> Path | None:
    parsed = _uri(uri)
    return Path(posixpath.normpath(unquote(parsed.path))) if parsed.scheme == "file" else None


def needs_storage(record: dict | None) -> bool:
    """Pure classifier: never touch an unavailable FUSE mount during startup."""
    for window in (record or {}).get("windows", []):
        for uri in window.get("locations", []):
            folder = _local_folder(uri)
            if folder is None or folder.is_relative_to(DRIVE_ROOT):
                return True
    return False


def validate_file_manager(record: Any) -> None:
    if not isinstance(record, dict) or record.get("provider") != "nemo" or record.get("desktop_id") != "nemo.desktop":
        raise ValueError("Unsupported default file manager checkpoint (expected Nemo)")
    windows = record.get("windows")
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ValueError("Invalid file manager window list")
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("Invalid file manager window")
        if "title" in window and (not isinstance(window["title"], str) or len(window["title"]) > 8192):
            raise ValueError("Invalid file manager window title")
        locations = window.get("locations")
        if not isinstance(locations, list) or not 1 <= len(locations) <= MAX_TABS:
            raise ValueError("Invalid file manager tab list")
        for uri in locations:
            _uri(uri)
        active = window.get("active_tab")
        if type(active) is not int or not 0 <= active < len(locations):
            raise ValueError("Invalid file manager active tab")
        placement = window.get("placement")
        if not isinstance(placement, dict) or not isinstance(placement.get("workspace_name"), str) or not placement["workspace_name"]:
            raise ValueError("File manager window has no workspace name")
        if type(placement.get("workspace")) is not int or placement["workspace"] < 0:
            raise ValueError("Invalid file manager workspace")
        identity = placement.get("monitor_intent") or placement.get("monitor_identity")
        if not isinstance(identity, dict) or not any(identity.get(key) for key in ("edid_hash", "serial", "connector")):
            raise ValueError("File manager window has no display identity")
        if placement.get("state") not in {"normal", "minimized", "maximized", "fullscreen"}:
            raise ValueError("Invalid file manager window state")
        for key in ("geometry", "geometry_relative"):
            geometry = placement.get(key)
            if not isinstance(geometry, dict) or any(type(geometry.get(field)) is not int for field in ("x", "y", "width", "height")) or geometry["width"] <= 0 or geometry["height"] <= 0:
                raise ValueError("Invalid file manager window geometry")


def matching_windows(shell: dict) -> list[dict]:
    result = []
    for window in shell.get("windows", []):
        aliases = {str(value).lower() for value in window.get("app_ids", [])}
        aliases.update(str(window.get(key) or "").lower() for key in ("app_id", "wm_class"))
        if aliases & ALIASES:
            result.append(window)
    return result


def _bridge(method: str, *args: Any) -> Any:
    try:
        result = subprocess.run(
            ["/usr/bin/gdbus", "call", "--session", "--dest", BUS_NAME,
             "--object-path", OBJECT_PATH, "--method", f"{BUS_NAME}.{method}",
             "--timeout", "2", *[str(arg) for arg in args]],
            capture_output=True, text=True, timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CommandError("Nemo state bridge did not respond within 3 seconds") from error
    if result.returncode:
        raise CommandError("Nemo state bridge unavailable; install the Nemo integration and reopen Nemo")
    if method in {"Release", "SelectTab", "CloseWindow"}:
        return result.stdout.strip() == "(true,)"
    try:
        return json.loads(ast.literal_eval(result.stdout)[0])
    except (ValueError, TypeError, SyntaxError, IndexError) as error:
        raise CommandError("Nemo state bridge returned an invalid response") from error


class _LiveWindows:
    def __init__(self):
        self.ids: dict[tuple[int, int], int] = {}

    def get(self, *, strict: bool = True) -> list[dict]:
        shell = capture_shell()
        if not shell.get("available"):
            raise CommandError("Cannot inspect Nemo: GNOME window state is unavailable")
        visible = matching_windows(shell)
        if not visible:
            return []
        state = _bridge("GetState")
        pid = state.get("pid")
        entries = state.get("windows", [])
        visible_ids = {item["id"] for item in visible}
        if any(self.ids.get((pid, item["id"])) not in visible_ids for item in entries) or not visible_ids.issubset(set(self.ids.values())):
            lease = uuid.uuid4().hex
            try:
                identified = _bridge("Identify", lease)
                if not identified.get("ok") or identified.get("pid") != pid:
                    raise CommandError("Nemo window identification is busy or its process changed; retry saving")
                deadline = time.monotonic() + 1.5
                while True:
                    visible = matching_windows(capture_shell())
                    for item in identified.get("windows", []):
                        candidates = [window for window in visible if window.get("pid") == pid and item.get("marker") and window.get("title") == item["marker"]]
                        if len(candidates) == 1:
                            self.ids[(pid, item["id"])] = candidates[0]["id"]
                    if all(window["id"] in self.ids.values() for window in visible) or time.monotonic() >= deadline:
                        break
                    time.sleep(.05)
            finally:
                # The bridge also expires the lease if this process is killed.
                _bridge("Release", lease)
            shell = capture_shell()
            visible = matching_windows(shell)
            state = _bridge("GetState")
            if state.get("pid") != pid:
                raise CommandError("Nemo restarted during window capture; retry saving")
            entries = state.get("windows", [])
        result = []
        for item in entries:
            wid = self.ids.get((pid, item["id"]))
            window = next((value for value in visible if value["id"] == wid and value.get("pid") == pid), None)
            if window and item.get("complete"):
                result.append(dict(item, pid=pid, shell=window))
        if strict and {item["shell"]["id"] for item in result} != {window["id"] for window in visible}:
            raise CommandError("Nemo cannot capture every tab/window yet (loading or split-pane view); checkpoint not replaced")
        return result


def capture_file_manager(shell: dict) -> dict:
    if not shell.get("available"):
        raise CommandError("Cannot capture default file manager: GNOME window state is unavailable")
    record = {"provider": "nemo", "desktop_id": "nemo.desktop", "windows": []}
    expected = {window["id"] for window in matching_windows(shell)}
    if not expected:
        return record
    names = {item["index"]: item["name"] for item in shell.get("workspaces", [])}
    windows = _LiveWindows().get()
    if {item["shell"]["id"] for item in windows} != expected:
        raise CommandError("Nemo windows changed while saving; retry the checkpoint")
    for item in windows:
        window = item["shell"]
        placement = {key: window.get(key) for key in PLACEMENT_KEYS}
        placement["workspace_name"] = names.get(window.get("workspace"))
        record["windows"].append({"locations": item["locations"], "active_tab": item["active_tab"], "title": window.get("title", ""), "placement": placement})
    validate_file_manager(record)
    return record


def _default_is_nemo() -> bool:
    try:
        result = subprocess.run(["xdg-mime", "query", "default", "inode/directory"], capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and result.stdout.strip() == "nemo.desktop"
    except (OSError, subprocess.TimeoutExpired):
        return False


def _check_folder(uri: str) -> None:
    folder = _local_folder(uri)
    if folder and folder.is_relative_to(DRIVE_ROOT):
        mounts = [(mount, unit) for mount, unit in DRIVES.items() if folder.is_relative_to(mount)]
        if not mounts:
            raise CommandError("Saved Nemo folder is under an unmanaged/unverified drive")
        mount, unit = max(mounts, key=lambda item: len(item[0].parts))
        for command in (["/usr/bin/systemctl", "--user", "is-active", "--quiet", unit],
                        ["/usr/bin/findmnt", "--mountpoint", str(mount), "--noheadings"]):
            try:
                probe = subprocess.run(command, capture_output=True, timeout=2)
            except (OSError, subprocess.TimeoutExpired) as error:
                raise CommandError(f"Nemo is waiting for mounted storage: {unit}") from error
            if probe.returncode:
                raise CommandError(f"Nemo is waiting for mounted storage: {unit}")
    try:
        probe = subprocess.run(["/usr/bin/gio", "info", "--attributes=standard::type", "--", uri],
                               capture_output=True, text=True, timeout=3,
                               env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CommandError("Saved Nemo folder is unavailable (directory probe timed out)") from error
    if probe.returncode or not any(line.strip() == "standard::type: 2" for line in probe.stdout.splitlines()):
        raise CommandError("Saved Nemo folder is unavailable or no longer a directory")


def _target(placement: dict) -> dict:
    return resolve_placement_target(placement, provider="Nemo", shell=capture_shell())


def _place(wid: int, target: dict, deadline: float) -> None:
    owner = operations.current()
    _placement_authority(owner)
    if owner is not None:
        deadline = min(deadline, owner.deadline)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CommandError("Nemo placement deadline elapsed before any placement request")
    if not placement_lock.acquire(timeout=remaining):
        raise CommandError("Nemo placement deadline elapsed while waiting for another window; no placement requested")
    try:
        _placement_authority(owner)
        if time.monotonic() >= deadline:
            raise CommandError("Nemo placement deadline elapsed while waiting for another window; no placement requested")
        _place_locked(wid, target, deadline, owner)
    finally:
        placement_lock.release()


def _placement_authority(owner) -> None:
    if owner is None:
        if operations.current() is not None:
            raise CommandError("Nemo restoration operation changed")
        return
    try:
        owner.check()
        if operations.current() != owner:
            raise CommandError("Nemo restoration operation changed")
        if owner.mode == "startup":
            # Reuse the established startup mutation gate, including a changed
            # status owner, cancellation and suspension after a blocking reply.
            from .browser import browser_continuation_guard
            browser_continuation_guard(owner)
    except (TimeoutError, OSError, ValueError, CommandError) as error:
        raise CommandError("Nemo restoration no longer owns a live operation: " + str(error)) from error


def _place_locked(wid: int, target: dict, deadline: float, owner) -> None:
    stable_since = None
    last_move = float("-inf")
    staging = None
    pending = False
    request_id = None
    requested = False
    while time.monotonic() < deadline:
        _placement_authority(owner)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        shell = capture_shell(timeout=min(10, remaining))
        _placement_authority(owner)
        if time.monotonic() >= deadline:
            break
        window = next((item for item in matching_windows(shell) if item["id"] == wid), None)
        if not window:
            raise CommandError("Nemo window closed before its placement was verified")
        expected = staging or target
        correct = placement_matches(window, expected)
        if correct:
            stable_since = stable_since if stable_since is not None else time.monotonic()
            if time.monotonic() - stable_since >= .4:
                if staging is None:
                    return
                # On an inactive workspace Mutter defers the entire request,
                # including minimize. Apply that state while still active,
                # verify it, then hand the exact window to its destination.
                minimize_first = target["state"] == "minimized" and staging["state"] != "minimized"
                destination = dict(staging, state="minimized") if minimize_first else target
                _placement_authority(owner)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                requested = True
                result = move_window_result(wid, destination, timeout=min(10, remaining))
                _placement_authority(owner)
                pending, request_id = placement_pending(result), result.get("token")
                if not placement_accepted(result):
                    raise CommandError("GNOME rejected final Nemo window placement")
                staging = destination if minimize_first else None
                stable_since = None
                last_move = time.monotonic()
        else:
            stable_since = None
            if time.monotonic() - last_move >= 1:
                active = shell.get("active_workspace")
                if type(active) is not int:
                    raise CommandError("GNOME active workspace is unavailable")
                staging = dict(target, workspace=active) if active != target["workspace"] or target["state"] == "minimized" else None
                if staging:
                    staging.pop("workspace_name", None)
                    # Minimized windows cannot process a Wayland resize until
                    # mapped; stage visibly, then minimize at the destination.
                    if staging["state"] == "minimized":
                        staging["state"] = "normal"
                _placement_authority(owner)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                requested = True
                result = move_window_result(wid, staging or target, timeout=min(10, remaining))
                _placement_authority(owner)
                pending, request_id = placement_pending(result), result.get("token")
                if not placement_accepted(result):
                    raise CommandError("GNOME rejected Nemo window placement")
                last_move = time.monotonic()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.15, remaining))
    if not requested:
        raise CommandError("Nemo placement deadline elapsed before any placement request")
    if staging is not None:
        # A staging receipt cannot finish the unrequested final placement.
        raise CommandError("Nemo placement timed out before requesting its final saved placement; retry restoration")
    if pending:
        raise PlacementPending("Nemo placement accepted; awaiting compositor verification", request_id)
    raise CommandError("Nemo window did not settle on its saved workspace/display/geometry")


def restore_file_manager(record: dict | None, *, dry_run: bool = False,
                         no_place: bool = False, workspace: str | None = None,
                         reporter: Reporter | None = None, timeout: float = 30) -> int:
    """Restore each saved window within its own budget and the operation cap."""
    if record is not None:
        validate_file_manager(record)
    windows = [window for window in (record or {}).get("windows", []) if not workspace or window["placement"]["workspace_name"] == workspace]
    def report(state: str, message: str, current: int):
        print(message, flush=True)
        if reporter:
            reporter(state, message, current, max(1, len(windows)))
    if not windows:
        report("skipped", "No saved file manager windows; not launching Nemo", 1)
        return 0
    if dry_run:
        report("ready", f"Would restore {len(windows)} Nemo window(s) with their tabs and placement", len(windows))
        return len(windows)
    if not _default_is_nemo():
        raise CommandError("Saved file manager is Nemo but the default file manager has changed; nothing launched")
    live = _LiveWindows()
    claimed: set[tuple[int, int]] = set()
    errors = []
    evidence_results = []
    restored = 0
    owner = operations.current()
    for index, saved in enumerate(windows):
        # A preceding window's I/O and serialized placements must not consume
        # every later window's opportunity. The lifecycle deadline stays fixed.
        deadline = time.monotonic() + max(0.0, timeout)
        if owner is not None:
            deadline = min(deadline, owner.deadline)
        token = None
        identity_evidence = PhaseEvidence()
        content_evidence = PhaseEvidence()
        placement_evidence = PhaseEvidence(EvidenceState.SKIPPED if no_place else EvidenceState.UNKNOWN)
        try:
            _placement_authority(owner)
            if time.monotonic() >= deadline:
                raise CommandError("File manager restoration deadline exceeded")
            target = saved["placement"] if no_place else _target(saved["placement"])
            report("running", f"Nemo window {index + 1}: checking {len(saved['locations'])} tab(s)", index)
            candidates = [item for item in live.get() if (item["pid"], item["id"]) not in claimed and item["locations"] == saved["locations"]]
            candidates.sort(key=lambda item: (item["shell"].get("workspace") != target["workspace"], item["shell"].get("monitor") != target.get("monitor")))
            current = candidates[0] if candidates else None
            if current is None:
                for uri in saved["locations"]:
                    if time.monotonic() >= deadline:
                        raise CommandError("File manager restoration deadline exceeded")
                    _check_folder(uri)
                # Exact folder data, not a guessed title, selects the new window.
                before = {(item["pid"], item["id"]) for item in live.get()}
                if not no_place:
                    label = unquote(urlsplit(saved["locations"][0]).path).rstrip("/").rsplit("/", 1)[-1]
                    title = saved.get("title") or label
                    if title:
                        token = expect_window("nemo", target, title=title)
                argv = ["/usr/bin/nemo", "--no-default-window"]
                if len(saved["locations"]) > 1:
                    argv.append("--tabs")
                launch_graphical_service([*argv, "--", *saved["locations"]], "file-manager")
                while time.monotonic() < deadline:
                    matches = [item for item in live.get(strict=False) if (item["pid"], item["id"]) not in before and item["locations"] == saved["locations"]]
                    if len(matches) > 1:
                        raise CommandError("Multiple new Nemo windows match; refusing ambiguous placement")
                    if matches:
                        current = matches[0]
                        break
                    time.sleep(.2)
                if current is None:
                    raise CommandError("Nemo did not expose the requested window/tabs before the deadline")
            # A failed placement still belongs to this saved record; never
            # reuse that window for a later, identical-folder record.
            claimed.add((current["pid"], current["id"]))
            identity_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Exact Nemo bridge/native window")
            if not _bridge("SelectTab", current["id"], saved["active_tab"]):
                raise CommandError("Nemo could not select the saved active tab")
            verified = next((item for item in live.get() if item["pid"] == current["pid"] and item["id"] == current["id"]), None)
            if not verified or verified["locations"] != saved["locations"] or verified["active_tab"] != saved["active_tab"]:
                raise CommandError("Nemo tabs changed during restoration")
            content_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Folder URIs and active tab verified")
            if not no_place:
                _place(current["shell"]["id"], target, deadline)
                placement_evidence = PhaseEvidence(EvidenceState.VERIFIED, "Observed saved native placement")
            restored += 1
            report("running", f"Nemo window {index + 1}: tabs and placement restored", index + 1)
        except (CommandError, OSError, ValueError) as error:
            message = f"Nemo window {index + 1}: {error}"
            errors.append(message)
            if identity_evidence.state == EvidenceState.UNKNOWN:
                identity_evidence = PhaseEvidence(EvidenceState.FAILED, str(error))
            elif content_evidence.state == EvidenceState.UNKNOWN:
                content_evidence = PhaseEvidence(EvidenceState.FAILED, str(error))
            else:
                placement_evidence = PhaseEvidence(EvidenceState.WAITING if isinstance(error, PlacementPending) else EvidenceState.FAILED,
                                                  str(error), True, getattr(error, "request_id", None))
            report("running", message, index + 1)
        finally:
            evidence_results.append(ProviderItemResult("nemo", str(index + 1), identity_evidence,
                                                       content_evidence, placement_evidence))
            if token:
                cancel_expected_window(token)
    if errors:
        message = "; ".join(errors)
        report("waiting" if waiting_only(evidence_results) else "failed", message, len(windows))
        raise ProviderRestoreError(message, evidence_results)
    report("ready", f"Restored {restored} Nemo window(s), including tabs and placement", len(windows))
    return ProviderCount(restored, evidence_results)
