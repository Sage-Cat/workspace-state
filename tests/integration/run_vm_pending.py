#!/usr/bin/python3
"""Prove that slow Chrome pages never delay placement of native windows.

This guest-only observer neither restores applications nor changes coordinator
state. The loopback HTTP fixture holds every page response until the browsers
stage has verified its placements; only then is the gate opened. A short HTTP
sleep cannot prove this, because the gate stays shut until the evidence exists.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import run_vm_poweroff as p

MINIMUM_ITEMS = 3


def placement_problems(results):
    """Problems with browsers-stage results captured while the pages were still gated."""
    problems = []
    if len(results) < MINIMUM_ITEMS:
        problems.append('Fewer than three Chrome windows were placed before the pages were released')
    for item in results:
        if item.get('created') is True or item.get('reused') is not True:
            problems.append(f"{item.get('item_id')}: placement did not use Chrome's own native window")
        for phase in ('identity', 'content', 'placement'):
            if item.get(phase, {}).get('state') != 'verified':
                problems.append(f"{item.get('item_id')}: {phase} was not verified while pages were still loading")
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
                     'released_when': 'browsers stage finished', 'fallback_seconds': 90}))


def observe(_args):
    from workspace_state.login_status import status_path
    gate = p.read(p.f.ROOT / 'http-delay.json').get('pending_gate')
    if not gate or gate['armed_boot_id'] == p.boot():
        return
    directory = p.f.ROOT / 'pending-returns' / p.boot()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    context, released = None, False
    deadline = time.monotonic() + 200
    while time.monotonic() < deadline:
        try:
            status = p.read(status_path())
            owner = status.get('operation_context', {})
            if owner.get('boot_id') != p.boot() or owner.get('mode') != 'startup':
                time.sleep(.1)
                continue
            if context is not None and owner != context:
                raise RuntimeError('Startup owner changed during the slow-page proof')
            context = owner
            stage = next((item for item in status.get('stages', []) if item.get('id') == 'browsers'), {})
            results = stage.get('provider_results') or []
            if results and stage.get('state') in {'ready', 'degraded', 'failed'} and not released:
                # The pages are still gated: this is the evidence that placement did not wait for them.
                receipt = {'boot_id': p.boot(), 'operation_context': context, 'provider_results': results,
                           'released_at': time.time(), 'condition': 'browsers-stage-finished-while-pages-gated',
                           'product_state_mutated': False}
                p.write(directory / 'release.json', receipt)
                p.write(p.f.ROOT / 'http-pending-release.json', receipt)
                released = True
            if (released and status.get('operation_state') in {'failed', 'completed'}
                    and all(item.get('state') in {'ready', 'skipped', 'failed', 'degraded'}
                            for item in status.get('stages', []))):
                p.write(directory / 'final-status.json', status)
                return
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        time.sleep(.1)
    raise RuntimeError('Slow-page observer did not reach measured startup completion')


def verify(_args):
    directory = p.f.ROOT / 'pending-returns' / p.boot()
    release = p.read(directory / 'release.json')
    if release.get('boot_id') != p.boot():
        raise RuntimeError('The page gate was not released by this boot')
    failures = placement_problems(release.get('provider_results', []))
    events = []
    for line in p.f.run('journalctl', '--user', '-b', '-u', 'wsctl-scale-pages.service',
                       '-o', 'cat', '--no-pager').splitlines():
        try:
            value = json.loads(line)
            if value.get('event') == 'synthetic-page':
                events.append(value)
        except json.JSONDecodeError:
            pass
    if not any(event.get('gate_released') is True and event.get('gated_seconds', 0) >= 10 for event in events):
        failures.append('No HTTP request stayed gated beyond the placement of the windows')
    p.write(directory / 'http-events.json', events)
    result = {'passed': not failures, 'outcome': 'passed' if not failures else 'failed', 'failures': failures,
              'boot_id': p.boot(), 'operation_context': release['operation_context'],
              'items': [item.get('item_id') for item in release.get('provider_results', [])],
              'product_state_mutated': False, 'manual_placement': False, 'manual_save': False}
    p.write(directory / 'result.json', result)
    print(json.dumps(result))
    if failures:
        raise RuntimeError('Slow-page placement proof failed')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('phase', choices=('configure', 'observe', 'verify'))
    args = parser.parse_args()
    p.guest_environment()
    globals()[args.phase](args)


if __name__ == '__main__':
    main()
