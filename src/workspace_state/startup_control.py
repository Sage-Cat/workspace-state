"""Cooperative startup cancellation; never close restored applications.

A same-user private receipt revokes only its full immutable operation context.
The coordinator delegates bounded transport calls so its GNOME loop stays live.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat

from . import operations


StartupAborted = operations.StartupAborted


def _private_startup_json(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_nlink != 1):
            raise ValueError("unsafe startup cancellation record")
        data = stream.read(8193)
    if len(data) > 8192:
        raise ValueError("oversized startup cancellation record")
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("startup cancellation record is not an object")
    return value


def _abort_matches(receipt: dict, context: operations.OperationContext) -> bool:
    return (receipt.get("schema_version") == 1 and receipt.get("mode") == "startup"
            and receipt.get("session_id") == context.login_generation
            and receipt.get("operation_id") == context.operation_id
            and receipt.get("operation_context") == context.to_dict())


def abort_requested(context: operations.OperationContext) -> bool:
    if context.mode != "startup" or context.boot_id != operations.boot_id():
        return False
    from .login_status import runtime_root
    try:
        return _abort_matches(_private_startup_json(runtime_root() / "startup-abort.json"), context)
    except (OSError, ValueError, TypeError):
        return False


def check_abort(context: operations.OperationContext) -> None:
    if abort_requested(context):
        raise StartupAborted("Startup restoration was stopped by the user; restored applications are preserved")


def _publish(context, state, message):
    from . import login_status
    def update(document):
        if document.get("operation_state") not in {"running", "cancelling", "cancelled"}:
            raise ValueError("startup is already terminal")
        operations.transition(document, state)
        document["cancelled"] = state == "cancelled"
        document["overall_message"] = message
        if state == "cancelled":
            for stage in document.get("stages", []):
                if stage.get("state") not in login_status.TERMINAL_STATES:
                    stage.update(state="degraded", message="Stopped by user; partial restored state preserved")
                    login_status._record_stage_event(stage, "degraded", stage["message"])
    return login_status._locked_update(update, context=context, mode="startup", allow_expired=True,
                                       startup_control=True, event=message)


def cancel(context: operations.OperationContext) -> bool:
    from . import login_status
    if not abort_requested(context):
        return False
    document = json.loads(login_status.status_path().read_text())
    if not context.matches(document) or document.get("operation_state") not in {"running", "cancelling", "cancelled"}:
        return False
    if document.get("operation_state") == "cancelled":
        return True
    if not _publish(context, "cancelling", "Stopping startup restoration; restored applications are preserved"):
        return False
    from .browser import _host_paths, _request_path
    errors = []
    for path in _host_paths():
        try:
            result = _request_path(path, "abort_restore", {"operation_context": context.to_dict()},
                                   timeout=2, check_operation=False)
            if not isinstance(result, dict) or result.get("aborted") is not True or result.get("operation_context") != context.to_dict():
                raise RuntimeError("Chrome did not acknowledge exact cancellation context")
        except RuntimeError as error:
            errors.append(str(error))
    # Revoke only already-issued, owned staging handoffs. This does not close
    # or move a window that has already completed its placement.
    from .desktop import _winctl
    from .provider_progress import _provider_items
    from .provider_results import NATIVE_HANDOFF_PREFIX
    tokens = {str(item.get("placement", {}).get("request_id", ""))[len(NATIVE_HANDOFF_PREFIX):]
              for _stage, item in _provider_items(document)
              if item.get("placement", {}).get("state") == "waiting"
              and str(item.get("placement", {}).get("request_id", "")).startswith(NATIVE_HANDOFF_PREFIX)}
    for token in tokens:
        try:
            _winctl(["cancel", token], timeout=2)
        except (RuntimeError, FileNotFoundError) as error:
            errors.append(str(error))
    if errors:
        _publish(context, "cancelling", "Stop requested; waiting for provider acknowledgement: " + errors[0][:200])
        return False
    return _publish(context, "cancelled", "Startup restoration stopped; restored applications and saved snapshot are preserved")


def main():
    context = operations.current()
    if context is None:
        return 2
    try:
        return 0 if cancel(context) else 1
    except (OSError, ValueError, RuntimeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
