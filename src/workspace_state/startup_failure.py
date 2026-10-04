"""Summarize failed startup evidence without treating aggregates as new crashes."""
from __future__ import annotations


def browser_reconciliation_pending(document: dict) -> bool:
    for stage in document.get("stages", []):
        if not isinstance(stage, dict) or stage.get("id") != "browsers" or stage.get("state") != "failed":
            continue
        if str(stage.get("provider_error", "")).startswith("Browser reconciliation needs review:"):
            return True
        for item in stage.get("provider_results", []):
            if not isinstance(item, dict):
                continue
            identity = item.get("identity", {})
            if not isinstance(identity, dict) or identity.get("state") != "failed":
                continue
            detail = str(identity.get("detail", ""))
            if (detail.startswith("Browser reconciliation needs review:")
                    or "Original grouped Chrome window does not match the saved tabs" in detail):
                return True
    return False


def failure_message(document: dict, failed: list[str]) -> str:
    if browser_reconciliation_pending(document):
        workspace_aggregate = any(isinstance(stage, dict) and stage.get("id") == "workspace"
                                  and stage.get("provider_completion_pending")
                                  for stage in document.get("stages", []))
        summarized = {"browsers", "login-finalization"}
        if workspace_aggregate:
            summarized.add("workspace")
        additional = [name for name in failed if name not in summarized]
        reason = "Browser reconciliation needs review: saved tabs differ from the original grouped window."
        for stage in document.get("stages", []):
            if not isinstance(stage, dict) or stage.get("id") != "browsers":
                continue
            candidates = [stage.get("provider_error", "")]
            candidates.extend(item.get("identity", {}).get("detail", "")
                              for item in stage.get("provider_results", [])
                              if isinstance(item, dict) and isinstance(item.get("identity"), dict))
            explicit = next((value for value in candidates if isinstance(value, str)
                             and value.startswith("Browser reconciliation needs review:")), None)
            if explicit:
                reason = " ".join(explicit.split())[:500]
        message = (reason + " Compare saved and open tabs; choose the baseline before saving or retrying. "
                   "Login failure summarizes this unresolved restore.")
        if workspace_aggregate:
            message += " Workspace failure is the same aggregate."
        if additional:
            message += " Other failed steps: " + ", ".join(additional)
        return message
    return "Login completed with failures: " + ", ".join(failed)
