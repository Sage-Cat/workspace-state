"""Pass startup authority through legacy tmux process commands, not shells."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from . import operations
from .codex_resume import startup_context_argument, _process_stamp
from .login_status import runtime_root
from .util import atomic_json

BASE_MAPPING = '"wsctl-codex->wsctl-codex-resume *" "wsctl-claude->wsctl-claude-resume *"'
LEGACY_MAPPING = '"wsctl-codex->wsctl-codex-resume *"'


def scoped_mapping(context):
    # Keep the star against the closing quote: legacy resurrect expands its
    # unquoted restore-list output before eval, so a middle star becomes files.
    argument = startup_context_argument(context)
    return (BASE_MAPPING.replace('wsctl-codex-resume *', 'wsctl-codex-resume ' + argument + ' *')
            .replace('wsctl-claude-resume *', 'wsctl-claude-resume ' + argument + ' *'))


def scope_path():
    return runtime_root() / 'tmux-codex-launch-scope.json'


def _read_scope():
    try:
        value = json.loads(scope_path().read_text())
    except FileNotFoundError:
        return None
    context = operations.OperationContext.from_dict(value['operation_context'])
    if (value.get('schema_version') != 1 or type(value.get('owner_pid')) is not int
            or value['owner_pid'] <= 0 or not str(value.get('owner_start', '')).isdigit()
            or context.mode != 'startup' or value.get('previous_mapping') not in {BASE_MAPPING, LEGACY_MAPPING}
            or value.get('owned_mapping') != scoped_mapping(context)):
        raise RuntimeError('Invalid owned tmux Codex mapping; current option preserved')
    return value


def _tmux(*args):
    result = subprocess.run(['tmux', *args], capture_output=True, text=True, timeout=2)
    if result.returncode:
        raise RuntimeError('Cannot update owned tmux Codex mapping: ' + result.stderr.strip()[:200])
    return result.stdout.rstrip('\n')


def _owner_alive(record):
    return (record['operation_context']['boot_id'] == operations.boot_id()
            and _process_stamp(record['owner_pid']) == record['owner_start'])


def _descendant(pid, ancestor):
    for _ in range(64):
        if pid == ancestor:
            return True
        try:
            pid = int(Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return False
        if pid <= 1:
            return False
    return False


def begin(context, owner_pid, owner_start):
    """Own only the already-managed process mapping during this restore."""
    with operations.coordinator_lock(runtime_root() / 'tmux-codex-launch-scope.lock'):
        previous = _read_scope()
        if previous is None and context is None:
            return
        current = _tmux('show-option', '-gv', '@resurrect-processes')
        if previous is not None:
            if _owner_alive(previous):
                if (context is not None and previous['operation_context'] == context.to_dict()
                        and _descendant(owner_pid, previous['owner_pid'])
                        and current == previous['owned_mapping']):
                    return  # The wrapper and its nested pre-restore hook share ownership.
                raise RuntimeError('Another tmux restore still owns the Codex launch mapping')
            if current == previous['owned_mapping']:
                _tmux('set-option', '-g', '@resurrect-processes', previous['previous_mapping'])
                current = previous['previous_mapping']
            scope_path().unlink()
        if context is None:
            return
        context.check()
        if current not in {BASE_MAPPING, LEGACY_MAPPING}:
            raise RuntimeError('tmux Codex process mapping changed; current option preserved')
        owned = scoped_mapping(context)
        atomic_json(scope_path(), dict(schema_version=1, owner_pid=owner_pid,
            owner_start=owner_start, operation_context=context.to_dict(),
            previous_mapping=current, owned_mapping=owned))
        _tmux('set-option', '-g', '@resurrect-processes', owned)


def end(owner_pid=None):
    """Restore our exact option even after Abort; never touch a newer owner."""
    owner_pid = os.getppid() if owner_pid is None else owner_pid
    with operations.coordinator_lock(runtime_root() / 'tmux-codex-launch-scope.lock'):
        record = _read_scope()
        if record is None:
            return True
        inherited = operations.current()
        if (record['owner_pid'] != owner_pid or not _owner_alive(record)
                or (inherited is not None and record['operation_context'] != inherited.to_dict())):
            return False
        current = _tmux('show-option', '-gv', '@resurrect-processes')
        if current == record['owned_mapping']:
            _tmux('set-option', '-g', '@resurrect-processes', record['previous_mapping'])
        scope_path().unlink()
        return True
