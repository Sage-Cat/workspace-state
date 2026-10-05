"""Operation-scoped actual tmux identities, separate from saved names/indices."""
from __future__ import annotations

import json
from pathlib import Path

from .restore import _codex_ids, _session_fingerprint, codex_pane_bindings, tmux_runtime_identity
from .util import atomic_json


FILE_NAME = 'terminal-session-map.json'


def _owned(context, status_path):
    context.check()
    status = json.loads(status_path.read_text())
    if (context.mode != 'startup' or not context.matches(status)
            or status.get('operation_state') != 'running' or status.get('startup_suspended')):
        raise RuntimeError('Terminal restoration no longer owns this startup')


def publish(path: Path, sessions: list[dict], names: dict[str, str], context,
            status_path: Path) -> None:
    """Record explicit actual names and live anchors; never infer a global match."""
    _owned(context, status_path)
    entries = {}
    for session in sessions:
        saved = session['name']
        if saved in entries:
            continue  # Multiple terminal clients can share one tmux session.
        actual = names[saved]
        runtime = tmux_runtime_identity(actual)
        if runtime is None:
            raise RuntimeError(f'Cannot bind restored tmux session {actual}')
        entries[saved] = {'actual_name': actual, 'recipe_fingerprint': _session_fingerprint(session),
                          'runtime': runtime, 'codex_panes': codex_pane_bindings(session, actual)}
    _owned(context, status_path)
    atomic_json(path, {'schema_version': 1, 'operation_context': context.to_dict(),
                       'sessions': entries})


def read(path: Path, session: dict, context) -> dict | None:
    """Validate recipe, operation, server and immutable session identity anew."""
    if context is None:
        return None
    try:
        context.check()
        value = json.loads(path.read_text())
        if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('operation_context') != context.to_dict()):
            return None
        entry = value['sessions'][session['name']]
        actual, panes = entry['actual_name'], entry['codex_panes']
        if (not isinstance(actual, str) or not actual or not isinstance(panes, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.startswith('%')
                       for k, v in panes.items())
                or len(set(panes.values())) != len(panes)
                or not set(panes).issubset(set(_codex_ids(session).values()))
                or entry['recipe_fingerprint'] != _session_fingerprint(session)
                or not isinstance(entry.get('runtime'), dict)
                or entry['runtime'] != tmux_runtime_identity(actual)):
            return None
        return entry
    except (OSError, TypeError, ValueError, KeyError, TimeoutError, AttributeError):
        return None
