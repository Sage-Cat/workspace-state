"""Provider evidence and placement semantics, independent of app transports/HUD."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable

from .util import CommandError


class EvidenceState(str, Enum):
    UNKNOWN = 'unknown'
    WAITING = 'waiting'
    VERIFIED = 'verified'
    FAILED = 'failed'
    SKIPPED = 'skipped'


@dataclass(frozen=True)
class PhaseEvidence:
    state: EvidenceState = EvidenceState.UNKNOWN
    detail: str = ''
    retryable: bool = False
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {'state': self.state.value, 'detail': self.detail, 'retryable': self.retryable}
        if self.request_id:
            result['request_id'] = self.request_id
        return result


@dataclass(frozen=True)
class ProviderItemResult:
    provider: str
    item_id: str
    identity: PhaseEvidence = field(default_factory=PhaseEvidence)
    content: PhaseEvidence = field(default_factory=PhaseEvidence)
    placement: PhaseEvidence = field(default_factory=PhaseEvidence)
    attention: tuple[str, ...] = ()
    created: bool = False
    reused: bool = False

    @property
    def success(self) -> bool:
        return not self.attention and all(phase.state in {EvidenceState.VERIFIED, EvidenceState.SKIPPED}
                                         for phase in (self.identity, self.content, self.placement))

    @property
    def retryable(self) -> bool:
        return any(phase.retryable for phase in (self.identity, self.content, self.placement))

    def to_dict(self) -> dict[str, Any]:
        return {'provider': self.provider, 'item_id': self.item_id,
                'identity': self.identity.to_dict(), 'content': self.content.to_dict(),
                'placement': self.placement.to_dict(), 'attention': list(self.attention),
                'created': self.created, 'reused': self.reused,
                'success': self.success, 'retryable': self.retryable}


class ProviderCount(int):
    """Compatible with existing integer-returning providers, with item evidence."""
    def __new__(cls, value: int, results: Iterable[ProviderItemResult] = ()):
        instance = super().__new__(cls, value)
        instance.results = tuple(results)
        return instance


class ProviderRestoreError(CommandError):
    def __init__(self, message: str, results: Iterable[ProviderItemResult] = ()):
        super().__init__(message)
        self.results = tuple(results)


class PlacementPending(CommandError):
    """An accepted compositor request still needs observation on a later retry."""
    def __init__(self, message: str, request_id: str | None = None):
        super().__init__(message)
        self.request_id = request_id


def waiting_only(results) -> bool:
    """Pending work is distinct from failed identity/content or user attention."""
    results = tuple(results)
    return bool(results) and any(phase.state == EvidenceState.WAITING for result in results
                                for phase in (result.identity, result.content, result.placement)) and all(
        not result.attention and all(phase.state in {EvidenceState.VERIFIED, EvidenceState.SKIPPED, EvidenceState.WAITING}
                                     for phase in (result.identity, result.content, result.placement))
        for result in results)


def placement_pending(result: dict[str, Any]) -> bool:
    return result.get("status") in {"accepted", "deferred", "applied"} or bool(result.get("deferred"))


def placement_accepted(result: dict[str, Any]) -> bool:
    """Acceptance permits observation/retry, but never proves completion."""
    return result.get('placed') is True or result.get('status') in {'accepted', 'deferred', 'applied', 'verified'}


def placement_matches(window: dict[str, Any], target: dict[str, Any], *, tolerance: int = 1) -> bool:
    if any(window.get(key) != target.get(key) for key in ('workspace', 'monitor', 'state')):
        return False
    if target.get('state') in {'maximized', 'fullscreen'}:
        return True  # Mutter, not a saved rectangle, owns these dimensions.
    actual = (window.get('geometry_relative') if target.get('coordinate_space') == 'monitor'
              else window.get('geometry')) or {}
    expected = target.get('geometry') or {}
    if not expected:
        return True
    try:
        return all(abs(float(actual[key]) - float(value)) <= tolerance for key, value in expected.items())
    except (KeyError, TypeError, ValueError):
        return False
