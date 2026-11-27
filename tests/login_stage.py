"""Write the status document of one login, as the startup restore publishes it."""
from __future__ import annotations

import json

from workspace_state import login_status, operations


def write_status(login, stages=None, *, context=None, restored_snapshot=None,
                 session_id=None, login_key=None, operation_state="running"):
    """`stages` maps a stage id to its fields (`state`, `provider_results`, ...)."""
    session_id = session_id or login["login_generation"]
    context = context or operations.OperationContext(
        login["boot_id"], session_id, "c" * 32, "startup", 1, 10**9)
    document = {
        "schema_version": 1, "mode": "startup", "session_id": session_id,
        "login_key": login_key or login["login_key"],
        "operation_id": context.operation_id, "operation_context": context.to_dict(),
        "operation_state": operation_state,
        "stages": [{"id": name, "message": "", **{"state": "ready", **fields}}
                   for name, fields in (stages or {}).items()],
    }
    if restored_snapshot is not None:
        document["restored_snapshot"] = restored_snapshot
    path = login_status.status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document))
    return document
