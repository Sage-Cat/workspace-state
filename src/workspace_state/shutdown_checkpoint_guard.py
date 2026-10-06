"""Retain verified pre-drain state when a cancelled shutdown is retried.

The immutable bundle is recovery evidence, never shutdown authorization. A new
operation must prove that the original partial desktop and tmux state have not
changed before it may reuse that evidence instead of capturing absent apps.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import time

from . import operations
from .desktop import capture_shell
from .graphical_drain import Manager, private_json, reconcile, validate_receipt
from .login_status import operation_path, runtime_root, status_path
from .storage import path_for, validate
from .util import atomic_json, data_home, run

_FILE = re.compile(r"tmux_resurrect_\d{8}T\d{6}\.txt")
_BUNDLE = re.compile(r"[0-9a-f]{32}-[0-9a-f]{32}\.json")
_LEDGER = re.compile(r"shutdown-graphical-drain-[0-9a-f]{32}\.json")
_MAX_BYTES = 16 * 1024 * 1024


def _pointer() -> Path:
    return runtime_root() / "shutdown-retry-protection.json"


def _ledgers():
    return [path for path in runtime_root().glob("shutdown-graphical-drain-*.json") if _LEDGER.fullmatch(path.name)]


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _encoded(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _private_bytes(path: Path, *, immutable: bool = False, private: bool = True) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(stream.fileno())
        if (not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.getuid()
                or meta.st_mode & (0o077 if private else 0o022) or meta.st_nlink != 1
                or (immutable and meta.st_mode & 0o222) or meta.st_size > _MAX_BYTES):
            raise ValueError(f"unsafe shutdown checkpoint file: {path}")
        return stream.read(_MAX_BYTES + 1)


@contextmanager
def _locks(*, runtime: bool = True):
    """Same order as continuum-save; never wait on the desktop main loop."""
    descriptors = []
    try:
        if runtime:
            root = runtime_root()
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptors.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC))
            fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        target = data_home() / "state.lock"
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptors.append(os.open(target, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600))
        fcntl.flock(descriptors[-1], fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    except BlockingIOError as error:
        raise RuntimeError("checkpoint save is still running; retry after it finishes") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _authority(context: operations.OperationContext) -> dict:
    context.check()
    document = private_json(status_path())
    if (context.mode != "shutdown" or operations.current() != context or not context.matches(document)
            or document.get("cancelled") is True
            or document.get("operation_state") not in {"preparing", "prepared", "authorized"}):
        raise ValueError("shutdown checkpoint ownership was withdrawn or replaced")
    return document


def _tmux_directory() -> Path:
    option = run(["tmux", "show-option", "-gqv", "@resurrect-dir"], timeout=2).strip()
    if not option:
        legacy = Path.home() / ".tmux/resurrect"
        return legacy if legacy.is_dir() else Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "tmux/resurrect"
    option = option.replace("$HOME", str(Path.home())).replace("$HOSTNAME", socket.gethostname())
    return Path(option).expanduser().absolute()


def _tmux_path() -> Path:
    directory = _tmux_directory().resolve()
    link = directory / "last"
    if not link.is_symlink():
        raise ValueError("tmux checkpoint has no exact last target")
    target = link.resolve(strict=True)
    if target.parent != directory or not _FILE.fullmatch(target.name):
        raise ValueError("tmux last target is outside its checkpoint directory")
    return target


def _sessions(value: list[dict]) -> list[dict]:
    return sorted([{"name": session["name"], "windows": sorted([
        {"index": window["index"], "name": window["name"], "layout": window["layout"],
         "active": window["active"], "panes": sorted([
             {key: pane.get(key) for key in ("index", "id", "pid", "start_ticks", "cwd", "command", "active", "label")}
             | {"codex": (pane.get("codex") or {}).get("session_id") if isinstance(pane.get("codex"), dict) else pane.get("codex")}
             for pane in window["panes"]], key=lambda item: item["index"])}
        for window in session["windows"]], key=lambda item: item["index"])}
        for session in value], key=lambda item: item["name"])


def _live_sessions() -> list[dict]:
    from .capture import codex_for_pane
    fields = ("session_name", "window_index", "window_name", "window_layout", "window_active",
              "pane_index", "pane_id", "pane_pid", "pane_current_path", "pane_current_command", "pane_active")
    output = run(["tmux", "list-panes", "-a", "-F", "\t".join("#{" + name + "}" for name in fields)], timeout=2)
    sessions = {}
    deadline = time.monotonic() + 5
    for line in output.splitlines():
        row = line.split("\t")
        if len(row) != len(fields) or time.monotonic() >= deadline:
            raise ValueError("tmux state cannot be proved within the retry budget")
        name, index, title, layout, active, pi, pane_id, pid, cwd, command, pa = row
        label = run(["tmux", "show-options", "-p", "-qv", "-t", pane_id, "@pane_label"], timeout=min(1, max(.01, deadline-time.monotonic())))
        session = sessions.setdefault(name, {"name": name, "windows": {}})
        window = session["windows"].setdefault(index, {"index": int(index), "name": title, "layout": layout, "active": active == "1", "panes": []})
        window["panes"].append({"index": int(pi), "id": pane_id, "cwd": cwd, "command": command,
                                "pid": int(pid), "start_ticks": _process(int(pid))["start_ticks"],
                                "active": pa == "1", "label": label.removesuffix("\n") if label else None,
                                "codex": codex_for_pane(int(pid), cwd, pane_id=pane_id)})
    return _sessions([dict(item, windows=list(item["windows"].values())) for item in sessions.values()])


def _file_record(path: Path, *, private: bool = True) -> dict:
    raw = _private_bytes(path, private=private)
    return {"path": str(path), "digest": _digest(raw), "bytes": base64.b64encode(raw).decode()}


def _verify_saved_sessions(snapshot: dict, live: list[dict], *, degraded: bool) -> None:
    saved = _sessions(snapshot.get("sessions", []))
    observed = _sessions(live)
    # Recipes omit volatile process identities. The bundle additionally pins
    # those identities, while this comparison checks the captured intent.
    identities = []
    for collection in (saved, observed):
        current = []
        for session in collection:
            for window in session["windows"]:
                for pane in window["panes"]:
                    current.append(pane.pop("codex"))
                    pane.pop("pid")
                    pane.pop("start_ticks")
        identities.append(current)
    if saved != observed:
        raise ValueError("tmux structure changed after save; checkpoint not sealed")
    for before, after in zip(*identities):
        if before and after and before != after or before and not after and not degraded:
            raise ValueError("tmux conversation identity changed after save; checkpoint not sealed")


def _record_bytes(record: dict) -> bytes:
    try:
        raw = base64.b64decode(record["bytes"], validate=True)
    except (KeyError, ValueError, TypeError) as error:
        raise ValueError("invalid sealed checkpoint bytes") from error
    if len(raw) > _MAX_BYTES or record.get("digest") != _digest(raw):
        raise ValueError("sealed checkpoint digest differs")
    return raw


def _bundle(descriptor: dict) -> dict:
    if (not isinstance(descriptor, dict) or descriptor.get("schema_version") != 1
            or not _BUNDLE.fullmatch(str(descriptor.get("bundle_name", "")))):
        raise ValueError("missing exact sealed shutdown checkpoint bundle")
    raw = _private_bytes(data_home() / "shutdown-checkpoints" / descriptor["bundle_name"], immutable=True)
    if _digest(raw) != descriptor.get("bundle_digest"):
        raise ValueError("shutdown checkpoint bundle changed")
    value = json.loads(raw)
    if not isinstance(value, dict) or not isinstance(value.get("canonical"), dict) or not isinstance(value.get("tmux"), dict) or not isinstance(value.get("sessions"), list) or not isinstance(value.get("native"), dict) or not isinstance(value.get("providers"), dict):
        raise ValueError("shutdown checkpoint bundle is malformed")
    context = operations.OperationContext.from_dict(value.get("operation_context"))
    if (value.get("schema_version") != 1 or context.mode != "shutdown"
            or descriptor["bundle_name"] != f"{context.operation_id}-{value.get('invocation_id')}.json"):
        raise ValueError("shutdown checkpoint bundle identity differs")
    _record_bytes(value["canonical"])
    _record_bytes(value["tmux"])
    return value


def _unchanged(value: dict) -> None:
    canonical = Path(value["canonical"]["path"])
    target = Path(value["tmux"]["path"])
    if canonical != path_for().absolute() or target != _tmux_path():
        raise ValueError("checkpoint paths changed after graphical drain")
    for key in ("canonical", "tmux"):
        record = value[key]
        if _private_bytes(Path(record["path"]), private=key == "canonical") != _record_bytes(record):
            raise ValueError("checkpoint changed after graphical drain; save current state explicitly")


def seal_checkpoint(context: operations.OperationContext, invocation_id: str, *, degraded: bool = False) -> dict:
    if not re.fullmatch(r"[0-9a-f]{32}", invocation_id):
        raise ValueError("checkpoint bundle needs exact worker invocation")
    with _locks():
        _authority(context)
        inherited = None
        if _pointer().exists():
            protection, previous, owner = _protected_record()
            ledger = _settled(protection, owner)
            _desktop_unchanged(protection, owner, ledger)
            inherited = protection.get("checkpoint_bundle")
            _unchanged(previous)
        canonical = _file_record(path_for().absolute())
        snapshot = json.loads(_record_bytes(canonical))
        validate(snapshot)
        # Safe fallback recipes may deliberately retain unresolved identities.
        # Pin the live tmux state separately instead of treating that recipe as
        # proof of what is currently running.
        live = _live_sessions()
        _verify_saved_sessions(snapshot, live, degraded=degraded or bool(inherited))
        shell = capture_shell(timeout=2)
        native = _native(shell)
        providers = _providers(shell)
        value = {"schema_version": 1, "operation_context": context.to_dict(), "invocation_id": invocation_id,
                 "canonical": canonical, "tmux": _file_record(_tmux_path(), private=False), "sessions": live,
                 "providers": providers, "native": native,
                 "degraded": degraded or bool(inherited and _bundle(inherited).get("degraded")), "inherited": inherited}
        name = f"{context.operation_id}-{invocation_id}.json"
        target = data_home() / "shutdown-checkpoints" / name
        if target.exists():
            raise ValueError("worker checkpoint bundle already exists")
        atomic_json(target, value)
        target.chmod(0o400)
        raw = _private_bytes(target, immutable=True)
        _authority(context)
        return {"schema_version": 1, "bundle_name": name, "bundle_digest": _digest(raw)}


def _process(pid: int) -> dict:
    from .capture import _parse_proc_stat
    root = Path("/proc") / str(pid)
    if root.stat().st_uid != os.getuid():
        raise ValueError("native process belongs to another user")
    _ppid, ticks = _parse_proc_stat((root / "stat").read_text())
    groups = [line.split(":", 2)[2] for line in (root / "cgroup").read_text().splitlines() if line.startswith("0::")]
    if len(groups) != 1:
        raise ValueError("native process cgroup cannot be proved")
    if _parse_proc_stat((root / "stat").read_text())[1] != ticks:
        raise ValueError("native process changed during checkpoint proof")
    return {"start_ticks": ticks, "control_group": groups[0]}


def _native(shell: dict) -> dict[str, dict]:
    if shell.get("available") is not True:
        raise ValueError("native window state unavailable for shutdown retry")
    result = {}
    for window in shell.get("windows", []):
        if type(window.get("id")) is not int or type(window.get("pid")) is not int or window["pid"] <= 0:
            raise ValueError("native window lacks stable process identity")
        # A HUD stage actor has no MetaWindow. Do not exempt arbitrary titles.
        key = str(window["id"])
        if key in result:
            raise ValueError("duplicate native window identity")
        result[key] = {field: window.get(field) for field in ("id", "pid", "app_id", "app_ids", "wm_class", "wm_instance", "workspace", "monitor", "monitor_identity", "monitor_geometry", "work_area", "geometry", "geometry_relative", "state")}
        result[key].update(_process(window["pid"]))
    return result


def _providers(shell: dict) -> dict:
    """Observe companion state without Identify/title leases or placement."""
    from . import browser, file_manager, vscode
    result = {}
    if file_manager.matching_windows(shell):
        state = file_manager._bridge("GetState")
        if not isinstance(state, dict) or type(state.get("pid")) is not int:
            raise ValueError("Nemo content unavailable for retry protection")
        result["nemo"] = {"pid": state["pid"], "windows": sorted([
            {key: window.get(key) for key in ("id", "locations", "active_tab", "complete")}
            for window in state.get("windows", [])], key=lambda item: item["id"])}
        if len(result["nemo"]["windows"]) != len(file_manager.matching_windows(shell)):
            raise ValueError("Nemo native and companion window counts differ")
        if any(item.get("complete") is not True for item in result["nemo"]["windows"]):
            raise ValueError("Nemo is still loading; retry after it settles")
    if vscode.matching_windows(shell):
        states = vscode._states(time.monotonic() + 2)
        result["vscode"] = sorted([{"pid": item["pid"], "instance": item["instance"], "project": vscode._project(item)} for item in states], key=lambda item: item["instance"])
        if not result["vscode"]:
            raise ValueError("VS Code content unavailable for retry protection")
    chrome = browser._shell_browser_windows(shell, "google-chrome")
    if chrome:
        profiles = [browser._request_path(path, "capture", timeout=2) for path in browser._host_paths()]
        result["chrome"] = sorted([{"profile": item.get("profile"), "windows": sorted([
            {"id": window.get("id"), "runtime_window_id": window.get("runtime_window_id"), "tabs": [
                {key: tab.get(key) for key in ("url", "pinned", "group", "active")}
                for tab in window.get("tabs", [])], "groups": sorted([
                    {key: group.get(key) for key in ("id", "title", "color", "collapsed")}
                    for group in window.get("groups", [])], key=lambda group: group["id"])}
            for window in item.get("windows", [])], key=lambda window: window["id"])} for item in profiles], key=lambda item: str(item["profile"]))
        if sum(len(item["windows"]) for item in result["chrome"]) != len(chrome):
            raise ValueError("Chrome content unavailable for retry protection")
    return result


def arm_retry_protection(completion: dict) -> None:
    context = operations.OperationContext.from_dict(completion.get("operation_context"))
    with _locks():
        document = _authority(context)
        if document.get("operation_state") != "authorized" or document.get("commit_authorized") is not True:
            raise ValueError("checkpoint drain has no current authorization")
        descriptor = completion.get("checkpoint_bundle")
        value = _bundle(descriptor)
        if value["operation_context"] != context.to_dict() or value["invocation_id"] != completion.get("invocation_id"):
            raise ValueError("checkpoint bundle belongs to another worker")
        _unchanged(value)
        shell = capture_shell(timeout=2)
        manager = Manager(time.monotonic() + 6, context.boot_id)
        units = [manager.snapshot(unit) for unit in manager.candidates()]
        native = _native(shell)
        # Companion observations were obtained by the worker. Never silently
        # adopt new windows or assume an arbitrary profile caused a difference.
        if native != value["native"]:
            raise ValueError("native windows changed after verified checkpoint; shutdown not drained")
        window_units = {}
        from .graphical_stop import migrated_owner
        for proof in units:
            try:
                migrated = migrated_owner(proof["control_group"])
            except FileNotFoundError:
                migrated = None
            for key, window in native.items():
                group = window["control_group"]
                if (group == proof["control_group"] or group.startswith(proof["control_group"] + "/")
                        or migrated is not None and migrated.pid == window["pid"] and migrated.started == window["start_ticks"]):
                    if key in window_units:
                        raise ValueError("native window has conflicting drain ownership")
                    window_units[key] = proof["unit"]
        protection = {"schema_version": 1, "operation_context": context.to_dict(),
                      "completion": completion, "checkpoint_bundle": descriptor,
                      "native": native, "providers": value["providers"], "units": units, "window_units": window_units}
        if _live_sessions() != value["sessions"]:
            raise ValueError("tmux changed after verified checkpoint")
        _authority(context)
        _unchanged(value)
        atomic_json(_pointer(), protection)


def _legacy_issued() -> bool:
    try:
        acknowledgement = private_json(runtime_root() / "shutdown-drain-manual-baseline.json")
        generation = (runtime_root() / "login-generation").read_text().strip()
        acknowledged = (acknowledgement.get("records", {}) if acknowledgement.get("boot_id") == operations.boot_id()
                        and acknowledgement.get("login_generation") == generation else {})
    except (OSError, ValueError):
        acknowledged = {}
    for path in _ledgers():
        try:
            value = private_json(path)
            owner = operations.OperationContext.from_dict(value.get("operation_context"))
            validate_receipt(value, owner)
        except (OSError, ValueError, TypeError):
            return True
        if acknowledged.get(path.name) == _digest(_private_bytes(path)):
            continue
        if owner.boot_id == operations.boot_id() and any(state != "planned" for state in value["requests"].values()):
            return True
    return False


def protected() -> bool:
    return _pointer().exists() or _pointer().is_symlink() or _legacy_issued()


def _protected_record() -> tuple[dict, dict, operations.OperationContext]:
    if not _pointer().exists():
        raise ValueError("prior graphical drain has no sealed checkpoint; explicit recovery is required")
    pointer = private_json(_pointer())
    if (not isinstance(pointer.get("completion"), dict) or not isinstance(pointer.get("native"), dict)
            or not isinstance(pointer.get("providers"), dict) or not isinstance(pointer.get("units"), list)
            or not isinstance(pointer.get("window_units"), dict)
            or pointer["completion"].get("checkpoint_bundle") != pointer.get("checkpoint_bundle")):
        raise ValueError("shutdown retry protection is malformed")
    owner = operations.OperationContext.from_dict(pointer.get("operation_context"))
    generation = (runtime_root() / "login-generation").read_text().strip()
    if (pointer.get("schema_version") != 1 or owner.mode != "shutdown"
            or owner.boot_id != operations.boot_id() or owner.login_generation != generation):
        raise ValueError("shutdown retry protection belongs to another boot or login")
    value = _bundle(pointer.get("checkpoint_bundle"))
    if value["operation_context"] != owner.to_dict() or pointer.get("completion", {}).get("invocation_id") != value["invocation_id"]:
        raise ValueError("shutdown retry protection worker identity differs")
    return pointer, value, owner


def _settled(pointer: dict, owner: operations.OperationContext) -> dict:
    ledger = private_json(runtime_root() / f"shutdown-graphical-drain-{owner.operation_id}.json")
    validate_receipt(ledger, owner)
    if ledger.get("settled") is not True or ledger.get("status") == "running" or any(state == "issuing" for state in ledger["requests"].values()):
        raise ValueError("previous graphical drain is still settling; wait before retrying")
    expected = {item["unit"]: item for item in pointer["units"]}
    if all(state == "planned" for state in ledger["requests"].values()):
        return ledger  # Exact helper receipt proves no StopUnit request issued.
    if {item["unit"]: item for item in ledger["units"]} != expected:
        raise ValueError("graphical drain inventory differs from armed checkpoint")
    return ledger


def _desktop_unchanged(pointer: dict, owner: operations.OperationContext, ledger: dict) -> None:
    manager = Manager(time.monotonic() + 8, owner.boot_id)
    issued = [item for item in ledger["units"] if ledger["requests"][item["unit"]] != "planned"]
    settled, complete, errors = reconcile(manager, issued, ledger["requests"])
    if not settled or not complete or errors:
        raise ValueError("original application stops are not verified: " + "; ".join(errors))
    expected = {item["unit"]: item for item in pointer["units"]}
    for unit in manager.candidates():
        properties = manager.inspect(unit)
        proof = expected.get(unit)
        if (proof is None or properties.get("InvocationID") != proof["invocation_id"]
                or properties.get("Job") or properties.get("ActiveState") in {"activating", "deactivating"}):
            raise ValueError("new application invocation appeared after graphical drain")
    shell = capture_shell(timeout=2)
    current = _native(shell)
    original = pointer["native"]
    if any(key not in original or original[key] != window for key, window in current.items()):
        raise ValueError("surviving desktop windows changed; save current state explicitly")
    for key in original.keys() - current.keys():
        if not any(pointer.get("window_units", {}).get(key) == item["unit"] for item in issued):
            raise ValueError("an unowned original window disappeared; explicit recovery is required")
    providers = _providers(shell)
    for name, record in providers.items():
        if name == "vscode" and any(item["project"].get("dirty_count", 0) for item in record):
            raise ValueError("surviving dirty VS Code editors require an explicit manual save")
        if record != pointer["providers"].get(name):
            raise ValueError(f"surviving {name} content changed; save current state explicitly")


@dataclass(frozen=True)
class CheckpointReuse:
    """A verified inherited checkpoint, including its original fallback status."""
    degraded: bool


def reuse_if_protected(context: operations.OperationContext) -> CheckpointReuse | None:
    if not protected():
        return None
    with _locks():
        _authority(context)
        pointer, value, owner = _protected_record()
        if owner.operation_id == context.operation_id:
            raise ValueError("cannot recapture a shutdown operation after its drain was armed")
        ledger = _settled(pointer, owner)
        if all(state == "planned" for state in ledger["requests"].values()) and value.get("inherited") is None:
            _pointer().unlink()
            return None  # No stop was issued: the new operation takes a fresh save.
        _unchanged(value)
        if _live_sessions() != value["sessions"]:
            raise ValueError("tmux sessions changed after graphical drain; save current state explicitly")
        _desktop_unchanged(pointer, owner, ledger)
        _authority(context)
        _unchanged(value)
        return CheckpointReuse(degraded=bool(value.get("degraded")))


def _current_shutdown_status() -> tuple[dict, operations.OperationContext, str] | None:
    """Historical telemetry cannot block a save or prove current settlement."""
    try:
        record = private_json(operation_path())
    except FileNotFoundError:
        try:
            record = private_json(status_path())
        except FileNotFoundError:
            return None
        if record.get("mode") != "shutdown":
            return None
    owner = operations.OperationContext.from_dict(record.get("operation_context"))
    if owner.mode != "shutdown" or owner.boot_id != operations.boot_id():
        return None
    try:
        generation = (runtime_root() / "login-generation").read_text().strip()
    except FileNotFoundError as error:
        raise ValueError("manual save has no current login proof for shutdown settlement") from error
    if not generation:
        raise ValueError("manual save has no current login proof for shutdown settlement")
    if owner.login_generation != generation:
        return None
    status = private_json(status_path())
    if not owner.matches(status):
        raise ValueError("manual save has no matching current shutdown settlement")
    return status, owner, generation


def check_manual_save_allowed() -> None:
    """Called under the caller's state lock, before any capture or write."""
    current = _current_shutdown_status()
    is_protected = protected()
    if current is None:
        if not is_protected:
            return
        status = private_json(status_path())
        status_owner = operations.OperationContext.from_dict(status.get("operation_context"))
        generation = (runtime_root() / "login-generation").read_text().strip()
    else:
        status, status_owner, generation = current
    if (not status_owner.matches(status) or status_owner.boot_id != operations.boot_id()
            or status_owner.login_generation != generation):
        raise ValueError("manual save status belongs to another boot or login")
    if (status.get("operation_state") not in {"cancelled", "failed"}
            or status.get("commit_authorized") is True or status.get("recovery_pending") is True):
        raise ValueError("shutdown is still active; wait for cancellation/recovery before saving")
    if current is not None:
        from .shutdown_profiles import transaction_path
        try:
            transaction = private_json(transaction_path())
        except FileNotFoundError:
            transaction = None
        if (transaction is not None and transaction.get("operation_id") == status_owner.operation_id
                and transaction.get("session_id") == generation):
            raise ValueError("shutdown profile recovery is still armed; wait for rollback before saving")
    if not is_protected:
        return
    if _pointer().exists():
        pointer, _value, owner = _protected_record()
        _prove_settlement(_settled(pointer, owner), owner)
        return
    # Explicit saving chooses the actual desktop as a new baseline. It does
    # not need an old sealed bundle, but cannot race an unjoined legacy drain.
    for path in _ledgers():
        ledger = private_json(path)
        owner = operations.OperationContext.from_dict(ledger.get("operation_context"))
        validate_receipt(ledger, owner)
        if owner.boot_id != operations.boot_id() or owner.login_generation != generation:
            raise ValueError("legacy graphical drain belongs to another boot or login")
        _prove_settlement(ledger, owner)


def _prove_settlement(ledger: dict, owner: operations.OperationContext) -> None:
    if ledger.get("settled") is not True or ledger.get("status") == "running" or any(state == "issuing" for state in ledger["requests"].values()):
        raise ValueError("previous graphical drain is still settling; wait before saving")
    manager = Manager(time.monotonic() + 6, owner.boot_id)
    for proof in ledger["units"]:
        properties = manager.inspect(proof["unit"])
        if properties.get("Job") or properties.get("ActiveState") in {"activating", "deactivating"}:
            raise ValueError("previous application stop job is still active")
    issued = [item for item in ledger["units"] if ledger["requests"][item["unit"]] != "planned"]
    settled, _complete, _errors = reconcile(manager, issued, ledger["requests"])
    if not settled:
        raise ValueError("previous application stops are still settling")


def validate_drain_plan(context: operations.OperationContext, proofs: list[dict]) -> None:
    """Exact-plan gate in the helper, before its first StopUnit request."""
    # The helper's per-operation ledger lock is distinct from the save locks.
    with _locks():
        document = _authority(context)
        if document.get("operation_state") != "authorized" or document.get("commit_authorized") is not True:
            raise ValueError("graphical drain plan is no longer authorized")
        pointer, value, owner = _protected_record()
        if owner != context or {item["unit"]: item for item in proofs} != {item["unit"]: item for item in pointer["units"]}:
            raise ValueError("graphical drain plan changed after verified checkpoint")
        _unchanged(value)
        shell = capture_shell(timeout=2)
        if _native(shell) != pointer["native"] or _providers(shell) != pointer["providers"]:
            raise ValueError("desktop content changed before application stops; checkpoint retained")
        if _live_sessions() != value["sessions"]:
            raise ValueError("tmux changed before application stops; checkpoint retained")
        _authority(context)


def clear_after_manual_save() -> None:
    """Caller holds state_lock across precheck, capture, save and this clear."""
    if not protected():
        return
    # Old issued ledgers remain useful diagnostics, but no longer block a new
    # explicit baseline. Bind acknowledgement to their exact operation IDs.
    acknowledged = {}
    for path in _ledgers():
        value = private_json(path)
        owner = operations.OperationContext.from_dict(value.get("operation_context"))
        validate_receipt(value, owner)
        if owner.boot_id == operations.boot_id() and value.get("settled") is True:
            acknowledged[path.name] = _digest(_private_bytes(path))
    atomic_json(runtime_root() / "shutdown-drain-manual-baseline.json", {"boot_id": operations.boot_id(),
                "login_generation": (runtime_root() / "login-generation").read_text().strip(), "records": acknowledged})
    _pointer().unlink(missing_ok=True)


def restore_protected_tmux_candidate(path: Path) -> Path:
    """Repair only the plugin's exact candidate and protected original target."""
    with _locks(runtime=False):
        _pointer_value, value, _owner = _protected_record()
        target = Path(value["tmux"]["path"])
        candidate = Path(path).absolute()
        if candidate.parent.resolve() != target.parent or not _FILE.fullmatch(candidate.name) or candidate.is_symlink():
            raise ValueError("tmux candidate is outside the protected checkpoint directory")
        # Validate paths/modes even when their contents were just overwritten.
        _private_bytes(candidate, private=False)
        _private_bytes(target, private=False)
        if _tmux_path() != target:
            raise ValueError("protected tmux last link changed")
        raw = _record_bytes(value["tmux"])
        for item in {candidate, target}:
            descriptor = os.open(item, os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        return target
