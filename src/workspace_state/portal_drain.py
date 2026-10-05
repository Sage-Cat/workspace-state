"""Stop Ubuntu's proven document portal before the enclosing user manager exits.

Only normal StopUnit is used. The committed shutdown's existing drain lock and
absolute deadline cover this ledger too. Re-entry reconciles an issued job; it
never issues a second stop. No FUSE mount is manipulated directly.
"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from . import operations
from .graphical_drain import (Manager as GraphicalManager, authorized, private_json,
                             reconcile)
from .util import atomic_json

UNIT = "xdg-document-portal.service"
BUS = "org.freedesktop.portal.Documents"
EXECUTABLE = Path("/usr/libexec/xdg-document-portal")
FRAGMENT = Path("/usr/lib/systemd/user/xdg-document-portal.service")
INVOCATION = re.compile(r"[0-9a-f]{32}")
PROPERTIES = ("Id", "LoadState", "ActiveState", "SubState", "Transient", "ControlGroup",
              "InvocationID", "PartOf", "FragmentPath", "Result", "Job", "MainPID", "BusName")


def group() -> str:
    return f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/session.slice/{UNIT}"


def process_identity(pid: int) -> dict:
    root = Path("/proc") / str(pid)
    expected = EXECUTABLE.stat()
    actual = (root / "exe").stat()
    status = dict(line.split(":", 1) for line in (root / "status").read_text().splitlines() if ":" in line)
    stat_parts = (root / "stat").read_text().rsplit(") ", 1)
    fields = stat_parts[-1].split()
    if len(stat_parts) != 2 or len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("document portal process start identity is malformed")
    if (EXECUTABLE.resolve() != EXECUTABLE or expected.st_uid != 0 or expected.st_mode & 0o022
            or (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino)
            or str((root / "exe").readlink()) != str(EXECUTABLE)
            or status.get("Uid", "").split() != [str(os.getuid())] * 4
            or (root / "cgroup").read_text().strip() != "0::" + group()
            or (root / "cmdline").read_bytes().rstrip(b"\0").split(b"\0") != [os.fsencode(EXECUTABLE)]
            or fields[0] in {"Z", "X"}):
        raise ValueError("document portal process is not the exact native user service")
    return {"pid": pid, "start_ticks": fields[19], "executable": str(EXECUTABLE),
            "executable_dev": actual.st_dev, "executable_ino": actual.st_ino, "uid": os.getuid()}


def mount_identity() -> dict | None:
    # Parse kernel metadata only: stat/readdir on a blocked FUSE mount can hang.
    target = f"/run/user/{os.getuid()}/doc"
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) > 6 and fields[4] == target:
            matches.append(fields)
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("document portal mount is stacked or ambiguous")
    fields = matches[0]
    tail = fields[fields.index("-") + 1:]
    if (len(tail) != 3 or tail[:2] != ["fuse.portal", "portal"]
            or f"user_id={os.getuid()}" not in tail[2].split(",")):
        raise ValueError("document portal path has a foreign mount")
    return {"mount_id": int(fields[0]), "device": fields[2], "path": target,
            "filesystem": "fuse.portal", "source": "portal", "uid": os.getuid()}


class Manager(GraphicalManager):
    def inspect(self, unit: str = UNIT) -> dict:
        if unit != UNIT:
            raise ValueError("document portal drain accepts only its fixed native unit")
        result = self.run(["/usr/bin/systemctl", "--user", "show", UNIT, "--no-pager",
                           *(f"--property={name}" for name in PROPERTIES)])
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode and values.get("LoadState") != "not-found":
            raise RuntimeError("could not inspect native document portal unit")
        return values

    def bus_call(self, method: str, name: str, signature: str, kind: str):
        result = self.run(["/usr/bin/busctl", "--user", "--json=short", "call",
                           "org.freedesktop.DBus", "/org/freedesktop/DBus",
                           "org.freedesktop.DBus", method, signature, name])
        if result.returncode:
            raise RuntimeError("could not prove document portal bus ownership")
        import json
        value = json.loads(result.stdout)
        data = value.get("data")
        if value.get("type") != kind or not isinstance(data, list) or len(data) != 1:
            raise ValueError("invalid document portal bus ownership response")
        return data[0]

    def bus_owner(self) -> tuple[str, int] | None:
        exists = self.bus_call("NameHasOwner", BUS, "s", "b")
        if type(exists) is not bool:
            raise ValueError("invalid document portal bus ownership flag")
        if not exists:
            return None
        owner = self.bus_call("GetNameOwner", BUS, "s", "s")
        if not isinstance(owner, str) or not re.fullmatch(r":\d+\.\d+", owner):
            raise ValueError("document portal does not have a unique bus owner")
        pid = self.bus_call("GetConnectionUnixProcessID", owner, "s", "u")
        if type(pid) is not int or pid <= 1:
            raise ValueError("invalid document portal bus process")
        return owner, pid

    def snapshot(self, unit: str = UNIT) -> dict | None:
        properties = self.inspect(unit)
        bus_owner = self.bus_owner()
        mount = mount_identity()
        if properties.get("LoadState") == "not-found" or properties.get("ActiveState") == "inactive":
            if (properties.get("Job") or bus_owner is not None or mount is not None
                    or not self.empty({"control_group": group()})
                    or properties.get("Result", "success") != "success"):
                raise ValueError("inactive document portal still has a job, mount, bus owner or failure")
            return None
        metadata = FRAGMENT.stat()
        if (properties.get("Id") != UNIT or properties.get("LoadState") != "loaded"
                or properties.get("Transient") != "no" or properties.get("ControlGroup") != group()
                or properties.get("FragmentPath") != str(FRAGMENT) or FRAGMENT.resolve() != FRAGMENT
                or metadata.st_uid != 0 or metadata.st_mode & 0o022
                or properties.get("BusName") != BUS
                or "graphical-session.target" not in properties.get("PartOf", "").split()
                or not INVOCATION.fullmatch(properties.get("InvocationID", ""))
                or properties.get("ActiveState") != "active" or properties.get("Job")
                or properties.get("Result") != "success" or mount is None):
            raise ValueError("document portal unit, invocation or mount ownership is unproven")
        pid = int(properties.get("MainPID", "0"))
        if bus_owner is None or bus_owner[1] != pid:
            raise ValueError("document portal bus owner differs from its service main PID")
        identity = process_identity(pid)
        # Recheck the bus after reading process metadata, so a replacement
        # cannot combine an old PID proof with a new name owner.
        if self.bus_owner() != bus_owner:
            raise ValueError("document portal bus ownership changed during inspection")
        return {"unit": UNIT, "invocation_id": properties["InvocationID"],
                "control_group": group(), "bus_name": BUS, "bus_owner": bus_owner[0],
                "process": identity, "mount": mount}

    def empty(self, proof: dict) -> bool:
        return super().empty(proof)


def validate_receipt(document: dict, context: operations.OperationContext) -> None:
    if (not isinstance(document, dict) or document.get("schema_version") != 1
            or document.get("operation_context") != context.to_dict()
            or not isinstance(document.get("status"), str)
            or document["status"] not in {"running", "succeeded", "failed"}
            or type(document.get("settled")) is not bool or not isinstance(document.get("errors"), list)
            or not isinstance(document.get("units"), list) or len(document["units"]) > 1):
        raise ValueError("document portal receipt belongs to another operation or is malformed")
    if (type(document.get("not_running")) is not bool or type(document.get("settlement_only")) is not bool
            or ("not_issued" in document and type(document["not_issued"]) is not bool)
            or (not document["units"] and not document["not_running"]
                and not document["settlement_only"] and document.get("not_issued") is not True)):
        raise ValueError("document portal receipt lacks an explicit no-issuance outcome")
    for proof in document["units"]:
        if not isinstance(proof, dict):
            raise ValueError("document portal receipt has malformed native ownership")
        process, mount = proof.get("process", {}), proof.get("mount", {})
        if (not isinstance(process, dict) or not isinstance(mount, dict)
                or proof.get("unit") != UNIT or proof.get("control_group") != group()
                or not isinstance(proof.get("invocation_id"), str)
                or not INVOCATION.fullmatch(proof["invocation_id"])
                or proof.get("bus_name") != BUS or not isinstance(proof.get("bus_owner"), str)
                or not re.fullmatch(r":\d+\.\d+", proof["bus_owner"])
                or process.get("executable") != str(EXECUTABLE) or process.get("uid") != os.getuid()
                or type(process.get("pid")) is not int or process["pid"] <= 1
                or not isinstance(process.get("start_ticks"), str)
                or not re.fullmatch(r"\d+", process["start_ticks"])
                or type(process.get("executable_dev")) is not int or type(process.get("executable_ino")) is not int
                or mount.get("path") != f"/run/user/{os.getuid()}/doc" or mount.get("uid") != os.getuid()
                or mount.get("filesystem") != "fuse.portal" or mount.get("source") != "portal"
                or type(mount.get("mount_id")) is not int):
            raise ValueError("document portal receipt has invalid native ownership")
    requests = document.get("requests")
    if (not isinstance(requests, dict) or set(requests) != {proof["unit"] for proof in document["units"]}
            or any(not isinstance(value, str) or value not in {"planned", "issuing", "done", "failed"}
                   for value in requests.values())):
        raise ValueError("document portal receipt has invalid request states")


def verify_stopped(document: dict, context: operations.OperationContext, *, manager_factory=Manager) -> None:
    """Fresh read-only check before handoff; historical success cannot hide reactivation."""
    validate_receipt(document, context)
    if (document.get("status") != "succeeded" or document.get("settled") is not True
            or document.get("errors") != [] or document.get("settlement_only") is True):
        raise ValueError("document portal has no successful shutdown settlement")
    authorized(context, threading.Event())
    manager = manager_factory(min(context.deadline, time.monotonic() + 3.0), context.boot_id)
    properties = manager.inspect(UNIT)
    if (properties.get("LoadState") not in {"loaded", "not-found"}
            or properties.get("ActiveState") != "inactive" or properties.get("Job")
            or properties.get("Result", "success") != "success"
            or manager.bus_owner() is not None or mount_identity() is not None
            or not manager.empty({"control_group": group()})):
        raise ValueError("document portal reactivated or did not remain stopped before handoff")
    if document["units"] and properties.get("InvocationID") not in {"", document["units"][0]["invocation_id"]}:
        raise ValueError("document portal invocation was replaced after its shutdown receipt")
    authorized(context, threading.Event())


def drain(context: operations.OperationContext, receipt: Path, *, deadline: float,
          timeout: float, settle_only: bool = False, withdrawn: threading.Event,
          manager_factory=Manager) -> dict:
    """Called under the parent graphical drain lock; never direct unmount."""
    if not math.isfinite(deadline) or timeout <= 0:
        raise ValueError("document portal drain requires a bounded deadline")
    wait_deadline = time.monotonic() + min(25.0, timeout)
    manager = manager_factory(wait_deadline, context.boot_id)
    fresh = not receipt.exists()
    if fresh:
        document = {"schema_version": 1, "operation_context": context.to_dict(),
                    "status": "running", "settled": False, "deadline": min(deadline, context.deadline),
                    "units": [], "requests": {}, "errors": [], "started_at": time.time(),
                    "not_running": False, "not_issued": True, "settlement_only": settle_only}
    else:
        document = private_json(receipt)
        validate_receipt(document, context)
        if document["settled"]:
            return document
    try:
        if fresh and not settle_only:
            manager.deadline = min(wait_deadline, document["deadline"])
            authorized(context, withdrawn)
            proof = manager.snapshot()
            document["units"] = [proof] if proof else []
            document["not_running"] = proof is None
            document["requests"] = {UNIT: "planned"} if proof else {}
            atomic_json(receipt, document)
            if proof is not None:
                if manager.snapshot() != proof:
                    raise ValueError("document portal identity changed before stop")
                authorized(context, withdrawn)
                if time.monotonic() >= document["deadline"]:
                    raise TimeoutError("document portal stop authorization expired")
                document["requests"][UNIT] = "issuing"
                document["not_issued"] = False
                atomic_json(receipt, document)
                try:
                    authorized(context, withdrawn)
                    if manager.snapshot() != proof:
                        raise ValueError("document portal identity changed at stop issuance")
                    manager.stop(proof)
                    document["requests"][UNIT] = "done"
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
                    document["requests"][UNIT] = "failed"
                    document["errors"].append(str(error))
                atomic_json(receipt, document)
        elif fresh:
            # Recovery before issuance has no native jobs to mutate or await.
            document.update(status="succeeded", settled=True)
        manager.deadline = wait_deadline  # Settlement may outlive work authorization.
        complete = False
        while not document["settled"] and time.monotonic() < wait_deadline:
            settled, complete, errors = reconcile(manager, document["units"], document["requests"])
            document["errors"] = list(dict.fromkeys([*document["errors"], *errors]))
            if settled and complete and document["units"] and mount_identity() is not None:
                complete = False
            if complete or (settled and document["errors"]):
                document["settled"] = settled
                break
            time.sleep(min(.1, max(0, wait_deadline - time.monotonic())))
        if not document["settled"]:
            document["errors"].append("document portal stop is pending or lacks native completion evidence")
        if withdrawn.is_set():
            document["errors"].append("document portal drain authorization was withdrawn")
        document["status"] = "failed" if document["errors"] or not document["settled"] else "succeeded"
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, TimeoutError) as error:
        document["status"] = "failed"
        document["errors"].append(str(error))
        if not document["units"] or all(value == "planned" for value in document["requests"].values()):
            document["settled"] = True
    if document["settled"]:
        document.update(finished_at=time.time(), finished_monotonic=time.monotonic())
    atomic_json(receipt, document)
    return document
