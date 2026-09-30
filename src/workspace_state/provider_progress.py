"""Bounded observation of existing provider requests; never replay restoration."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from typing import Any

from . import login_status, operations
from .provider_results import EvidenceState, PhaseEvidence, ProviderItemResult
from .startup import StageMarker, read_stage_marker, runtime_identity, write_stage_marker

PHASES = ("identity", "content", "placement")
PROVIDER_STAGES = {"browsers", "social-apps", "file-manager", "vscode"}
CATEGORY_STAGES = PROVIDER_STAGES | {"terminals", "virtual-machines"}
MAX_REQUESTS = 4
MAX_BUDGET_SECONDS = 1.0


def evidence_from_dict(value: dict[str, Any]) -> ProviderItemResult:
    """Decode persisted evidence strictly; corrupted markers never prove success."""
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
    if any(item.attention or any(phase.state in {EvidenceState.FAILED, EvidenceState.UNKNOWN}
           for phase in (item.identity, item.content, item.placement)) for item in items):
        return "failed"
    return "waiting"


def _provider_items(document: dict[str, Any]):
    for stage in document.get("stages", []):
        if not isinstance(stage, dict) or not isinstance(stage.get("provider_results"), list):
            continue
        for item in stage["provider_results"]:
            if isinstance(item, dict):
                yield stage, item


def has_pending(document: dict[str, Any]) -> bool:
    if not isinstance(document, dict) or document.get("mode") != "startup" or not isinstance(document.get("operation_context"), dict):
        return False
    return any(any(isinstance(item.get(phase), dict) and item[phase].get("state") == "waiting"
                   for phase in PHASES) for _stage, item in _provider_items(document))


def failure_summary(values: list[dict[str, Any]], fallback: str) -> str:
    """Keep the actionable item failure visible on the collapsed HUD row."""
    for item in values:
        if not isinstance(item, dict):
            continue
        details = item.get("attention", [])
        if not isinstance(details, list):
            details = []
        details = [detail for detail in details if isinstance(detail, str) and detail.strip()]
        for name in PHASES:
            phase = item.get(name, {})
            if (isinstance(phase, dict) and phase.get("state") in {"failed", "unknown"}
                    and isinstance(phase.get("detail"), str) and phase["detail"].strip()):
                details.append(phase["detail"])
        if details:
            label = str(item.get("item_id") or item.get("provider") or "Application")
            return f"{label}: {' '.join(details[0].split())}"[:240]
    return " ".join(str(fallback).split())[:240]


def refresh_stage_evidence(document: dict[str, Any]) -> None:
    """Called only while the status lock is held."""
    provider_waiting = False
    provider_failed = False
    for stage in document.get("stages", []):
        if not isinstance(stage, dict) or stage.get("id") not in PROVIDER_STAGES:
            continue
        values = stage.get("provider_results")
        if not isinstance(values, list) or not values:
            continue
        state = evidence_state(values)
        # A transport/launch failure outside the enumerated item evidence cannot
        # be erased by the successful observations for the other items.
        if stage.get("provider_error"):
            state = "failed"
        message = {"ready": "All provider phases verified", "skipped": "No saved provider items",
                   "waiting": "Waiting for application restore verification",
                   "failed": "Provider restoration needs attention; saved intent is preserved"}[state]
        current = sum(evidence_state([item]) == "ready" for item in values)
        if state == "failed":
            message = f"{current}/{len(values)} verified · " + failure_summary(
                values, stage.get("provider_error") or message)
        stage.update(state=state, message=message, current=current, total=len(values))
        if state == "failed":
            stage["error"] = str(stage.get("provider_error") or message)
        else:
            stage.pop("error", None)
        login_status._record_stage_event(stage, state, message)
        provider_waiting |= state == "waiting"
        provider_failed |= state == "failed"
    stages = {stage.get("id"): stage for stage in document.get("stages", []) if isinstance(stage, dict)}
    workspace = stages.get("workspace")
    if workspace is not None and workspace.get("provider_completion_pending"):
        # Only promote an aggregate that the startup worker explicitly handed
        # over after all its synchronous category work completed.
        category_states = [stages.get(name, {}).get("state") for name in CATEGORY_STAGES]
        if provider_failed or "failed" in category_states:
            workspace.update(state="failed", message="Workspace restoration needs attention; saved intent is preserved")
        elif provider_waiting or any(state not in login_status.TERMINAL_STATES for state in category_states):
            workspace.update(state="waiting", message="Waiting for application restore verification")
        elif "degraded" in category_states:
            workspace.update(state="degraded", message="Application restoration completed with unverified details")
            workspace.pop("provider_completion_pending", None)
        else:
            workspace.update(state="ready", message="Application workspace restoration verified", current=1, total=1)
            workspace.pop("error", None)
            workspace.pop("provider_completion_pending", None)
    states = [stage.get("state") for stage in stages.values()]
    if provider_waiting:
        document["overall_message"] = "Waiting for application restore verification"
    elif provider_failed:
        document["overall_message"] = "Application restoration needs attention; saved intent is preserved"
    elif states and all(state in {"ready", "skipped"} for state in states):
        document["overall_message"] = "All login systems are ready"

    if (document.get("mode") == "startup" and states
            and all(state in login_status.TERMINAL_STATES for state in states)):
        target = "failed" if "failed" in states else "completed"
        if document.get("operation_state", "running") in {"running", target}:
            operations.transition(document, target)


def _query_request(token: str, timeout: float) -> dict[str, Any]:
    """Bound a CLI and its gdbus child together, with no unrelated processes."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(["gnome-winctl", "expectation", token, "--json"],
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
    return result if isinstance(result, dict) else {"token": token, "status": "unknown"}


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
    state = ("verified" if result.get("urls_restored") is True else
             "accepted" if result.get("urls_pending") is True else "failed")
    return {"token": token, "status": state,
            "detail": "; ".join(result.get("url_errors") or ["Exact loaded tab URLs verified"])}


def _update_markers(document: dict[str, Any], context: operations.OperationContext) -> None:
    root, boot, generation = runtime_identity()
    if boot != context.boot_id or (generation is not None and generation != context.login_generation):
        return
    # Do not acquire the startup-directory lock while holding the status lock.
    # Resolve only a directory that the startup worker already owns.
    directory = root / (f"startup-{boot}-{generation}" if generation else f"startup-{boot}")
    if not directory.is_dir() and generation:
        legacy = root / f"startup-{boot}"
        try:
            owner = json.loads((legacy / "generation-owner.json").read_text())
        except (OSError, ValueError):
            return
        if owner != {"schema_version": 1, "boot_id": boot, "login_generation": generation}:
            return
        directory = legacy
    if not directory.is_dir():
        return
    stages = {stage.get("id"): stage for stage in document.get("stages", []) if isinstance(stage, dict)}
    for category in CATEGORY_STAGES:
        stage = stages.get(category, {})
        path = directory / f"{category}.done"
        previous = read_stage_marker(path, category)
        if (previous is None or previous.operation_context != context.to_dict()
                or stage.get("state") not in {"ready", "skipped", "waiting", "failed"}):
            continue
        write_stage_marker(path, StageMarker(category, str(stage["state"]), previous.snapshot,
                                           str(stage.get("message", "")), context.to_dict(),
                                           tuple(stage.get("provider_results", previous.provider_results))))
    browser_items = {(item.get("provider"), item.get("item_id")): item
                     for item in stages.get("browsers", {}).get("provider_results", [])
                     if isinstance(item, dict)}
    for path in sorted((directory / "browser-items").glob("*.done"))[:128]:
        previous = read_stage_marker(path, "browsers")
        if previous is None or previous.operation_context != context.to_dict() or not previous.provider_results:
            continue
        replacements = [browser_items.get((item.get("provider"), item.get("item_id")))
                        for item in previous.provider_results]
        if any(current is None or any(old.get(phase, {}).get("request_id") != current.get(phase, {}).get("request_id")
                                     for phase in PHASES if old.get(phase, {}).get("state") == "waiting")
               for old, current in zip(previous.provider_results, replacements)):
            continue
        if tuple(replacements) != previous.provider_results:
            state = evidence_state(replacements)
            write_stage_marker(path, StageMarker("browsers", state, previous.snapshot,
                                               "Chrome restore observation updated", context.to_dict(), tuple(replacements)))
    # Autosave requires all attempted categories to have verified proof plus
    # resolved Codex identities. Pending/failed/legacy markers never arm it.
    if all((marker := read_stage_marker(directory / f"{name}.done", name)) is not None
           and marker.verified_for(context) for name in CATEGORY_STAGES) and stages.get("codex", {}).get("state") in {"ready", "skipped"}:
        path = directory / "autosave.ready"
        path.write_text("ready\n")
        path.chmod(0o600)
    else:
        (directory / "autosave.ready").unlink(missing_ok=True)


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
        for stage, item in _provider_items(snapshot):
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
        if owner.deadline > started:
            for phase_name, token in tokens[:min(MAX_REQUESTS, max(0, max_requests))]:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                outcome["queried"] += 1
                try:
                    result = (_query_browser_request(token, remaining) if phase_name == "content"
                              else _query_request(token, remaining))
                except OSError:
                    result = {"token": token, "status": "unknown"}
                if result.get("token") == token and result.get("deferred") is not True:
                    observations[(phase_name, token)] = result
        def mutate(document):
            expired = owner.deadline <= time.monotonic()
            for stage, item in _provider_items(document):
                key = (str(stage.get("id")), str(item.get("provider")), str(item.get("item_id")))
                for phase_name in PHASES:
                    phase = item.get(phase_name, {})
                    if phase.get("state") != "waiting":
                        continue
                    token = phase.get("request_id")
                    observation = observations.get((phase_name, token), {}) if expected.get((*key, phase_name)) == token else {}
                    state = "expired" if expired else observation.get("status")
                    if state == "verified":
                        detail = ("Exact loaded tab URLs verified" if phase_name == "content"
                                  else "Compositor verified the exact placement request")
                        phase.update(state="verified", retryable=False, detail=detail)
                    elif state in {"failed", "expired", "cancelled"}:
                        phase.update(state="failed", retryable=True,
                                     detail=(observation.get("detail") or
                                             f"{phase_name.capitalize()} {state}; saved intent and existing window are preserved"))
                item["success"] = evidence_state([item]) == "ready"
                item["retryable"] = any(item.get(name, {}).get("retryable") is True for name in PHASES)
            if expired:
                for stage in document.get("stages", []):
                    if isinstance(stage, dict) and stage.get("state") in {"pending", "running", "waiting"}:
                        message = "Startup operation deadline expired; saved intent is preserved"
                        stage.update(state="failed", message=message, error=message)
                        if stage.get("provider_results"):
                            stage["provider_error"] = message
                        login_status._record_stage_event(stage, "failed", message)
            document["provider_progress_cursor"] = cursor + outcome["queried"]
            refresh_stage_evidence(document)
            _update_markers(document, owner)
            outcome["pending"] = has_pending(document)
        outcome["updated"] = login_status._locked_update(mutate, context=owner, mode="startup", allow_expired=True,
                                                          lock_timeout=max(0.0, end - time.monotonic()))
        outcome["needs_retry"] = not outcome["updated"]
        return outcome
    except (OSError, TypeError, KeyError, ValueError):
        outcome["needs_retry"] = True
        return outcome


def cmd_placement_progress(_args: argparse.Namespace) -> int:
    outcome = reconcile_pending()
    print(json.dumps(outcome, sort_keys=True))
    return int(outcome["needs_retry"] or (outcome["expired"] and not outcome["updated"]))
