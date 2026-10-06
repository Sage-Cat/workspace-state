"""Passive reuse of an explicitly verified, unchanged native status view.

This is not a PID-to-thread cache. Any terminal output, input, resize, title,
foreground process or login change withdraws the evidence. No account text or
terminal input is retained or generated here.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from . import codex_status, operations
from .login_status import runtime_root


def _scope() -> dict[str, str] | None:
    try:
        generation = (runtime_root() / "login-generation").read_text().strip()
        boot = operations.boot_id()
        if not re.fullmatch(r"[0-9a-f]+", generation) or not codex_status._UUID.fullmatch(boot):
            return None
        return {"boot_id": boot, "login_generation": generation}
    except OSError:
        return None


def _fingerprint(snapshot: codex_status._Snapshot, home: Path) -> str:
    value = {"chain": {str(pid): asdict(identity) for pid, identity in snapshot.client.chain.items()},
             "argv": [argument.hex() for argument in snapshot.client.argv],
             "row": snapshot.client.row, "column": snapshot.client.column,
             "title": snapshot.title, "screen": snapshot.screen,
             "width": snapshot.width, "height": snapshot.height, "home": str(home)}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _directory(*, create: bool) -> Path:
    directory = runtime_root() / "native-status-evidence"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("native status evidence directory is not private")
    return directory


def _read(path: Path) -> dict:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096):
            raise ValueError("native status evidence record is not private")
        raw = stream.read(4097)
    if len(raw) > 4096:
        raise ValueError("native status evidence record is oversized")
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError("invalid native status evidence record")
    return record


def remember(proofs: list[codex_status.StatusProof], scope: dict[str, str] | None) -> None:
    """Retain only real, late-validated proofs in the same boot and login."""
    if not proofs:
        return
    if scope is None or _scope() != scope:
        raise RuntimeError("native status evidence login changed; checkpoint preserved")
    directory = _directory(create=True)
    records = []
    for proof in proofs:
        if not codex_status.revalidate(proof):
            raise RuntimeError("native status evidence changed; checkpoint preserved")
        view = codex_status._Snapshot(proof.client, proof.title, proof.screen, proof.width, proof.height)
        records.append((directory / f"{proof.pid}.json", {
            "schema_version": 1, "scope": scope, "pid": proof.pid,
            "pane_id": proof.pane_id, "session_id": proof.session_id,
            "fingerprint": _fingerprint(view, proof.codex_home),
            "status_block_digest": hashlib.sha256(proof.status_block.encode()).hexdigest(),
        }))
    if _scope() != scope:
        raise RuntimeError("native status evidence login changed; checkpoint preserved")
    for target, record in records:
        descriptor, temporary = tempfile.mkstemp(prefix=".status-", dir=directory)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(record, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def after_owned_height_restore(proof: codex_status.StatusProof, expected_height: int) -> codex_status.StatusProof:
    """Rebind a manual proof after restoring an explicitly authorized height.

    No resize or terminal input happens here. This narrow operator path accepts
    only an exact suffix crop of the verified transcript, including its complete
    final status report. Automatic capture never renews a changed viewport.
    """
    if (not isinstance(proof, codex_status.StatusProof) or isinstance(expected_height, bool)
            or not isinstance(expected_height, int) or not 0 < expected_height <= proof.height):
        raise codex_status.StatusRefused("invalid owned height restoration")
    deadline = codex_status._deadline(1.0)
    current = codex_status._snapshot(proof.pid, proof.pane_id, deadline)
    if (current.client.chain != proof.client.chain or current.client.argv != proof.client.argv
            or current.title != proof.title or current.width != proof.width or current.height != expected_height
            or codex_status._home(proof.pid) != proof.codex_home or not codex_status._safe_composer(current)):
        raise codex_status.StatusRefused("native status owner or view changed during height restoration")
    original = codex_status._Snapshot(proof.client, proof.title, proof.screen, proof.width, proof.height)
    old, visible = codex_status._log(original), codex_status._log(current)
    block = tuple(proof.status_block.splitlines())
    if (len(visible) < len(block) or len(visible) > len(old) or old[-len(visible):] != visible
            or visible[-len(block):] != block):
        raise codex_status.StatusRefused("native status transcript changed during height restoration")
    renewed = replace(proof, client=current.client, screen=current.screen, width=current.width, height=current.height)
    if not codex_status._revalidate(renewed, deadline):
        raise codex_status.StatusRefused("native status evidence changed after height restoration")
    return renewed


def passive_identity(pid: int, pane: str) -> dict[str, str | int] | None:
    """Read unchanged native evidence; never send input or resize a pane."""
    try:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or not re.fullmatch(r"%\d+", pane):
            return None
        record = _read(_directory(create=False) / f"{pid}.json")
        scope = _scope()
        identity = record.get("session_id")
        if (type(record.get("schema_version")) is not int or record.get("schema_version") != 1
                or scope is None or record.get("scope") != scope
                or record.get("pid") != pid or record.get("pane_id") != pane
                or not isinstance(identity, str) or not codex_status._UUID.fullmatch(identity)):
            return None
        deadline = codex_status._deadline(1.0)
        current = codex_status._snapshot(pid, pane, deadline)
        home = codex_status._home(pid)
        if (not codex_status._safe_composer(current)
                or _fingerprint(current, home) != record.get("fingerprint")):
            return None
        lines = codex_status._log(current)
        anchor = next((index for index in range(len(lines) - 1, -1, -1) if lines[index] == "/status"), None)
        block = codex_status._block(lines[anchor:]) if anchor is not None else None
        if (block is None or block[0] != identity
                or hashlib.sha256(block[1].encode()).hexdigest() != record.get("status_block_digest")):
            return None
        catalog = codex_status._catalog(home, deadline)
        if identity not in catalog or not codex_status._title_matches(current.title, catalog, identity):
            return None
        if codex_status._snapshot(pid, pane, deadline) != current or _scope() != scope:
            return None
        native = current.client.chain[pid]
        return {"pid": pid, "session_id": identity, "confidence": "native-status-view",
                "start_ticks": str(native.start_ticks), "tty": native.stdin}
    except (OSError, ValueError, TypeError, codex_status.StatusRefused):
        return None
