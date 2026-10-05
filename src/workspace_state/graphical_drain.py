"""Drain proven wsctl graphical units before GNOME tears down its compositor.

Run only after the coordinator commits the final countdown. Exit 0 means a
successful, settled receipt; 1 means settled failure; 75 means ownership must
be retained while outstanding stop jobs are reconciled. Re-entry never issues
another stop. Killing a systemctl client does not cancel its systemd job.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import sys
import threading
import time

if __package__ in {None, ""}:
    # The coordinator executes this exact file from its sealed package with -I.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from workspace_state import operations
from workspace_state.login_status import runtime_root, status_path
from workspace_state.util import atomic_json


UNIT = re.compile(r"wsctl-app-[A-Za-z0-9_][A-Za-z0-9_.-]{0,39}-[0-9a-f]{8}\.service")
INVOCATION = re.compile(r"[0-9a-f]{32}")
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Transient", "ControlGroup",
              "InvocationID", "PartOf", "FragmentPath", "Result", "Job")
MAX_UNITS = 64
MAX_TIMEOUT = 25.0
UNSETTLED = 75


def private_json(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor) as stream:
        metadata = os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077 or metadata.st_nlink != 1):
            raise ValueError(f"unsafe graphical drain record: {path}")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("graphical drain record is not an object")
    return value


def authorized(context: operations.OperationContext, withdrawn: threading.Event) -> None:
    context.check()
    document = private_json(status_path())
    if (withdrawn.is_set() or not context.matches(document)
            or document.get("operation_state") != "authorized"
            or document.get("commit_authorized") is not True
            or document.get("cancelled") is True):
        raise ValueError("graphical drain authorization was withdrawn or replaced")


def sealed_helper(value: str, releases: Path) -> str:
    path = Path(value)
    try:
        relative = path.relative_to(releases)
    except ValueError as error:
        raise ValueError("graphical stop helper is outside sealed releases") from error
    if (not path.is_absolute() or path.resolve() != path or len(relative.parts) != 6
            or not re.fullmatch(r"r-[0-9a-f]{24}", relative.parts[0])
            or relative.parts[1:] != ("components", "workspace-state", "src", "workspace_state", "graphical_stop.py")):
        raise ValueError("graphical stop helper is not an exact immutable release path")
    for item in (path, *path.parents[:5]):
        metadata = item.stat()
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o222:
            raise ValueError("graphical stop helper release is writable or foreign")
    if not path.is_file():
        raise ValueError("graphical stop helper is not a file")
    return str(path)


def prove_unit(unit: str, properties: dict, commands: object, releases: Path) -> dict:
    group = f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{unit}"
    fragment = f"/run/user/{os.getuid()}/systemd/transient/{unit}"
    if (not UNIT.fullmatch(unit) or properties.get("Id") != unit
            or properties.get("LoadState") != "loaded" or properties.get("Transient") != "yes"
            or properties.get("ControlGroup") != group or properties.get("FragmentPath") != fragment
            or "graphical-session.target" not in properties.get("PartOf", "").split()
            or not INVOCATION.fullmatch(properties.get("InvocationID", ""))):
        raise ValueError(f"{unit}: ownership is not the expected transient user graphical unit")
    if not isinstance(commands, list) or len(commands) != 1:
        raise ValueError(f"{unit}: expected exactly one graphical stop command")
    command = commands[0]
    if (not isinstance(command, list) or len(command) != 10 or command[0] != "/usr/bin/python3"
            or command[2] is not False or not isinstance(command[1], list)
            or len(command[1]) != 4 or command[1][:2] != ["/usr/bin/python3", "-I"]
            or command[1][3] != unit):
        raise ValueError(f"{unit}: graphical stop command or exact unit argument differs")
    helper = sealed_helper(command[1][2], releases)
    return {"unit": unit, "invocation_id": properties["InvocationID"],
            "control_group": group, "helper": helper}


class Manager:
    def __init__(self, deadline: float, boot: str):
        self.deadline = deadline
        self.boot = boot.replace("-", "")
        self.releases = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "workspace-state/desktop-releases"

    def run(self, args: list[str], *, stop: bool = False) -> subprocess.CompletedProcess:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("graphical drain deadline expired")
        return subprocess.run(args, text=True, capture_output=True,
                              timeout=remaining if stop else min(3.0, remaining), check=False)

    def candidates(self) -> list[str]:
        result = self.run(["/usr/bin/systemctl", "--user", "list-units", "--all", "--type=service",
                           "--output=json", "--no-pager"])
        if result.returncode:
            raise RuntimeError("could not enumerate user graphical units")
        records = json.loads(result.stdout)
        if not isinstance(records, list):
            raise ValueError("user unit inventory is not a list")
        units = sorted({record["unit"] for record in records
                        if isinstance(record, dict) and isinstance(record.get("unit"), str)
                        and UNIT.fullmatch(record["unit"]) and record.get("active") != "inactive"})
        if len(units) > MAX_UNITS:
            raise ValueError("graphical unit inventory exceeds the bounded drain limit")
        return units

    def inspect(self, unit: str) -> dict:
        result = self.run(["/usr/bin/systemctl", "--user", "show", unit, "--no-pager",
                           *(f"--property={name}" for name in PROPERTIES)])
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode and values.get("LoadState") != "not-found":
            raise RuntimeError(f"{unit}: could not inspect user unit")
        return values

    def snapshot(self, unit: str) -> dict:
        properties = self.inspect(unit)
        escaped = "".join(char if char.isascii() and char.isalnum() else f"_{ord(char):02x}" for char in unit)
        result = self.run(["/usr/bin/busctl", "--user", "--json=short", "get-property",
                           "org.freedesktop.systemd1", f"/org/freedesktop/systemd1/unit/{escaped}",
                           "org.freedesktop.systemd1.Service", "ExecStop"])
        if result.returncode:
            raise RuntimeError(f"{unit}: could not inspect structured stop command")
        value = json.loads(result.stdout)
        if value.get("type") != "a(sasbttttuii)":
            raise ValueError(f"{unit}: unexpected stop command signature")
        proof = prove_unit(unit, properties, value.get("data"), self.releases)
        if properties.get("Job") or properties.get("ActiveState") not in {"active", "reloading"}:
            raise ValueError(f"{unit}: unit already has a job or is not active")
        if properties.get("Result") != "success":
            raise ValueError(f"{unit}: unit already has a failure")
        return proof

    def stop(self, proof: dict) -> None:
        # Only exact proven names are ever sent to stop; no pattern or shell.
        result = self.run(["/usr/bin/systemctl", "--user", "--no-pager", "--job-mode=fail",
                           "stop", proof["unit"]], stop=True)
        if result.returncode:
            raise RuntimeError(f"{proof['unit']}: systemctl stop failed ({result.returncode})")

    def empty(self, proof: dict) -> bool:
        path = Path("/sys/fs/cgroup") / proof["control_group"].lstrip("/") / "cgroup.events"
        try:
            values = dict(line.split() for line in path.read_text().splitlines())
        except FileNotFoundError:
            return True
        return values.get("populated") == "0"

    def journal(self, proof: dict) -> list[dict]:
        result = self.run(["/usr/bin/journalctl", "--user", "--boot=" + self.boot, "--no-pager",
                           "--output=json", "USER_INVOCATION_ID=" + proof["invocation_id"]])
        if result.returncode:
            raise RuntimeError(f"{proof['unit']}: stop result journal unavailable")
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def manager_record(record: dict, proof: dict, boot: str) -> bool:
    return (record.get("_BOOT_ID") == boot.replace("-", "") and record.get("_UID") == str(os.getuid())
            and record.get("_COMM") == "systemd" and record.get("_SYSTEMD_USER_UNIT") == "init.scope"
            and record.get("USER_UNIT") == proof["unit"]
            and record.get("USER_INVOCATION_ID") == proof["invocation_id"])


def journal_result(records: list[dict], proof: dict, boot: str) -> tuple[bool, list[str]]:
    complete = False
    errors = []
    for record in records:
        if not manager_record(record, proof, boot):
            continue
        if (record.get("UNIT_RESULT") not in {None, "success"}
                or (record.get("JOB_TYPE") == "stop" and record.get("JOB_RESULT") not in {None, "done"})
                or (record.get("COMMAND") == "ExecStop" and record.get("EXIT_STATUS") not in {None, "0"})
                or (record.get("EXIT_CODE") in {"killed", "dumped"}
                    and record.get("EXIT_STATUS") in {"9", "KILL", "SIGKILL"})):
            errors.append(f"{proof['unit']}: {record.get('MESSAGE') or 'failed stop journal record'}")
        if ((record.get("JOB_TYPE"), record.get("JOB_RESULT")) == ("stop", "done")
                or record.get("MESSAGE_ID") == "7ad2d189f7e94e70a38c781354912448"):
            complete = True
    return complete, errors


def reconcile(manager: Manager, units: list[dict], requests: dict[str, str]) -> tuple[bool, bool, list[str]]:
    """Return settled, proved success, errors. Never create or cancel a job."""
    settled, complete, errors = True, True, []
    for proof in units:
        unresolved = requests[proof["unit"]] == "issuing"
        unresolved_error = (f"{proof['unit']}: stop issuance is unresolved; retain ownership and inspect "
                            "the original systemctl client or its invocation-bound stop-job journal")
        properties = manager.inspect(proof["unit"])
        if properties.get("Job") or properties.get("ActiveState") in {"activating", "deactivating"}:
            settled = False
            complete = False
            continue
        if properties.get("LoadState") != "not-found":
            if properties.get("InvocationID") not in {"", proof["invocation_id"]}:
                errors.append(f"{proof['unit']}: invocation replaced during drain")
                complete = False
                if unresolved:
                    settled = False
                    errors.append(unresolved_error)
                continue
            if properties.get("ActiveState") != "inactive" or properties.get("Result") != "success":
                errors.append(f"{proof['unit']}: unit did not stop successfully")
                complete = False
                if properties.get("ActiveState") not in {"inactive", "failed"}:
                    # planned/done/failed prove there is no live issuer left.
                    # An interrupted issuing marker does not: a surviving
                    # systemctl child might still enqueue its StopUnit call.
                    if unresolved:
                        settled = False
                        errors.append(unresolved_error)
                    continue
                # A failed unit can still have processes awaiting termination.
        if not manager.empty(proof):
            settled = False
            complete = False
            continue
        records = manager.journal(proof)
        done, failures = journal_result(records, proof, manager.boot)
        if unresolved and not any(
            manager_record(record, proof, manager.boot) and record.get("JOB_TYPE") == "stop"
            and record.get("JOB_RESULT") in {"done", "canceled", "timeout", "failed", "dependency", "skipped"}
            for record in records
        ):
            # Natural application exit alone does not settle an unknown IPC
            # issuer. Only a terminal result for its exact stop job does.
            settled = False
            done = False
            errors.append(unresolved_error)
        complete = complete and done and not failures
        errors.extend(failures)
    return settled, complete, errors


def validate_receipt(document: dict, context: operations.OperationContext, *, require_portal: bool = False) -> None:
    if (document.get("schema_version") != 1 or document.get("operation_context") != context.to_dict()
            or document.get("status") not in {"running", "succeeded", "failed"}
            or type(document.get("settled")) is not bool or not isinstance(document.get("units"), list)
            or not isinstance(document.get("errors"), list)):
        raise ValueError("graphical drain receipt belongs to another operation or is malformed")
    for proof in document["units"]:
        if (not isinstance(proof, dict) or not UNIT.fullmatch(proof.get("unit", ""))
                or not INVOCATION.fullmatch(proof.get("invocation_id", ""))
                or proof.get("control_group") != f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{proof['unit']}"):
            raise ValueError("graphical drain receipt has invalid unit ownership")
    requests = document.get("requests")
    if (not isinstance(requests, dict) or set(requests) != {proof["unit"] for proof in document["units"]}
            or any(value not in {"planned", "issuing", "done", "failed"} for value in requests.values())):
        raise ValueError("graphical drain receipt has invalid request states")
    if "portal" in document:
        from workspace_state.portal_drain import validate_receipt as validate_portal
        validate_portal(document["portal"], context)
    if require_portal and document.get("status") == "succeeded":
        portal = document.get("portal")
        if (document.get("portal_required") is not True or not isinstance(portal, dict)
                or portal.get("status") != "succeeded" or portal.get("settled") is not True
                or portal.get("errors") != [] or portal.get("settlement_only") is True):
            raise ValueError("graphical drain lacks successful native document portal settlement")


def drain(context: operations.OperationContext, receipt: Path, *, timeout: float = MAX_TIMEOUT,
          settle_only: bool = False, withdrawn: threading.Event | None = None,
          manager_factory=Manager, action_deadline: float | None = None,
          portal_runner=None) -> dict:
    """Caller holds the operation receipt lock for this entire function."""
    withdrawn = withdrawn or threading.Event()
    deadline = time.monotonic() + min(MAX_TIMEOUT, timeout)
    manager = manager_factory(deadline, context.boot_id)
    fresh = not receipt.exists()
    if fresh:
        document = {"schema_version": 1, "operation_context": context.to_dict(),
                    "status": "running", "settled": False,
                    "deadline": min(action_deadline if action_deadline is not None else deadline, context.deadline),
                    "units": [], "requests": {}, "errors": [], "started_at": time.time()}
    else:
        document = private_json(receipt)
        validate_receipt(document, context)
        if document["settled"] and document.get("portal_required") is True:
            return document
    try:
        if fresh and not settle_only:
            authorized(context, withdrawn)
            manager.deadline = min(deadline, document["deadline"])
            names = manager.candidates()
            with ThreadPoolExecutor(max_workers=min(16, max(1, len(names)))) as pool:
                document["units"] = list(pool.map(manager.snapshot, names))
            document["requests"] = {name: "planned" for name in names}
            # All ownership checks finish, and the exact intent is durable,
            # before the first stop request. Re-entry only reconciles this set.
            atomic_json(receipt, document)
            receipt_lock = threading.Lock()

            def request_state(proof, state):
                with receipt_lock:
                    document["requests"][proof["unit"]] = state
                    atomic_json(receipt, document)

            def stop(proof):
                try:
                    if manager.snapshot(proof["unit"]) != proof:
                        raise ValueError(f"{proof['unit']}: invocation or helper changed before stop")
                    authorized(context, withdrawn)
                    request_state(proof, "issuing")
                    authorized(context, withdrawn)
                    manager.stop(proof)
                    request_state(proof, "done")
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
                    request_state(proof, "failed")
                    return str(error)
                return None

            with ThreadPoolExecutor(max_workers=min(16, max(1, len(names)))) as pool:
                document["errors"].extend(error for error in pool.map(stop, document["units"]) if error)
        elif fresh:
            # Cancellation/recovery before the helper ever ran has no jobs.
            document.update(status="succeeded", settled=True,
                            finished_at=time.time(), finished_monotonic=time.monotonic())
            atomic_json(receipt, document)
            return document

        while time.monotonic() < deadline:
            document["settled"] = False
            settled, complete, errors = reconcile(manager, document["units"], document["requests"])
            document["settled"] = settled
            document["errors"] = list(dict.fromkeys([*document["errors"], *errors]))
            if complete or (settled and document["errors"]):
                document["settled"] = settled
                break
            # Journal delivery can follow unit collection briefly. Wait for
            # invocation-bound completion evidence within the same budget.
            time.sleep(min(.1, max(0, deadline - time.monotonic())))
        else:
            complete = False
            document["errors"].append("graphical drain completion was not verified within the wait budget")
        if complete and (not document["errors"] or document.get("portal_required") is True):
            # Native document exports belong to the desktop lifecycle too.
            # Stop their exact service after app stops, while this committed
            # operation still owns the inhibitor and before native handoff.
            from workspace_state.portal_drain import drain as drain_portal
            runner = portal_runner or drain_portal
            document["portal_required"] = True
            document["settled"] = False
            # Persist native settlement ownership before its first possible
            # IPC. A child error or interrupted receipt write cannot turn
            # earlier app completion into permission to release this owner.
            atomic_json(receipt, document)
            portal = runner(context, receipt.with_name(receipt.stem + "-portal.json"),
                            deadline=document["deadline"],
                            timeout=max(.001, deadline - time.monotonic()),
                            settle_only=settle_only, withdrawn=withdrawn)
            document["portal"] = portal
            document["settled"] = portal.get("settled") is True
            complete = portal.get("status") == "succeeded" and document["settled"]
            document["errors"].extend(portal.get("errors", []))
        if withdrawn.is_set():
            document["errors"].append("graphical drain process received cancellation")
        if document["status"] == "failed" or document["errors"] or not complete:
            document["status"] = "failed"
        else:
            document["status"] = "succeeded"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
        document["status"] = "failed"
        document["errors"].append(str(error))
        # No intent was published, so no stop could have been issued here.
        if fresh and not receipt.exists():
            document["settled"] = True
    if document["settled"]:
        document.update(finished_at=time.time(), finished_monotonic=time.monotonic())
    atomic_json(receipt, document)
    return document


def exit_status(document: dict) -> int:
    if document.get("settled") is not True:
        return UNSETTLED
    return 0 if document.get("status") == "succeeded" else 1


def check_units(timeout: float = MAX_TIMEOUT) -> dict:
    """Read-only preflight: unsupported mutable units fail before checkpointing."""
    manager = Manager(time.monotonic() + timeout, operations.boot_id())
    names = manager.candidates()
    with ThreadPoolExecutor(max_workers=min(16, max(1, len(names)))) as pool:
        units = list(pool.map(manager.snapshot, names))
    return {"status": "checked", "units": units}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--timeout", type=float, default=MAX_TIMEOUT)
    parser.add_argument("--deadline", type=float)
    parser.add_argument("--settle-only", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    descriptor = None
    try:
        if not (0 < args.timeout <= MAX_TIMEOUT):
            raise ValueError("graphical drain timeout must be within 0..25 seconds")
        if args.check:
            if args.receipt is not None or args.deadline is not None or args.settle_only:
                raise ValueError("--check cannot be combined with drain or settlement arguments")
            print(json.dumps(check_units(args.timeout), sort_keys=True), flush=True)
            return 0
        if args.receipt is None:
            raise ValueError("--receipt is required for graphical drain")
        if args.deadline is not None and (not math.isfinite(args.deadline) or args.deadline <= 0):
            raise ValueError("graphical drain deadline must be finite and positive")
        context = operations.OperationContext.from_dict(json.loads(os.environ[operations.CONTEXT_ENV]))
        if context.mode != "shutdown" or context.boot_id != operations.boot_id():
            raise ValueError("graphical drain requires this boot's shutdown context")
        if (not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", context.operation_id)
                or args.receipt.parent.resolve() != runtime_root().resolve()
                or args.receipt.name != f"shutdown-graphical-drain-{context.operation_id}.json"):
            raise ValueError("graphical drain receipt is not the exact operation's runtime path")
        descriptor = os.open(args.receipt.with_suffix(".lock"),
                             os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077 or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("graphical drain lock is not private")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return UNSETTLED
        withdrawn = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: withdrawn.set())
        signal.signal(signal.SIGINT, lambda *_: withdrawn.set())
        result = drain(context, args.receipt, timeout=args.timeout,
                       settle_only=args.settle_only, withdrawn=withdrawn, action_deadline=args.deadline)
        print(json.dumps(result, sort_keys=True), flush=True)
        return exit_status(result)
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"wsctl: graphical drain refused: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if descriptor is not None:
            os.close(descriptor)


if __name__ == "__main__":
    raise SystemExit(main())
