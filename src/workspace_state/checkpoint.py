"""Versioned checkpoint input and one desktop context per capture transaction."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from .desktop import capture_shell, desktop_topology_signature, workspace_names
from .util import atomic_json, CommandError, data_home

CURRENT_VERSION = 5
HISTORY_LIMIT = 8
HISTORY_MAX_BYTES = 16 * 1024 * 1024
_HISTORY_NAME = re.compile(r"\d{8}T\d{12}Z-([a-f0-9]{20})\.json")
CAPTURE_CATEGORIES = ("terminals", "browsers", "social-apps", "file-manager", "vscode")


def category_data(snapshot: dict, category: str):
    if category == "terminals":
        return {key: snapshot.get(key, [] if key != "desktop" else {})
                for key in ("sessions", "terminals", "desktop")}
    if category == "browsers":
        browsers = snapshot.get("browsers")
        value = (browsers.get("google_chrome", {}) if isinstance(browsers, dict)
                 else snapshot.get("chrome", {}))
        value = deepcopy(value) if isinstance(value, dict) else {}
        # Recovery observations are not the recipe whose adoption is authorized.
        value.pop("latest_observation", None)
        return value
    return snapshot.get(category.replace("-", "_"))


def category_digest(snapshot: dict, category: str) -> str:
    encoded = json.dumps(category_data(snapshot, category), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def category_provenance(snapshot: dict, category: str) -> dict:
    """Do not assign the latest terminal timestamp to legacy browser data."""
    digest = category_digest(snapshot, category)
    records = snapshot.get("category_provenance")
    record = records.get(category) if isinstance(records, dict) else None
    if (isinstance(record, dict) and record.get("schema_version") == 1
            and record.get("content_digest") == digest):
        return deepcopy(record)
    # Keep legacy evidence available for investigation without claiming it is
    # authoritative: older saves could merge a recipe into a newer context.
    return {"schema_version": 1, "content_digest": digest, "captured_at": None,
            "source": "legacy-or-unverified", "state": "unknown",
            "capture_context": deepcopy(snapshot.get("capture_context"))}


def category_adopted(snapshot: dict, category: str, owner: dict | None) -> bool:
    adoption = category_provenance(snapshot, category).get("adoption")
    return bool(owner and isinstance(adoption, dict)
                and adoption.get("schema_version") == 1
                and adoption.get("source") == "manual-save"
                and adoption.get("category") == category
                and all(adoption.get(key) == value for key, value in owner.items())
                and adoption.get("content_digest") == category_digest(snapshot, category))


def record_provenance(snapshot: dict, previous: dict | None, *, source: str,
                      owner: dict | None = None, retained: dict[str, str] | None = None,
                      problems: dict[str, list[str]] | None = None) -> None:
    """Publish recipe evidence and adoption atomically with the category data."""
    previous, retained, problems = previous or {}, retained or {}, problems or {}
    # A terminal-only publication drops the whole-desktop context. Preserve a
    # separately bound observation witness only from a previously valid capture;
    # category provenance describes the old recipe and cannot prove this input.
    from .browser_reconciliation import (WITNESS_KEY, ReconciliationRequired,
                                         browser_state, observation_witness)
    snapshot.pop(WITNESS_KEY, None)
    if source == "terminal-autosave" and browser_state(snapshot) == browser_state(previous):
        try:
            witness = observation_witness(previous)
        except (ReconciliationRequired, TypeError, ValueError, AttributeError):
            witness = None  # Preserve terminals, but never upgrade unknown evidence.
        if witness is not None:
            snapshot[WITNESS_KEY] = witness
    records = {}
    for category in CAPTURE_CATEGORIES:
        if category_data(snapshot, category) is None:
            continue
        if source == "terminal-autosave" and category != "terminals":
            records[category] = category_provenance(previous, category)
            continue
        if category in retained:
            record = category_provenance(previous, category)
            record.update(state="retained", retained_reason=retained[category],
                          retention_evidence={"attempted_at": snapshot.get("created_at"),
                                              "capture_errors": list(problems.get(category, []))})
        else:
            context = snapshot.get("capture_context") or {}
            evidence = context.get("provider_evidence", {}).get(category.replace("-", "_"))
            record = {
                "schema_version": 1, "content_digest": category_digest(snapshot, category),
                "captured_at": context.get("captured_at") or snapshot.get("created_at"),
                "source": source, "state": "failed" if problems.get(category) else "captured",
                "capture_context": ({
                    "topology_signature": context.get("topology_signature"),
                    "captured_at": context.get("captured_at"),
                    "provider_evidence": deepcopy(evidence),
                } if context else None),
            }
            if problems.get(category):
                record["capture_errors"] = list(problems[category])
            elif owner and source == "manual-save":
                record["adoption"] = {
                    "schema_version": 1, "source": "manual-save", "category": category,
                    **owner, "adopted_at": record["captured_at"],
                    "baseline_digest": record["content_digest"],
                    "content_digest": record["content_digest"],
                }
            elif category_adopted(previous, category, owner):
                record["adoption"] = category_provenance(previous, category)["adoption"]
                record["adoption"]["content_digest"] = record["content_digest"]
        records[category] = record
    snapshot["category_provenance"] = records
    warnings = list(retained.values())
    if source == "terminal-autosave":
        warnings.extend(previous.get("capture_errors", {}).get("preserved_categories", []))
    if warnings:
        errors = snapshot.setdefault("capture_errors", {})
        errors["preserved_categories"] = list(dict.fromkeys([
            *errors.get("preserved_categories", []), *warnings,
        ]))


def migrate(snapshot: dict) -> dict:
    if not isinstance(snapshot, dict):
        raise ValueError("workspace checkpoint must be an object")
    version = snapshot.get("version", 1)
    if type(version) is not int or version not in {1, 2, 3, 4, CURRENT_VERSION}:
        raise ValueError(f"unsupported workspace checkpoint version: {version!r}")
    value = deepcopy(snapshot)
    if version == 1:
        # Legacy Chrome and terminal placement remain readable by providers.
        # Retain original fields while making the top-level contract explicit.
        value["version"] = 2
        version = 2
    if version == 2:
        value["version"] = 3
        version = 3
    if version == 3:
        value["version"] = 4
        if "chrome" in value and "browsers" not in value:
            value["browsers"] = {"google_chrome": deepcopy(value["chrome"])}
        version = 4
    if version == 4:
        value["version"] = CURRENT_VERSION
        value.setdefault("capture_context", None)  # Legacy evidence is unknown.
    return value


@dataclass(frozen=True)
class CaptureContext:
    shell: dict
    names: tuple[str, ...]
    topology: str
    captured_at: str
    capture_id: str

    @classmethod
    def begin(cls) -> CaptureContext:
        captured_at = datetime.now(timezone.utc).isoformat()
        shell = capture_shell()
        return cls(deepcopy(shell), tuple(workspace_names(shell=shell)),
                   desktop_topology_signature(shell), captured_at, str(uuid4()))

    def verify(self, shell: dict | None = None) -> None:
        current = capture_shell() if shell is None else shell
        if desktop_topology_signature(current) != self.topology:
            raise CommandError("desktop topology changed during capture; previous checkpoint preserved")

    def evidence(self, providers: dict, *, created_at: str) -> dict:
        return {"schema_version": 2, "topology_signature": self.topology,
                "capture_id": self.capture_id, "captured_at": self.captured_at,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "snapshot_created_at": created_at, "provider_evidence": deepcopy(providers)}


def keep_generation(directory: Path, snapshot: dict) -> None:
    """Retain a bounded set of complete prior publications, never failed captures."""
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded).hexdigest()[:20]
    if directory.exists() and any(directory.glob(f"*-{digest}.json")):
        return
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{digest}.json"
    atomic_json(directory / name, snapshot)
    owned = [path for path in directory.glob("*.json")
             if re.fullmatch(r"\d{8}T\d{12}Z-[a-f0-9]{20}\.json", path.name)]
    for old in sorted(owned, key=lambda path: path.name)[:-HISTORY_LIMIT]:
        old.unlink()


def history_generations(directory: Path | None = None) -> list[tuple[str, dict]]:
    """Read bounded private generations, verifying the writer's exact filename hash.

    No canonical/history files are changed. Unknown files are never evidence;
    any unsafe or corrupt managed generation makes a recovery lookup uncertain.
    """
    directory = directory if directory is not None else data_home() / "recovery" / "history"
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
            raise ValueError("browser capture history directory is not private")
        names = []
        with os.scandir(descriptor) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise ValueError("browser capture history inventory exceeds its bound")
                if _HISTORY_NAME.fullmatch(entry.name):
                    names.append(entry.name)
                    if len(names) > HISTORY_LIMIT:
                        raise ValueError("browser capture history generations exceed their bound")
        result = []
        for name in sorted(names):
            generation = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                                 dir_fd=descriptor)
            try:
                metadata = os.fstat(generation)
                if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                        or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}
                        or metadata.st_nlink != 1 or not 0 < metadata.st_size <= HISTORY_MAX_BYTES):
                    raise ValueError("browser capture history generation is unsafe")
                with os.fdopen(generation, "rb", closefd=False) as stream:
                    raw = stream.read(HISTORY_MAX_BYTES + 1)
                after = os.fstat(generation)
                if (len(raw) != metadata.st_size or len(raw) > HISTORY_MAX_BYTES
                        or (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                        != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                    raise ValueError("browser capture history generation changed while read")
            finally:
                os.close(generation)
            def unique_object(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("browser capture history has duplicate object keys")
                    value[key] = item
                return value
            def finite_constant(value):
                raise ValueError("browser capture history contains a non-finite number")
            snapshot = json.loads(raw, object_pairs_hook=unique_object, parse_constant=finite_constant)
            if not isinstance(snapshot, dict):
                raise ValueError("browser capture history generation is not an object")
            encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
            expected = _HISTORY_NAME.fullmatch(name).group(1)
            if hashlib.sha256(encoded).hexdigest()[:20] != expected:
                raise ValueError("browser capture history generation hash does not match its name")
            result.append((name, snapshot))
        return result
    finally:
        os.close(descriptor)


def verify_capture_context(snapshot: dict) -> None:
    context = snapshot.get("capture_context")
    if context is None:
        return  # Legacy recipe/manual data has no claimed live capture evidence.
    if (not isinstance(context, dict) or type(context.get("schema_version")) is not int
            or context["schema_version"] not in {1, 2}):
        raise ValueError("unsupported capture context")
    if desktop_topology_signature(capture_shell()) != context.get("topology_signature"):
        raise CommandError("desktop topology changed before publication; previous checkpoint preserved")
