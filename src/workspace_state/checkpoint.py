"""Versioned checkpoint input and one desktop context per capture transaction."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path

from .desktop import capture_shell, desktop_topology_signature, workspace_names
from .util import atomic_json, CommandError

CURRENT_VERSION = 5
HISTORY_LIMIT = 8


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

    @classmethod
    def begin(cls) -> CaptureContext:
        shell = capture_shell()
        return cls(deepcopy(shell), tuple(workspace_names(shell=shell)),
                   desktop_topology_signature(shell), datetime.now(timezone.utc).isoformat())

    def verify(self, shell: dict | None = None) -> None:
        current = capture_shell() if shell is None else shell
        if desktop_topology_signature(current) != self.topology:
            raise CommandError("desktop topology changed during capture; previous checkpoint preserved")

    def evidence(self, providers: dict) -> dict:
        return {"schema_version": 1, "topology_signature": self.topology,
                "captured_at": self.captured_at, "provider_evidence": providers}


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


def verify_capture_context(snapshot: dict) -> None:
    context = snapshot.get("capture_context")
    if context is None:
        return  # Legacy recipe/manual data has no claimed live capture evidence.
    if not isinstance(context, dict) or context.get("schema_version") != 1:
        raise ValueError("unsupported capture context")
    if desktop_topology_signature(capture_shell()) != context.get("topology_signature"):
        raise CommandError("desktop topology changed before publication; previous checkpoint preserved")
