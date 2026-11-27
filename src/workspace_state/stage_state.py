"""The one place where a stage's state is derived from its item evidence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .provider_results import (EvidenceState, PhaseEvidence, ProviderItemResult, RECIPE_AGE_ITEM,
                               blocking_attention, displayed_state)

PHASES = ("identity", "content", "placement")


@dataclass(frozen=True)
class StageState:
    real: str  # what was restored; advisory notes never change it
    shown: str  # what the stage shows: an advisory note turns `ready` into `degraded`
    waiting_with_failures: bool = False


def stage_document(document: dict[str, Any] | None, category: str) -> dict[str, Any]:
    stages = document.get("stages") if isinstance(document, dict) else None
    return next((stage for stage in stages or ()
                 if isinstance(stage, dict) and stage.get("id") == category), {})


def stage_verified(stage: dict[str, Any]) -> bool:
    """Whether the stage's evidence shows every item restored (advisory notes aside).

    A stage that is `failed`, `waiting`, `running` or `pending` is not verified
    whatever its items say; `degraded` only adds an advisory note to what is shown.
    """
    if stage.get("state") not in {"ready", "skipped", "degraded"}:
        return False
    values = stage.get("provider_results")
    if isinstance(values, list) and values:
        return stage_state(values, stage.get("provider_error")).real == "ready"
    return stage.get("state") in {"ready", "skipped"} and not stage.get("provider_error")


def evidence_from_dict(value: dict[str, Any]) -> ProviderItemResult:
    """Decode persisted evidence strictly; corrupted records never prove success."""
    phases = []
    for name in PHASES:
        phase = value.get(name)
        if not isinstance(phase, dict):
            raise ValueError("missing provider phase")
        token = phase.get("request_id")
        if token is not None and (not isinstance(token, str) or not token or len(token) > 256):
            raise ValueError("invalid placement request identity")
        phases.append(PhaseEvidence(EvidenceState(phase["state"]), str(phase.get("detail", "")),
                                    phase.get("retryable") is True, token))
    attention = value.get("attention", [])
    if not isinstance(attention, list) or any(not isinstance(item, str) for item in attention):
        raise ValueError("invalid provider attention")
    return ProviderItemResult(str(value["provider"]), str(value["item_id"]), *phases,
                              tuple(attention), value.get("created") is True, value.get("reused") is True)


def evidence_state(values: list[dict[str, Any]]) -> str:
    """Compute aggregate state from phase evidence, never from accepted counts."""
    try:
        items = [evidence_from_dict(value) for value in values]
    except (KeyError, TypeError, ValueError):
        return "failed"
    if not items:
        return "skipped"
    if all(item.success for item in items):
        return "ready"
    if any(blocking_attention(item.attention) or any(phase.state in {EvidenceState.FAILED, EvidenceState.UNKNOWN}
           for phase in (item.identity, item.content, item.placement)) for item in items):
        return "failed"
    return "waiting"



def stage_state(values: list[dict[str, Any]], provider_error: object = None) -> StageState:
    """A provider stage's state from its item evidence, ignoring advisory notes."""
    state = evidence_state(values)
    waiting_with_failures = False
    # A transport/launch failure outside the enumerated item evidence cannot
    # be erased by the successful observations for the other items.
    if provider_error:
        state = "failed"
    elif state == "failed" and any(
            isinstance(item, dict)
            and any(isinstance(item.get(phase), dict) and item[phase].get("state") == "waiting"
                    for phase in PHASES)
            and not any(isinstance(item.get(phase), dict) and item[phase].get("state") in {"failed", "unknown"}
                        for phase in PHASES) and not blocking_attention(item.get("attention") or ())
            for item in values):
        # A failed item does not settle the category while another item's
        # placement is still owned by the compositor.
        state = "waiting"
        waiting_with_failures = True
    elif state == "failed" and any(evidence_state([item]) == "ready" for item in values
                                   if item.get("item_id") != RECIPE_AGE_ITEM):
        # Some items failed while others verified: a partly restored category.
        state = "degraded"
    return StageState(state, displayed_state(state, values), waiting_with_failures)


def provider_items(document: dict[str, Any]):
    """Every persisted item with the stage that holds it."""
    for stage in document.get("stages", []):
        if not isinstance(stage, dict) or not isinstance(stage.get("provider_results"), list):
            continue
        for item in stage["provider_results"]:
            if isinstance(item, dict):
                yield stage, item


def stage_waiting_phases(stage: dict[str, Any]):
    """Each phase of the stage's items that still waits: (item, phase name, phase)."""
    for item in stage.get("provider_results") or ():
        if isinstance(item, dict):
            for name in PHASES:
                phase = item.get(name)
                if isinstance(phase, dict) and phase.get("state") == "waiting":
                    yield item, name, phase


def waiting_phases(document: dict[str, Any]):
    """Each waiting phase of the document: (stage, item, phase name, phase). The one pending test."""
    for stage in document.get("stages", []):
        if isinstance(stage, dict) and isinstance(stage.get("provider_results"), list):
            for item, name, phase in stage_waiting_phases(stage):
                yield stage, item, name, phase


def placement_pending(stage: dict[str, Any] | None) -> bool:
    """Whether the compositor still owns a placement request of this stage."""
    return bool(stage) and stage.get("state") == "waiting" and any(
        phase.get("request_id") for _item, _name, phase in stage_waiting_phases(stage))


def deferred(stage: dict[str, Any] | None) -> bool:
    """A stage that startup left `waiting` for the cloud drives has no results yet."""
    return bool(stage) and stage.get("state") == "waiting" and not stage.get("provider_results")


def not_failed(stage: dict[str, Any] | None) -> bool:
    """Nothing is deferred: a missing stage is nothing to do, a failed one stays failed."""
    return not stage or stage.get("state") != "failed"
