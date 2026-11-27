"""Bounded, read-only provider observation."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from dataclasses import replace
from typing import Any

from . import login_status, operations
from .provider_results import EvidenceState, PhaseEvidence, ProviderItemResult, NATIVE_HANDOFF_PREFIX, RECIPE_AGE_ITEM, ADVISORY_PREFIX, blocking_attention, has_advisory
from .stage_state import PHASES, evidence_state, provider_items, stage_state, stage_waiting_phases, waiting_phases
from .startup import runtime_identity

PROVIDER_STAGES = {"browsers", "social-apps", "file-manager", "vscode", "terminals"}
CATEGORY_STAGES = PROVIDER_STAGES
MAX_REQUESTS = 4
MAX_BUDGET_SECONDS = 1.0


def login_status_document(login: dict[str, Any] | None) -> dict[str, Any] | None:
    """The status document, when it belongs to the startup of `login`."""
    if login is None:
        return None
    try:
        document = json.loads(login_status.status_path().read_text())
        context = operations.OperationContext.from_dict(document.get("operation_context"))
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(document, dict) or document.get("mode") != "startup"
            or context.boot_id != login.get("boot_id")
            or document.get("session_id") != login.get("login_generation")
            or document.get("login_key") != login.get("login_key")):
        return None
    return document


def has_pending(document: dict[str, Any]) -> bool:
    if not isinstance(document, dict) or document.get("mode") != "startup" or not isinstance(document.get("operation_context"), dict):
        return False
    return any(True for _ in waiting_phases(document))


def has_native_handoff(document: dict[str, Any]) -> bool:
    return any(name == "placement" and str(phase.get("request_id", "")).startswith(NATIVE_HANDOFF_PREFIX)
               for _stage, _item, name, phase in waiting_phases(document))


def only_native_handoffs_pending(document: dict[str, Any]) -> bool:
    """No application work remains: every wait is an already-submitted move."""
    return has_native_handoff(document) and all(
        name == "placement" and str(phase.get("request_id", "")).startswith(NATIVE_HANDOFF_PREFIX)
        for _stage, _item, name, phase in waiting_phases(document))


def item_needs_attention(item: Any) -> bool:
    """A persisted item with a failed or unknown phase, or explicit attention."""
    return isinstance(item, dict) and (bool(blocking_attention(item.get("attention") or ())) or any(
        isinstance(item.get(phase), dict) and item[phase].get("state") in {"failed", "unknown"}
        for phase in PHASES))


def failure_summary(values: list[dict[str, Any]], fallback: str, *, advisory: bool = True) -> str:
    """Keep the actionable item failure visible on the collapsed HUD row.

    Real problems come first; advisory notes are named only when `advisory` is
    set, so they never stand in for an error that is not an item.
    """
    for notes in (False, True) if advisory else (False,):
        for item in values:
            if not isinstance(item, dict):
                continue
            details = item.get("attention", [])
            if not isinstance(details, list):
                details = []
            details = [detail for detail in details if isinstance(detail, str) and detail.strip()
                       and detail.startswith(ADVISORY_PREFIX) == notes]
            for name in PHASES:
                phase = item.get(name, {})
                if (not notes and isinstance(phase, dict) and phase.get("state") in {"failed", "unknown"}
                        and isinstance(phase.get("detail"), str) and phase["detail"].strip()):
                    details.append(phase["detail"])
            if details:
                label = ("Chrome recipe" if item.get("item_id") == RECIPE_AGE_ITEM
                         else str(item.get("item_id") or item.get("provider") or "Application"))
                return f"{label}: {' '.join(details[0].removeprefix(ADVISORY_PREFIX).split())}"[:240]
    return " ".join(str(fallback).split())[:240]


def refresh_stage_evidence(document: dict[str, Any]) -> None:
    """Called only while the status lock is held."""
    provider_waiting = False
    provider_failed = False
    provider_degraded = False
    for stage in document.get("stages", []):
        if not isinstance(stage, dict) or stage.get("id") not in PROVIDER_STAGES:
            continue
        values = stage.get("provider_results")
        if not isinstance(values, list) or not values:
            continue
        derived = stage_state(values, stage.get("provider_error"))
        state, waiting_with_failures = derived.shown, derived.waiting_with_failures
        message = {"ready": "All provider phases verified", "skipped": "No saved provider items",
                   "waiting": "Waiting for application restore verification",
                   "degraded": "Provider restoration needs attention; saved intent is preserved",
                   "failed": "Provider restoration needs attention; saved intent is preserved"}[state]
        # The age of a retained recipe is a note about the whole stage, not an item.
        counted = [item for item in values if not (isinstance(item, dict) and item.get("item_id") == RECIPE_AGE_ITEM)]
        current = sum(evidence_state([item]) == "ready" for item in counted)
        if state == "failed":
            # A window that only has a note about its placement is not verified here.
            current = sum(evidence_state([item]) == "ready" and not has_advisory(item) for item in counted)
        if state in {"failed", "degraded"}:
            summary = failure_summary(values, stage.get("provider_error") or message,
                                      advisory=state == "degraded")
            needing = sum(item_needs_attention(item) or has_advisory(item) for item in counted)
            real = bool(stage.get("provider_error")) or any(item_needs_attention(item) for item in values)
            message = (f"{current}/{len(counted)} verified · " + summary if state == "failed" else
                       (f"{needing} of {len(counted)} item(s) need attention · " if needing else "") + summary)
            if stage.get("id") == "browsers" and real:
                # Periodic checkpoints keep the Chrome recipe until the user accepts the live windows.
                message += " · run 'wsctl save' once Chrome looks right"
        if waiting_with_failures:
            # The failure must stay visible while the rest still waits.
            broken = sum(item_needs_attention(item) for item in counted)
            message = (f"{broken} of {len(counted)} item(s) need attention · "
                       + failure_summary(values, message, advisory=False) + "; waiting for placement of the rest")
        stage.update(state=state, message=message, current=current, total=len(counted))
        if state == "failed":
            stage["error"] = str(stage.get("provider_error") or message)
        else:
            stage.pop("error", None)
        login_status._record_stage_event(stage, state, message)
        provider_waiting |= state == "waiting"
        provider_failed |= state == "failed"
        provider_degraded |= state == "degraded"
    stages = {stage.get("id"): stage for stage in document.get("stages", []) if isinstance(stage, dict)}
    workspace = stages.get("workspace")
    if workspace is not None and workspace.get("provider_completion_pending"):
        # Only promote an aggregate that the startup worker explicitly handed
        # over after all its synchronous category work completed.
        category_states = [stages.get(name, {}).get("state") for name in CATEGORY_STAGES]
        if provider_failed or "failed" in category_states:
            from .startup_failure import browser_reconciliation_pending, browser_placement_failed, failure_message
            message = (failure_message(document, [name for name in CATEGORY_STAGES
                                                  if stages.get(name, {}).get("state") == "failed"])
                       if browser_reconciliation_pending(document) or browser_placement_failed(document) else
                       "Workspace restoration needs attention; saved intent is preserved")
            workspace.update(state="failed", message=message)
        elif provider_waiting or any(state not in login_status.TERMINAL_STATES for state in category_states):
            workspace.update(state="waiting", message="Waiting for application restore verification")
        elif provider_degraded or "degraded" in category_states:
            workspace.update(state="degraded", message="Application restoration completed with unverified details")
            workspace.pop("provider_completion_pending", None)
        else:
            workspace.update(state="ready", message="Application workspace restoration verified", current=1, total=1)
            workspace.pop("error", None)
            workspace.pop("provider_completion_pending", None)
    states = [stage.get("state") for stage in stages.values()]
    if provider_waiting:
        document["overall_message"] = "Waiting for application restore verification"
    elif provider_failed or provider_degraded:
        document["overall_message"] = "Application restoration needs attention; saved intent is preserved"
    elif states and all(state in {"ready", "skipped"} for state in states):
        document["overall_message"] = "All login systems are ready"

    # A failed aggregate may still contain another item's owned wait. Finish
    # observing it before closing the operation, preserving the failure itself.
    if (document.get("mode") == "startup" and states and not has_pending(document)
            and all(state in login_status.TERMINAL_STATES for state in states)):
        target = "failed" if "failed" in states else "completed"
        if document.get("operation_state", "running") in {"running", target}:
            operations.transition(document, target)


def _query_request(token: str, timeout: float) -> dict[str, Any]:
    """Bound a CLI and its gdbus child together, with no unrelated processes."""
    deadline = time.monotonic() + timeout
    native_token = token.removeprefix(NATIVE_HANDOFF_PREFIX)
    process = subprocess.Popen(["gnome-winctl", "expectation", native_token, "--json"],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    try:
        stdout, _stderr = process.communicate(timeout=max(.001, deadline - time.monotonic() - .025))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            process.communicate(timeout=max(.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            # A kernel-stalled process must not turn observation into an
            # unbounded join. Its process group has already received SIGKILL.
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        return {"token": token, "status": "unknown"}
    if process.returncode:
        return {"token": token, "status": "unknown"}
    try:
        result = json.loads(stdout)
    except ValueError:
        return {"token": token, "status": "unknown"}
    if not isinstance(result, dict):
        return {"token": token, "status": "unknown"}
    if token.startswith(NATIVE_HANDOFF_PREFIX):
        if result.get("token") != native_token or result.get("handoff_owned") is not True:
            return {"token": token, "status": "failed", "detail": "GNOME no longer owns the saved placement handoff"}
        result["token"] = token
    return result


def observe_manual_placements(items: list[ProviderItemResult], *,
                              budget_seconds: float = 5.0) -> list[ProviderItemResult]:
    """Observe a batch of submitted compositor handoffs under one shared deadline."""
    from .concurrency import completed_jobs

    deadline = time.monotonic() + budget_seconds
    pending = {item.placement.request_id for item in items
               if item.placement.state == EvidenceState.WAITING
               and (item.placement.request_id or "").startswith(NATIVE_HANDOFF_PREFIX)}
    observed = {}

    def query(token):
        remaining = deadline - time.monotonic()
        if remaining > 0:
            return _query_request(token, min(0.5, remaining))
        return {}

    while pending and time.monotonic() < deadline:
        jobs = {token: lambda token=token: query(token) for token in sorted(pending)}
        for token, result, error in completed_jobs(jobs):
            if error is not None or result.get("token") != token:
                continue
            status = result.get("status")
            if status == "verified" and result.get("deferred") is not True:
                observed[token] = PhaseEvidence(EvidenceState.VERIFIED,
                    "Compositor verified the exact placement request", request_id=token)
            elif status in {"failed", "expired", "cancelled"}:
                observed[token] = PhaseEvidence(EvidenceState.FAILED,
                    result.get("detail") or result.get("message") or f"Compositor placement {status}", True, token)
            else:
                continue
            pending.remove(token)
        if pending and (remaining := deadline - time.monotonic()) > 0:
            time.sleep(min(0.1, remaining))
    return [replace(item, placement=observed[item.placement.request_id])
            if item.placement.state == EvidenceState.WAITING and item.placement.request_id in observed
            else item for item in items]


def _query_browser_request(token: str, timeout: float) -> dict[str, Any]:
    """Observe the same claimed Chrome window, without navigating or focusing."""
    from .browser import BrowserUnavailable, request_browser
    try:
        request = json.loads(token)
        if (not isinstance(request, list) or len(request) != 3
                or not all(isinstance(value, str) and value for value in request[:2])
                or type(request[2]) is not int):
            raise ValueError("invalid Chrome observation identity")
        profile, restore_token, window_id = request
    except (TypeError, ValueError):
        return {"token": token, "status": "failed", "detail": "Invalid Chrome observation identity"}
    try:
        result = request_browser("restore_status", {"restore_token": restore_token},
                                 profile=profile, timeout=timeout)
    except BrowserUnavailable:
        return {"token": token, "status": "unknown"}
    if not isinstance(result, dict):
        return {"token": token, "status": "unknown"}
    if result.get("exists") is not True or result.get("window_id") != window_id:
        return {"token": token, "status": "failed", "detail": "The claimed Chrome window is no longer available"}
    state = ("failed" if result.get("group_warnings") else
             "verified" if result.get("urls_restored") is True else
             "accepted" if result.get("urls_pending") is True else "failed")
    return {"token": token, "status": state,
            "detail": "; ".join(result.get("group_warnings") or result.get("url_errors") or ["Exact saved tab URLs verified"])}


def _update_browser_receipt(document: dict[str, Any], context: operations.OperationContext) -> None:
    """Settle the browser reconciliation receipt with the outcome of the stage."""
    root, boot, session_id, login_key = runtime_identity()
    if boot != context.boot_id or (session_id is not None and session_id != context.login_generation):
        return
    # Do not acquire the startup-directory lock while holding the status lock.
    # Resolve only a directory that the startup restore already owns.
    directory = root / (f"startup-{boot}-{login_key}" if login_key else f"startup-{boot}")
    if not directory.is_dir():
        return
    stages = {stage.get("id"): stage for stage in document.get("stages", []) if isinstance(stage, dict)}
    receipt_path = directory / "browser-reconciliation.json"
    try:
        receipt = json.loads(receipt_path.read_text())
        stage = stages.get("browsers", {})
        if (receipt.get("operation_context") == context.to_dict()
                and receipt.get("state") in {"waiting-reuse-only", "verified-reuse-only"}
                and stage.get("state") in {"ready", "failed"}):
            from .util import atomic_json
            receipt.update(state="verified-reuse-only" if stage["state"] == "ready" else "failed",
                           verified_windows=stage.get("current", 0),
                           failures=[] if stage["state"] == "ready" else [stage.get("message", "Browser verification failed")])
            atomic_json(receipt_path, receipt)
    except (OSError, TypeError, ValueError):
        pass


def reconcile_pending(context: operations.OperationContext | None = None, *,
                      max_requests: int = MAX_REQUESTS,
                      budget_seconds: float = MAX_BUDGET_SECONDS) -> dict[str, Any]:
    """Observe at most four exact requests in one second; no launch/place/focus."""
    started = time.monotonic()
    end = started + min(MAX_BUDGET_SECONDS, max(0.0, budget_seconds))
    outcome = {"queried": 0, "updated": False, "pending": False, "expired": False, "needs_retry": False}
    try:
        snapshot = json.loads(login_status.status_path().read_text())
        owner = operations.OperationContext.from_dict(snapshot.get("operation_context"))
        inherited = context or operations.current()
        if (inherited is not None and inherited != owner) or owner.mode != "startup" or not owner.matches(snapshot) or owner.boot_id != operations.boot_id():
            return outcome
        if snapshot.get("operation_state") in {"completed", "cancelled", "failed"}:
            return outcome  # Late receipts cannot reopen a terminal operation.
        outcome["expired"] = owner.deadline <= started
        if not has_pending(snapshot) and not outcome["expired"]:
            return outcome
        outcome["pending"] = has_pending(snapshot)
        expected: dict[tuple[str, str, str, str], str] = {}
        tokens = []
        for stage, item in provider_items(snapshot):
            phases = ("placement", "content") if item.get("provider") == "chrome" else ("placement",)
            for phase_name in phases:
                phase = item.get(phase_name, {})
                token = phase.get("request_id")
                if phase.get("state") == "waiting" and isinstance(token, str) and token and len(token) <= 256:
                    expected[(str(stage["id"]), str(item["provider"]), str(item["item_id"]), phase_name)] = token
                    request = (phase_name, token)
                    if request not in tokens:
                        tokens.append(request)
        cursor = int(snapshot.get("provider_progress_cursor", 0)) % max(1, len(tokens))
        tokens = tokens[cursor:] + tokens[:cursor]
        observations = {}
        if owner.deadline > started or has_native_handoff(snapshot):
            for phase_name, token in tokens[:min(MAX_REQUESTS, max(0, max_requests))]:
                if owner.deadline <= started and not token.startswith(NATIVE_HANDOFF_PREFIX):
                    continue
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                outcome["queried"] += 1
                try:
                    result = (_query_browser_request(token, remaining) if phase_name == "content"
                              else _query_request(token, remaining))
                except OSError:
                    result = {"token": token, "status": "unknown"}
                if result.get("token") == token and (result.get("deferred") is not True
                        or result.get("status") in {"failed", "expired", "cancelled"}):
                    observations[(phase_name, token)] = result
        verified_terminals: list[str] = []

        def mutate(document):
            expired = owner.deadline <= time.monotonic()
            verified_terminals.clear()
            for stage, item in provider_items(document):
                key = (str(stage.get("id")), str(item.get("provider")), str(item.get("item_id")))
                for phase_name in PHASES:
                    phase = item.get(phase_name, {})
                    if phase.get("state") != "waiting":
                        continue
                    token = phase.get("request_id")
                    observation = observations.get((phase_name, token), {}) if expected.get((*key, phase_name)) == token else {}
                    durable = phase_name == "placement" and str(token).startswith(NATIVE_HANDOFF_PREFIX)
                    state = "expired" if expired and not durable else observation.get("status")
                    if expired and durable and state == "unknown":
                        state = "failed"
                    if state == "verified":
                        detail = ("Exact saved tab URLs verified" if phase_name == "content"
                                  else "Compositor verified the exact placement request")
                        phase.update(state="verified", retryable=False, detail=detail)
                        if phase_name == "placement" and item.get("provider") == "alacritty":
                            verified_terminals.append(str(token))
                    elif state in {"failed", "expired", "cancelled"}:
                        phase.update(state="failed", retryable=True,
                                     detail=(observation.get("detail") or observation.get("message") or
                                             f"{phase_name.capitalize()} {state}; saved intent and existing window are preserved"))
                item["success"] = evidence_state([item]) == "ready"
                item["retryable"] = any(item.get(name, {}).get("retryable") is True for name in PHASES)
            if expired:
                durable_pending = has_native_handoff(document)
                for stage in document.get("stages", []):
                    if isinstance(stage, dict) and stage.get("state") in {"pending", "running", "waiting"}:
                        if stage.get("provider_results") and not any(True for _ in stage_waiting_phases(stage)):
                            continue  # Nothing is pending: refresh settles the observed evidence below.
                        if stage.get("id") == "workspace" and stage.get("provider_completion_pending"):
                            continue  # Derive this aggregate from the category results below.
                        if durable_pending and (stage.get("id") == "workspace" or any(
                                item.get("placement", {}).get("state") == "waiting" and
                                str(item["placement"].get("request_id", "")).startswith(NATIVE_HANDOFF_PREFIX)
                                for item in stage.get("provider_results", []))):
                            continue  # Observing an accepted move grants no new mutation authority.
                        message = "Startup operation deadline expired; saved intent is preserved"
                        stage.update(state="failed", message=message, error=message)
                        if stage.get("provider_results"):
                            stage["provider_error"] = message
                        login_status._record_stage_event(stage, "failed", message)
            document["provider_progress_cursor"] = cursor + outcome["queried"]
            refresh_stage_evidence(document)
            _update_browser_receipt(document, owner)
            outcome["pending"] = has_pending(document)
        outcome["updated"] = login_status._locked_update(mutate, context=owner, mode="startup", allow_expired=True,
                                                          lock_timeout=max(0.0, end - time.monotonic()))
        outcome["needs_retry"] = not outcome["updated"]
        if outcome["updated"] and verified_terminals:
            # The window's placement is verified: its saved record is no longer unfinished.
            from . import cli, pending
            pending.clear_unfinished(cli._seen_directory(cli._checkpoint_login()), request_ids=verified_terminals)
        return outcome
    except (OSError, TypeError, KeyError, ValueError):
        outcome["needs_retry"] = True
        return outcome


def cmd_placement_progress(_args: argparse.Namespace) -> int:
    outcome = reconcile_pending()
    print(json.dumps(outcome, sort_keys=True))
    return int(outcome["needs_retry"] or (outcome["expired"] and not outcome["updated"]))
