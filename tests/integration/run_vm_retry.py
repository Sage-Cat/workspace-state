#!/usr/bin/python3
"""Verify a real partial application drain, HUD cancel, and checkpoint-preserving retry.

This observer never opens, closes, moves, saves, or reconstructs application state.
The host controller supplies the bounded native portal stop-job latency and clicks
the actual HUD Cancel button. A later genuine GNOME power-off must exit QEMU.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import time

import run_vm_ordinary as ordinary
import run_vm_poweroff as p


def owned_ledger(status, ledger, *, terminal):
    context = status.get('operation_context')
    if (not isinstance(context, dict) or context.get('mode') != 'shutdown'
            or context.get('operation_id') != status.get('operation_id')
            or ledger.get('schema_version') != 1
            or ledger.get('operation_context') != context
            or ledger.get('settled') is not True
            or ledger.get('status') != terminal
            or not isinstance(ledger.get('requests'), dict)
            or any(value == 'issuing' for value in ledger['requests'].values())):
        raise RuntimeError('Drain ledger is not settled under the exact measured owner')
    return context


def checkpoint_bytes(bundle, key):
    record = bundle[key]
    raw = base64.b64decode(record['bytes'], validate=True)
    if hashlib.sha256(raw).hexdigest() != record.get('digest'):
        raise RuntimeError('Sealed checkpoint evidence has changed')
    return raw


def settled(release):
    from workspace_state import storage
    from workspace_state.login_status import runtime_root, status_path
    directory = p.run_directory()
    before = p.read(directory / 'before.json')
    status = p.read(status_path())
    if (before.get('scenario') != 'ordinary-continuity-no-fixture-mutation'
            or p.boot() != before['boot_id'] or status.get('operation_state') != 'cancelled'
            or status.get('cancelled') is not True or status.get('commit_authorized') is True
            or status.get('recovery_pending') is True):
        raise RuntimeError('A genuine completed same-boot HUD cancellation is required')
    root = runtime_root()
    first = p.read(root / ('shutdown-graphical-drain-' + status['operation_id'] + '.json'))
    context = owned_ledger(status, first, terminal='failed')
    if context.get('boot_id') != before['boot_id']:
        raise RuntimeError('Cancelled drain belongs to another boot')
    pointer = p.read(root / 'shutdown-retry-protection.json')
    descriptor = pointer.get('checkpoint_bundle', {})
    if pointer.get('operation_context') != context:
        raise RuntimeError('Checkpoint protection belongs to another drain')
    from workspace_state.util import data_home
    bundle_path = data_home() / 'shutdown-checkpoints' / descriptor['bundle_name']
    raw = bundle_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor.get('bundle_digest'):
        raise RuntimeError('Protection does not point to the sealed checkpoint')
    bundle = json.loads(raw)
    if bundle.get('operation_context') != context:
        raise RuntimeError('Sealed checkpoint belongs to another operation')
    canonical = checkpoint_bytes(bundle, 'canonical')
    tmux = checkpoint_bytes(bundle, 'tmux')
    if Path(bundle['canonical']['path']).read_bytes() != canonical:
        raise RuntimeError('Cancelled drain replaced its verified canonical checkpoint')
    if Path(bundle['tmux']['path']).read_bytes() != tmux:
        raise RuntimeError('Cancelled drain replaced its verified terminal checkpoint')
    if p.differences(p.read(directory / 'expected.json'), storage.load()):
        raise RuntimeError('Cancelled drain checkpoint lost prepared content or placement')
    native = p.f.shell()
    original = p.read(directory / 'expected-native.json')
    old_ids = {window['id'] for window in original['windows']}
    current_ids = {window['id'] for window in native['windows']}
    if not current_ids < old_ids:
        raise RuntimeError('Trial did not leave a genuine partial original desktop')
    jobs_raw = p.f.run('systemctl', '--user', 'list-jobs', '--output=json', '--no-pager')
    jobs = json.loads(jobs_raw) if jobs_raw else []
    if jobs:
        raise RuntimeError('Cancellation has not joined all native stop jobs')
    output = p.f.run(str(Path(release) / 'bin/wsctl-continuum-save'))
    if (Path(bundle['canonical']['path']).read_bytes() != canonical
            or Path(bundle['tmux']['path']).read_bytes() != tmux
            or p.read(root / 'shutdown-retry-protection.json') != pointer):
        raise RuntimeError('Real terminal autosave overwrote protected recovery state')
    proof = {'release': release, 'captured_at': time.time(), 'status': status,
             'ledger': first, 'protection': pointer, 'bundle': bundle,
             'native': native, 'native_windows_before': len(old_ids),
             'native_windows_after': len(current_ids), 'jobs': jobs,
             'autosave_wrapper_called': True, 'autosave_exit_status': 0,
             'autosave_output': output, 'checkpoint_unchanged': True,
             'tmux_unchanged': True, 'no_reconstruction': True}
    p.write(directory / 'retry-cancel-settled.json', proof)
    return {'settled': True, 'run': str(directory), 'operation_id': status['operation_id'],
            'native_windows': len(current_ids), 'real_autosave_retained_checkpoint': True}


def verify_chain(directory, before, shutdown, graphical):
    from workspace_state.util import data_home
    previous = p.read(directory / 'retry-cancel-settled.json')
    first_context = owned_ledger(previous['status'], previous['ledger'], terminal='failed')
    status = shutdown['latest_status']
    filename = 'shutdown-graphical-drain-' + status['operation_id'] + '.json'
    final = p.read(data_home() / filename)
    p.write(directory / filename, final)
    final_context = owned_ledger(status, final, terminal='succeeded')
    if (first_context['operation_id'] == final_context['operation_id']
            or any(first_context.get(key) != final_context.get(key) for key in ('boot_id', 'login_generation'))
            or first_context['boot_id'] != before['boot_id']
            or previous.get('no_reconstruction') is not True
            or previous.get('autosave_wrapper_called') is not True
            or previous.get('checkpoint_unchanged') is not True or previous.get('tmux_unchanged') is not True
            or not any(item.get('operation_id') == first_context['operation_id']
                       for item in shutdown['cancelled_attempts'])):
        raise RuntimeError('Retry has no independent completed cancellation chain')
    worker = p.read(directory / 'shutdown-worker-complete.json')
    prepared = p.read(directory / 'shutdown-prepared.json')
    if (not p.receipt_matches(status, worker) or not p.receipt_matches(status, prepared)
            or prepared.get('invocation_id') != worker.get('invocation_id')
            or prepared.get('graphical_drain_completed') is not True
            or prepared.get('graphical_drain_receipt') != filename):
        raise RuntimeError('Retry handoff was not authorized by its exact completed new drain')
    descriptor = worker['checkpoint_bundle']
    raw = (data_home() / 'shutdown-checkpoints' / descriptor['bundle_name']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != descriptor.get('bundle_digest'):
        raise RuntimeError('Retry worker bundle digest differs')
    bundle = json.loads(raw)
    if (bundle.get('operation_context') != final_context
            or bundle.get('invocation_id') != worker.get('invocation_id')
            or bundle.get('inherited') != previous['protection']['checkpoint_bundle']
            or checkpoint_bytes(bundle, 'canonical') != checkpoint_bytes(previous['bundle'], 'canonical')
            or checkpoint_bytes(bundle, 'tmux') != checkpoint_bytes(previous['bundle'], 'tmux')):
        raise RuntimeError('New worker recaptured partial apps instead of carrying the sealed checkpoint')
    if not (float(previous['ledger']['started_at']) <= graphical['native_exit_at']
            <= float(previous['ledger']['finished_at']) <= float(final['started_at'])
            <= float(final['finished_at']) <= float(prepared['created_at'])
            < graphical['shell_shutdown_at']):
        raise RuntimeError('Actual Chrome exit and native retry drain chronology differ')
    if not (0 < float(final['finished_monotonic']) <= float(final['deadline']) <= float(final_context['deadline'])):
        raise RuntimeError('New native drain exceeded its scoped deadline')
    chrome = before['graphical_launch']['chrome']
    if sum(item.get('unit') == chrome['unit'] and item.get('invocation_id') == chrome['invocation_id']
           for item in previous['ledger']['units']) != 1:
        raise RuntimeError('Initial cancelled drain omitted the actual prepared Chrome invocation')
    stages = {stage['id']: stage for stage in status['stages']}
    for name in ('tmux-save', 'workspace-save'):
        if stages[name].get('state') != 'ready' or 'Reusing verified checkpoint' not in stages[name].get('message', ''):
            raise RuntimeError('Retry did not report reuse of the verified pre-drain checkpoint')
    proof = {'verified': True, 'first_operation': first_context, 'retry_operation': final_context,
             'cancelled_ledger': previous['ledger'], 'retry_ledger': final,
             'worker': worker, 'prepared': prepared, 'inherited_bundle': bundle,
             'autosave_retained_checkpoint': True, 'manual_reconstruction': False}
    p.write(directory / 'verified-retry-chain.json', proof)
    return proof


def verify(release):
    from workspace_state import storage
    from workspace_state.login_status import status_path
    directory = p.run_directory()
    before, expected = p.read(directory / 'before.json'), p.read(directory / 'expected.json')
    if p.boot() == before['boot_id'] or p.digest(expected) != before['expected_digest']:
        raise RuntimeError('Real cold boot with unchanged independent expected state is required')
    ordinary.require_boot_release(before, release)
    status = p.read(status_path())
    if status.get('operation_state') != 'completed':
        raise RuntimeError('Startup did not complete')
    shutdown, current = p.shutdown_evidence(directory, before), p.current_boot_evidence()
    actual, native = p.capture(), p.f.shell()
    chrome, canonical = p.f.observe_chrome(), storage.load()
    p.require_fixture_inventory(actual, native, before['synthetic_conversation_ids'])
    for name, value in [('live', actual), ('native', native), ('chrome', chrome),
                        ('status', status), ('canonical', canonical)]:
        p.write(directory / ('retry-' + name + '.json'), value)
    graphical = p.graphical_shutdown_evidence(directory, before, current)
    p.write(directory / 'verified-graphical_shutdown.json', graphical)
    chain = verify_chain(directory, before, shutdown, graphical)
    checks = {'shutdown_checkpoint': p.differences(expected, p.read(directory / 'shutdown-canonical.json')),
              'live': p.differences(expected, actual), 'canonical': p.differences(expected, canonical),
              'native': p.native_placement_failures(p.read(directory / 'expected-native.json'), native),
              'browser_ids': p.native_browser_identity_failures(chrome),
              'inventory': [] if p.native_inventory(native) == before['native_inventory'] else ['Changed inventory'],
              'startup': p.startup_failures(status, before, expected, vm_evidence=p.vm_restore_evidence(),
                                            shutdown_operation_id=shutdown['operation_id']),
              'qmp': p.qmp_exit_evidence(directory, before, current)['failures'],
              'user_manager': p.user_manager_shutdown_evidence(before, current)['failures'],
              'graphical': graphical['failures']}
    failures = {key: value for key, value in checks.items() if value}
    result = {'passed': not failures, 'failures': failures, 'release': release,
              'run': str(directory), 'native_windows': len(native['windows']),
              'shutdown': shutdown, 'retry_chain': chain, 'manual_reconstruction': False,
              'limitations': p.LIMITS}
    p.write(directory / 'retry-result.json', result)
    if failures:
        raise RuntimeError('Real cancel/retry continuity failed; see retry-result.json')
    return {'passed': True, 'run': str(directory), 'native_windows': len(native['windows'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('phase', choices=('prepare', 'settled', 'verify'))
    args = parser.parse_args()
    release = p.guest_environment()
    result = ordinary.prepare(release) if args.phase == 'prepare' else (
        settled(release) if args.phase == 'settled' else verify(release))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
