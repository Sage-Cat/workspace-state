#!/usr/bin/python3
"""Measure slow real shutdown captures in the disposable retained-upgrade guest.

The fixture throttles only the real checkpoint worker, not application processes.
It never changes producer code, timestamps, checkpoint payloads or startup results.
GNOME Power Off and the subsequent cold boot remain external VM-only actions.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess

import run_vm_poweroff as p
import run_vm_upgrade as upgrade

DROPIN = Path.home() / '.config/systemd/user/wsctl-shutdown-finalize@.service.d/90-wsctl-fixture-capture-cpu.conf'
DROPIN_TEXT = '# Disposable wsctl-validation capture-latency fixture only\n[Service]\nCPUQuota=30%\n'


def configure(remove=False):
    if DROPIN.exists() and DROPIN.read_text() != DROPIN_TEXT:
        raise RuntimeError('Refusing to replace an unrelated worker configuration')
    if remove:
        DROPIN.unlink(missing_ok=True)
    else:
        DROPIN.parent.mkdir(parents=True, exist_ok=True)
        DROPIN.write_text(DROPIN_TEXT)
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    return {'worker_cpu_quota_percent': None if remove else 30,
            'application_processes_throttled': False, 'producer_code_modified': False}


def measured_binding(checkpoint):
    context = checkpoint['capture_context']
    observation = checkpoint['browsers']['google_chrome']['latest_observation']
    start = datetime.fromisoformat(context['captured_at'])
    observed = datetime.fromisoformat(observation['captured_at'])
    return {'schema_version': observation['schema_version'],
            'capture_start': context['captured_at'], 'observation_time': observation['captured_at'],
            'snapshot_created_at': checkpoint['created_at'],
            'observation_minus_start_seconds': (observed - start).total_seconds()}


def producer_publication(current):
    """Archive original producer evidence separately from later autosaves.

    This observer grants no restoration authority and never replaces canonical
    state. History bytes must have their exact original publication hash.
    """
    if current.get('capture_context'):
        return current, 'observed-canonical'
    history = Path.home() / '.local/share/workspace-state/recovery/history'
    matches = []
    for path in history.glob('*.json'):
        candidate = p.read(path)
        fingerprint = hashlib.sha256(json.dumps(candidate, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:20]
        if (path.name.endswith('-' + fingerprint + '.json') and candidate.get('capture_context')
                and candidate.get('browsers') == current.get('browsers')
                and candidate.get('category_provenance', {}).get('browsers') == current.get('category_provenance', {}).get('browsers')):
            matches.append((candidate, path.name))
    if len(matches) != 1:
        raise RuntimeError('Missing unique original shutdown publication for latency measurement')
    return matches[0]


def verify(args):
    if args.expect_failure:
        return verify_negative(args)
    upgrade.verify(args)
    directory = p.run_directory()
    checkpoint = p.read(directory / 'shutdown-canonical.json')
    publication, source = producer_publication(checkpoint)
    p.write(directory / 'original-producer-publication.json', publication)
    measurement = {**measured_binding(publication), 'producer_evidence_source': source,
                   'later_autosave_dropped_context': checkpoint.get('capture_context') is None}
    if measurement['observation_minus_start_seconds'] < 1:
        raise RuntimeError('Capture was too fast: the original one-second rejection was not exercised')
    status = p.read(directory / 'upgrade-status.json')
    if args.expect_failure:
        stage = next(s for s in status['stages'] if s['id'] == 'browsers')
        if not any(text in stage.get('message', '') for text in (
                'not bound to its complete capture', 'lacks complete retained-capture evidence')):
            raise RuntimeError('Unfixed startup failed for an unrelated reason')
        from workspace_state.browser_reconciliation import ReconciliationRequired, retained_observation
        try:
            retained_observation(publication)
        except ReconciliationRequired as error:
            if 'not bound to its complete capture' not in str(error):
                raise RuntimeError('Original full producer failed for an unrelated reason') from error
        else:
            raise RuntimeError('Unfixed producer binding did not reproduce the original rejection')
    else:
        from workspace_state.browser_reconciliation import retained_observation
        if retained_observation(checkpoint) is None:
            raise RuntimeError('Candidate did not validate the real retained capture')
    result = {'harness_success': True, 'expected_failure_reproduced': args.expect_failure,
              'passed': not args.expect_failure, 'measurement': measurement,
              'producer_code_modified': False, 'manual_adoption': False}
    p.write(directory / 'capture-binding-result.json', result)
    return result


def verify_negative(args):
    """A pre-launch rejection cannot require a running browser to prove failure."""
    from workspace_state import cli
    from workspace_state.login_status import status_path
    from workspace_state.browser_reconciliation import ReconciliationRequired, retained_observation
    directory = p.run_directory()
    before = p.read(directory / 'before.json')
    status = p.read(status_path())
    if p.boot() == before['boot_id'] or status.get('operation_state') != 'failed':
        raise RuntimeError('Unfixed cold boot with settled failed startup is required')
    stage = next(s for s in status['stages'] if s['id'] == 'browsers')
    if stage['state'] != 'failed' or not any(text in stage.get('message', '') for text in (
            'not bound to its complete capture', 'lacks complete retained-capture evidence')):
        raise RuntimeError('Unfixed startup failed for an unrelated reason')
    checkpoint = p.read(directory / 'shutdown-canonical.json')
    publication, source = producer_publication(checkpoint)
    measurement = {**measured_binding(publication), 'producer_evidence_source': source,
                   'later_autosave_dropped_context': checkpoint.get('capture_context') is None}
    if measurement['observation_minus_start_seconds'] < 1:
        raise RuntimeError('Original producer capture was too fast to exercise the defect')
    try:
        retained_observation(publication)
    except ReconciliationRequired as error:
        if 'not bound to its complete capture' not in str(error):
            raise RuntimeError('Original full producer failed for an unrelated reason') from error
    else:
        raise RuntimeError('Original full producer was not incorrectly rejected')
    shutdown = p.shutdown_evidence(directory, before)
    boot = p.current_boot_evidence()
    checks = {'retained': upgrade.retained_failures(p.read(directory / 'original.json'),
               p.read(directory / 'expected.json'), checkpoint),
              'qmp': p.qmp_exit_evidence(directory, before, boot)['failures'],
              'user_manager': p.user_manager_shutdown_evidence(before, boot)['failures']}
    for name, observer in [('graphical_shutdown', p.graphical_shutdown_evidence),
                           ('graphical_drain', lambda d, b, bt: p.graphical_drain_evidence(d, b, bt, shutdown))]:
        proof = observer(directory, before, boot)
        p.write(directory / ('verified-' + name + '.json'), proof)
        checks[name] = proof['failures']
    native = p.f.shell()
    windows = [w for w in native['windows'] if 'google-chrome' in (w.get('app_ids') or [])]
    checks['prelaunch_refusal'] = [] if not windows else ['Unexpected browser windows after pre-launch rejection']
    for name, value in [('original-producer-publication', publication), ('upgrade-status', status),
                        ('upgrade-native', native), ('negative-capture', cli._capture_all())]:
        p.write(directory / (name + '.json'), value)
    failures = {key: value for key, value in checks.items() if value}
    result = {'harness_success': not failures, 'expected_failure_reproduced': not failures,
              'passed': False, 'failures': failures, 'measurement': measurement,
              'browser_launch_prevented': not windows, 'manual_adoption': False,
              'limits': ['Browser content must be verified when its original synthetic profile is reopened; no live browser existed after this failed startup']}
    p.write(directory / 'capture-binding-result.json', result)
    if failures:
        raise RuntimeError('Negative capture-binding proof failed; see capture-binding-result.json')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--expect-failure', action='store_true')
    parser.add_argument('--same-release-retry', action='store_true')
    parser.add_argument('--omit-original-ungrouped-window', action='store_true')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('phase', choices=('configure', 'remove', 'prepare', 'watch', 'verify'))
    args = parser.parse_args()
    args.installed_release = p.guest_environment()
    if args.phase in ('configure', 'remove'):
        result = configure(remove=args.phase == 'remove')
    elif args.phase == 'verify':
        result = verify(args)
    else:
        result = getattr(upgrade, args.phase)(args)
    print(json.dumps(result))


if __name__ == '__main__':
    main()
