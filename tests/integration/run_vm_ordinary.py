#!/usr/bin/python3
"""Observe an ordinary real cold-boot cycle without fixture or canonical writes."""
from __future__ import annotations

import argparse
import time
import uuid
from pathlib import Path

import run_vm_poweroff as p


def prepare(release):
    from workspace_state import storage
    from workspace_state.login_status import status_path
    status = p.read(status_path())
    if status.get('operation_state') != 'completed':
        raise RuntimeError('Successful startup is required')
    directory = p.ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    before_hash = p.digest(storage.load())
    expected, native = p.capture(), p.f.shell()
    identities = p.read(p.f.ROOT / 'conversation-identities.json')
    p.require_fixture_inventory(expected, native, identities)
    p.write(directory / 'expected.json', expected)
    p.write(directory / 'expected-native.json', native)
    p.write(directory / 'expected-chrome.json', p.f.observe_chrome())
    p.write(directory / 'before.json', {
        'run_id': directory.name, 'scenario': 'ordinary-continuity-no-fixture-mutation',
        'boot_id': p.boot(), 'login_generation': status['operation_context']['login_generation'],
        'prepared_at': time.time(), 'expected_digest': p.digest(expected),
        'installed_release': release, 'canonical_digest': before_hash,
        'native_inventory': p.native_inventory(native), 'synthetic_conversation_ids': identities,
        'graphical_launch': p.graphical_launch_evidence(), 'manual_save_called': False,
        'fixture_mutation': False,
    })
    if before_hash != p.digest(storage.load()):
        raise RuntimeError('Observer changed canonical state')
    p.write(p.ROOT / 'active.json', {'run_id': directory.name})
    return {'prepared': str(directory), 'observer_only': True}


def verify(release):
    from workspace_state import storage
    from workspace_state.login_status import status_path
    directory = p.run_directory()
    before, expected = p.read(directory / 'before.json'), p.read(directory / 'expected.json')
    status = p.read(status_path())
    if (before.get('scenario') != 'ordinary-continuity-no-fixture-mutation'
            or before.get('manual_save_called') is not False
            or before.get('fixture_mutation') is not False):
        raise RuntimeError('This is not an ordinary observer-only fixture')
    if p.boot() == before['boot_id'] or release != before['installed_release']:
        raise RuntimeError('Wrong boot or release')
    if p.digest(expected) != before['expected_digest']:
        raise RuntimeError('Independent expected evidence changed')
    if status.get('operation_state') != 'completed':
        raise RuntimeError('Startup did not complete')
    shutdown, boot = p.shutdown_evidence(directory, before), p.current_boot_evidence()
    actual, native = p.capture(), p.f.shell()
    chrome, canonical = p.f.observe_chrome(), storage.load()
    p.require_fixture_inventory(actual, native, before['synthetic_conversation_ids'])
    for name, value in [('live', actual), ('native', native), ('chrome', chrome),
                        ('status', status), ('canonical', canonical)]:
        p.write(directory / ('ordinary-' + name + '.json'), value)
    graphical = p.graphical_shutdown_evidence(directory, before, boot)
    p.write(directory / 'verified-graphical_shutdown.json', graphical)
    drain = p.graphical_drain_evidence(directory, before, boot, shutdown)
    p.write(directory / 'verified-graphical_drain.json', drain)
    checks = {
        'shutdown_checkpoint': p.differences(expected, p.read(directory / 'shutdown-canonical.json')),
        'live': p.differences(expected, actual), 'canonical': p.differences(expected, canonical),
        'native': p.native_placement_failures(p.read(directory / 'expected-native.json'), native),
        'browser_ids': p.native_browser_identity_failures(chrome),
        'inventory': [] if p.native_inventory(native) == before['native_inventory'] else ['Changed inventory'],
        'startup': p.startup_failures(status, before, expected,
            vm_evidence=p.vm_restore_evidence(), shutdown_operation_id=shutdown['operation_id']),
        'qmp': p.qmp_exit_evidence(directory, before, boot)['failures'],
        'user_manager': p.user_manager_shutdown_evidence(before, boot)['failures'],
        'graphical': graphical['failures'], 'drain': drain['failures'],
    }
    failures = {key: value for key, value in checks.items() if value}
    result = {'passed': not failures, 'failures': failures, 'manual_save_called': False,
        'fixture_mutation': False, 'release': release, 'shutdown': shutdown,
        'native_windows': len(native['windows']), 'limitations': p.LIMITS}
    p.write(directory / 'ordinary-result.json', result)
    if failures:
        raise RuntimeError('Ordinary continuity failed; see ordinary-result.json')
    return {'passed': True, 'run': str(directory), 'native_windows': len(native['windows'])}


def main():
    import json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('phase', choices=('prepare', 'verify'))
    args = parser.parse_args()
    release = p.guest_environment()
    print(json.dumps(globals()[args.phase](release)))


if __name__ == '__main__':
    main()
