#!/usr/bin/python3
"""Prove native pending returns before releasing synthetic next-boot pages.

This guest-only observer neither restores applications nor changes coordinator
state. It opens a loopback HTTP fixture gate only after three production restore
calls actually return pending. A short HTTP sleep alone cannot prove this path.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import time

import run_vm_poweroff as p

PENDING = re.compile(r'Chrome (Default/window-\d+): exact tab URLs are still loading')


def pending_returns(log):
    return sorted(set(PENDING.findall(log)))


def phase_problems(initial, final, *, expect_failure):
    first = initial.get('provider_results', [])
    waiting = [item for item in first if item.get('content', {}).get('state') == 'waiting']
    problems = []
    if len(waiting) < 3:
        problems.append('Fewer than three native pending results were persisted before completion')
    for item in waiting:
        if (item.get('identity', {}).get('state') != 'verified'
                or item.get('created') is not False or item.get('reused') is not True):
            problems.append('Initial pending result did not identify the unchanged original window')
        phase = item.get('placement', {})
        if expect_failure:
            if phase.get('state') != 'failed' or phase.get('request_id'):
                problems.append('Unfixed pending result did not reproduce unrequested failed placement')
        elif phase.get('state') != 'waiting' or not phase.get('request_id'):
            problems.append('Candidate pending result lacks a scoped placement continuation')
    final_items = {item['item_id']: item for item in final.get('provider_results', [])}
    for item in waiting:
        after = final_items.get(item['item_id'], {})
        if after.get('content', {}).get('state') != 'verified':
            problems.append('Pending native content did not become verified')
        expected = 'failed' if expect_failure else 'verified'
        if after.get('placement', {}).get('state') != expected:
            problems.append('Late native placement did not reach expected ' + expected)
        if after.get('created') is not False or after.get('reused') is not True:
            problems.append('Pending continuation did not preserve the original window')
    return problems


def configure(_args):
    root = p.f.ROOT
    p.f.configure_pages(0)
    p.write(root / 'http-delay.json', {'seconds': 0, 'pending_gate': {
        'armed_boot_id': p.boot(), 'max_seconds': 90}})
    source = Path(__file__).resolve()
    unit = Path.home() / '.config/systemd/user/wsctl-scale-pending-observer.service'
    if unit.exists() and 'Synthetic pending-return observer' not in unit.read_text():
        raise RuntimeError('Refusing to overwrite non-fixture observer')
    unit.write_text('[Unit]\nDescription=Synthetic pending-return observer\n'
        '[Service]\nType=exec\nExecStart=/usr/bin/python3 ' + str(source) + ' --disposable-guest observe\n'
        'RuntimeMaxSec=210\n[Install]\nWantedBy=default.target\n')
    p.f.run('systemctl', '--user', 'daemon-reload')
    p.f.run('systemctl', '--user', 'enable', unit.name)
    print(json.dumps({'armed_boot_id': p.boot(), 'next_boot_only': True,
                     'release_after_pending_returns': 3, 'fallback_seconds': 90}))


def observe(_args):
    from workspace_state.login_status import status_path
    gate = p.read(p.f.ROOT / 'http-delay.json').get('pending_gate')
    if not gate or gate['armed_boot_id'] == p.boot():
        return
    directory = p.f.ROOT / 'pending-returns' / p.boot()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    seen, context, released = None, None, False
    deadline = time.monotonic() + 200
    while time.monotonic() < deadline:
        try:
            status = p.read(status_path())
            owner = status.get('operation_context', {})
            if owner.get('boot_id') != p.boot() or owner.get('mode') != 'startup':
                time.sleep(.1)
                continue
            if context is not None and owner != context:
                raise RuntimeError('Startup owner changed during pending-return proof')
            context = owner
            marker_root = status_path().parent / ('startup-' + p.boot() + '-' + owner['login_generation'])
            # Python worker stdout may be block-buffered until exit. The HUD
            # event log is written at each real return and is scoped by the
            # current boot's runtime root; observe it rather than configuring
            # product buffering or waiting for the HTTP fallback deadline.
            log = (status_path().parent / 'login-hud.log').read_text()
            returned = pending_returns(log)
            if len(returned) >= 3 and not released:
                receipt = {'boot_id': p.boot(), 'operation_context': context,
                    'pending_return_items': returned, 'released_at': time.time(),
                    'condition': 'three-production-native-pending-returns', 'product_state_mutated': False}
                p.write(directory / 'release.json', receipt)
                p.write(p.f.ROOT / 'http-pending-release.json', receipt)
                released = True
            path = marker_root / 'browsers.done'
            if path.exists():
                marker = p.read(path)
                if marker.get('operation_context') != context:
                    raise RuntimeError('Browser evidence belongs to another startup')
                value = p.digest(marker)
                if value != seen:
                    waiting = sum(item.get('content', {}).get('state') == 'waiting'
                                  for item in marker.get('provider_results', []))
                    if waiting >= 3 and not (directory / 'initial.json').exists():
                        p.write(directory / 'initial.json', marker)
                    p.write(directory / 'latest.json', marker)
                    seen = value
            if (released and status.get('operation_state') in {'failed', 'completed'}
                    and all(stage.get('state') in {'ready', 'skipped', 'failed', 'degraded'}
                            for stage in status.get('stages', []))):
                p.write(directory / 'final-status.json', status)
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(.1)
    raise RuntimeError('Pending-return observer did not reach measured startup completion')


def verify(args):
    directory = p.f.ROOT / 'pending-returns' / p.boot()
    release, initial, final = (p.read(directory / name) for name in ('release.json', 'initial.json', 'latest.json'))
    if release.get('boot_id') != p.boot() or len(release.get('pending_return_items', [])) < 3:
        raise RuntimeError('Gate did not release after three native pending returns')
    if any(marker.get('operation_context') != release['operation_context'] for marker in (initial, final)):
        raise RuntimeError('Pending evidence belongs to another startup owner')
    failures = phase_problems(initial, final, expect_failure=args.expect_failure)
    events = []
    for line in p.f.run('journalctl', '--user', '-b', '-u', 'wsctl-scale-pages.service',
                       '-o', 'cat', '--no-pager').splitlines():
        try:
            value = json.loads(line)
            if value.get('event') == 'synthetic-page':
                events.append(value)
        except json.JSONDecodeError:
            pass
    if sum(event.get('gate_released') is True and event.get('gated_seconds', 0) >= 10
           for event in events) < 3:
        failures.append('Actual HTTP requests did not remain gated beyond native verification')
    p.write(directory / 'http-events.json', events)
    result = {'passed': not failures and not args.expect_failure, 'harness_success': not failures,
        'outcome': 'expected-regression' if not failures and args.expect_failure else 'passed' if not failures else 'failed',
        'expected_failure': args.expect_failure, 'failures': failures, 'boot_id': p.boot(),
        'operation_context': release['operation_context'], 'pending_items': release['pending_return_items'],
        'product_state_mutated': False, 'manual_placement': False, 'manual_save': False}
    p.write(directory / 'result.json', result)
    print(json.dumps(result))
    if failures:
        raise RuntimeError('Native pending→placement proof failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--expect-failure', action='store_true')
    parser.add_argument('phase', choices=('configure', 'observe', 'verify'))
    args = parser.parse_args()
    p.guest_environment()
    globals()[args.phase](args)


if __name__ == '__main__':
    main()
