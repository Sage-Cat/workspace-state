"""Operation-scoped actual tmux identities, separate from saved names/indices."""
from __future__ import annotations

import json
from pathlib import Path

from .restore import (_codex_ids, _conversation_ids, _session_fingerprint,
                      codex_pane_bindings, conversation_pane_bindings, tmux_runtime_identity)
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
        legacy = codex_pane_bindings(session, actual)
        bindings = conversation_pane_bindings(session, actual)
        bindings.update({'codex:' + key: value for key, value in legacy.items()})
        entries[saved] = {'actual_name': actual, 'recipe_fingerprint': _session_fingerprint(session),
                          'runtime': runtime, 'codex_panes': legacy,
                          'conversation_panes': bindings}
    _owned(context, status_path)
    atomic_json(path, {'schema_version': 1, 'operation_context': context.to_dict(),
                       'sessions': entries})


def read(path: Path, session: dict, context, *, allow_expired: bool = False) -> dict | None:
    """Validate recipe, operation, server and immutable session identity anew."""
    if context is None:
        return None
    try:
        if allow_expired:
            from .operations import boot_id
            if context.boot_id != boot_id():
                return None
        else:
            context.check()
        value = json.loads(path.read_text())
        if (type(value.get('schema_version')) is not int or value['schema_version'] != 1
                or value.get('operation_context') != context.to_dict()):
            return None
        entry = value['sessions'][session['name']]
        actual = entry['actual_name']
        panes = entry.get('conversation_panes', entry.get('codex_panes'))
        if 'codex_panes' in entry and 'conversation_panes' not in entry:
            panes = {'codex:' + key: value for key, value in panes.items()}
        legacy = entry.get('codex_panes', {})
        if (not isinstance(actual, str) or not actual or not isinstance(panes, dict)
                or not isinstance(legacy, dict)
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.startswith('%')
                       for k, v in legacy.items())
                or len(set(legacy.values())) != len(legacy)
                or not set(legacy).issubset(set(_codex_ids(session).values()))
                or any(not isinstance(k, str) or not isinstance(v, str) or not v.startswith('%')
                       for k, v in panes.items())
                or len(set(panes.values())) != len(panes)
                or not set(panes).issubset(set(_conversation_ids(session).values()))
                or entry['recipe_fingerprint'] != _session_fingerprint(session)
                or not isinstance(entry.get('runtime'), dict)
                or entry['runtime'] != tmux_runtime_identity(actual)):
            return None
        return {**entry, 'conversation_panes': panes}
    except (OSError, TypeError, ValueError, KeyError, TimeoutError, AttributeError):
        return None


def continued(path: Path, sessions: list[dict], previous, context) -> tuple[dict, dict]:
    """Carry verified live anchors into an explicit same-login retry.

    Observing an expired attempt grants no old mutation authority and never
    infers a new name or pane assignment.
    """
    if (previous.mode != context.mode or context.mode != 'startup'
            or previous.boot_id != context.boot_id
            or previous.login_generation != context.login_generation
            or context.attempt != previous.attempt + 1):
        raise RuntimeError('Terminal mapping retry belongs to another login')
    before = json.loads(path.read_text())
    if (type(before.get('schema_version')) is not int or before['schema_version'] != 1
            or not isinstance(before.get('sessions'), dict)
            or before.get('operation_context') != previous.to_dict()
            or any(read(path, session, previous, allow_expired=True) is None for session in sessions)
            or json.loads(path.read_text()) != before):
        raise RuntimeError('Cannot carry changed or stale terminal bindings into a retry')
    return before, dict(before, operation_context=context.to_dict())
