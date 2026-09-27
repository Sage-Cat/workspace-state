"""Immutable publisher authority and explicit lifecycle transitions.

Deadlines use CLOCK_MONOTONIC seconds and are valid only in their recorded boot.
Recovery may outlive the work deadline: expiry revokes commit, never rollback.
The status file is the atomic operation record; existing profile journals retain
the detailed compensation data. No additional competing lifecycle daemon exists.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import json
import fcntl
import math
import os
from pathlib import Path
import time
from typing import Iterator
from uuid import uuid4

CONTEXT_ENV = "WSCTL_OPERATION_CONTEXT"
STARTUP_BUDGET = 25 * 60.0  # Includes cloud finalization, bounded separately.
SHUTDOWN_BUDGET = 2 * 60 * 60.0  # Match the existing managed profile service budget.
_publisher: ContextVar[OperationContext | None] = ContextVar("wsctl_operation", default=None)


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


@dataclass(frozen=True)
class OperationContext:
    boot_id: str
    login_generation: str
    operation_id: str
    mode: str
    attempt: int
    deadline: float

    @classmethod
    def create(cls, generation: str, mode: str, *, operation_id: str | None = None,
               attempt: int = 1, budget: float | None = None) -> OperationContext:
        if budget is None:
            budget = SHUTDOWN_BUDGET if mode == "shutdown" else STARTUP_BUDGET
        return cls.from_dict(dict(boot_id=boot_id(), login_generation=generation,
                                  operation_id=operation_id or uuid4().hex, mode=mode,
                                  attempt=attempt, deadline=time.monotonic() + budget))

    @classmethod
    def from_dict(cls, value: object) -> OperationContext:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("incomplete operation context")
        if any(not isinstance(value[k], str) or not value[k] for k in
               ("boot_id", "login_generation", "operation_id")):
            raise ValueError("invalid operation identity")
        if value["mode"] not in {"startup", "shutdown"}:
            raise ValueError("invalid operation mode")
        if type(value["attempt"]) is not int or value["attempt"] < 1:
            raise ValueError("invalid operation attempt")
        deadline = value["deadline"]
        if isinstance(deadline, bool) or not isinstance(deadline, (float, int)) or \
                not math.isfinite(deadline) or deadline <= 0:
            raise ValueError("invalid operation deadline")
        return cls(**value)

    def to_dict(self) -> dict:
        return asdict(self)

    def matches(self, document: dict) -> bool:
        return (document.get("operation_context") == self.to_dict()
                and document.get("session_id") == self.login_generation
                and document.get("mode") == self.mode
                and document.get("operation_id") == self.operation_id)

    def remaining(self, maximum: float | None = None) -> float:
        remaining = max(0.0, self.deadline - time.monotonic())
        return remaining if maximum is None else min(maximum, remaining)

    def check(self) -> None:
        if self.boot_id != boot_id() or self.remaining() <= 0:
            raise TimeoutError("operation expired or belongs to a previous boot")


def current() -> OperationContext | None:
    context = _publisher.get()
    if context is None and os.environ.get(CONTEXT_ENV):
        # Malformed inherited authority is an error, never a request to adopt
        # whichever newer operation happens to be present.
        context = OperationContext.from_dict(json.loads(os.environ[CONTEXT_ENV]))
        _publisher.set(context)
    return context


def bind(context: OperationContext | None) -> None:
    _publisher.set(context)


@contextmanager
def publisher(context: OperationContext) -> Iterator[None]:
    token = _publisher.set(context)
    try:
        yield
    finally:
        _publisher.reset(token)


def child_environment() -> dict[str, str]:
    env = dict(os.environ)
    context = current()
    if context is not None:
        env[CONTEXT_ENV] = json.dumps(context.to_dict(), separators=(",", ":"))
    return env


@contextmanager
def coordinator_lock(path: Path) -> Iterator[None]:
    """One coordinator per runtime directory, released even after a crash.

    Never unlink the lock inode: waiters must all contend on the same object.
    The descriptor is not inherited by restored applications or workers.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another workspace lifecycle coordinator is already running") from error
        yield
    finally:
        os.close(descriptor)


TRANSITIONS = {
    "running": {"preparing", "completed", "failed", "cancelling"},
    "preparing": {"prepared", "failed", "cancelling", "recovering"},
    "prepared": {"authorized", "cancelling", "recovering", "failed"},
    "authorized": {"completed", "cancelling", "recovering", "failed"},
    "cancelling": {"recovering", "cancelled", "recovery-failed"},
    "recovering": {"cancelled", "failed", "recovery-failed"},
    "recovery-failed": {"recovering", "cancelled", "failed"},
    "cancelled": set(), "completed": set(), "failed": {"recovering"},
}
RECOVERY_STATES = {"cancelling", "recovering", "recovery-failed"}


def transition(document: dict, target: str) -> None:
    previous = document.get("operation_state", "running")
    if target != previous and target not in TRANSITIONS.get(previous, set()):
        raise ValueError(f"illegal operation transition {previous} -> {target}")
    document["operation_state"] = target
    document["recovery_pending"] = target in RECOVERY_STATES
    document["commit_authorized"] = target == "authorized" and not document.get("cancelled", False)


def context_from_status(path: Path, mode: str, operation_id: str | None = None,
                        *, allow_expired: bool = False) -> OperationContext:
    document = json.loads(path.read_text())
    context = OperationContext.from_dict(document.get("operation_context"))
    if not context.matches(document) or context.mode != mode or \
            (operation_id is not None and context.operation_id != operation_id):
        raise ValueError("operation context does not belong to this worker")
    inherited = current()
    if inherited is not None and inherited != context:
        raise ValueError("worker belongs to a superseded operation")
    if not allow_expired:
        context.check()
    elif context.boot_id != boot_id():
        raise ValueError("recovery belongs to another boot")
    bind(context)
    return context


def receipt_matches(document: dict, receipt: dict, *, allow_expired: bool = False) -> bool:
    """Authorization never accepts a legacy receipt for a contextual operation."""
    try:
        context = OperationContext.from_dict(document.get("operation_context"))
        if not context.matches(document) or receipt.get("operation_context") != context.to_dict():
            return False
        if allow_expired:
            return context.boot_id == boot_id()
        context.check()
        return True
    except (OSError, TypeError, ValueError, TimeoutError):
        return False
