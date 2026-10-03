#!/usr/bin/python3
"""Observe a genuine GNOME power-off cycle in the disposable scale guest.

This helper never requests power-off, closes applications, or fabricates HUD
receipts. An external operator confirms GNOME's real dialog after ``watch`` is
running, then boots the same VM and invokes ``verify``.
"""
from __future__ import annotations

import argparse
from collections import Counter
import copy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
import uuid

import run_vm_scale as f

ROOT = f.ROOT / 'poweroff'
RECEIPTS = ('shutdown-hud-rendered.json', 'shutdown-commit.json',
            'shutdown-worker-complete.json', 'shutdown-prepared.json')
LIMITS = ['23 conversation workers are synthetic; real Codex authentication is not exercised',
          'Cloud mounts and the command VM display profile are synthetic',
          'Host QMP exit and previous-boot journals must be archived separately']


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read(path):
    return json.loads(path.read_text())


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def boot():
    return Path('/proc/sys/kernel/random/boot_id').read_text().strip()


def run_directory():
    identifier = read(ROOT / 'active.json')['run_id']
    if not isinstance(identifier, str) or not identifier or any(c not in '0123456789abcdef-' for c in identifier):
        raise RuntimeError('Invalid poweroff run identity')
    return ROOT / identifier


def guest_environment():
    if socket.gethostname() != 'wsctl-validation' or f.run('systemd-detect-virt') != 'kvm':
        raise RuntimeError('Refusing outside wsctl-validation KVM guest')
    consent = read(f.ROOT / 'consent.json')
    if consent.get('hostname') != 'wsctl-validation' or consent.get('synthetic_only') is not True:
        raise RuntimeError('Disposable fixture consent is missing')
    for line in f.run('systemctl', '--user', 'show-environment').splitlines():
        key, separator, value = line.partition('=')
        if separator and key in {'DISPLAY', 'XAUTHORITY'}:
            os.environ[key] = value
    os.environ.update(PATH=str(Path.home() / '.local/bin') + ':' + os.environ['PATH'],
                      XDG_RUNTIME_DIR=f'/run/user/{os.getuid()}',
                      DBUS_SESSION_BUS_ADDRESS=f'unix:path=/run/user/{os.getuid()}/bus',
                      WAYLAND_DISPLAY='wayland-0', XDG_SESSION_TYPE='wayland')
    dirs = os.environ.get('XDG_DATA_DIRS', '/usr/local/share:/usr/share').split(':')
    if '/var/lib/snapd/desktop' not in dirs:
        dirs.append('/var/lib/snapd/desktop')
    os.environ['XDG_DATA_DIRS'] = ':'.join(dirs)
    release = (Path.home() / '.local/bin/wsctl').resolve().parent.parent
    if not (release / 'src/workspace_state').is_dir():
        raise RuntimeError('Installed workspace-state release is missing')
    sys.path.insert(0, str(release / 'src'))
    return str(release)


def native_inventory(shell):
    counts = Counter((str(w.get('wm_class')), tuple(sorted(w.get('app_ids') or [])))
                     for w in shell['windows'])
    return sorted([[name, list(ids), count] for (name, ids), count in counts.items()])


def require_fixture_inventory(snapshot, shell, conversation_ids):
    """Never promote an already duplicated or unmanaged desktop to expected state."""
    from workspace_state.social_apps import APPS, configured_apps, matching_windows
    apps = configured_apps()
    app_ids = {app.id for app in apps}
    if len(app_ids) != len(apps) or not {app.id for app in APPS}.issubset(app_ids):
        raise RuntimeError('Configured fixture applications are incomplete or ambiguous')
    records = snapshot.get('social_apps', {})
    if set(records) != app_ids or any(record.get('mode') != 'windowed' or record.get('running') is not True
                                    or len(record.get('windows', [])) != 1 for record in records.values()):
        raise RuntimeError('Every configured fixture application must have exactly one captured visible window')
    panes = [p for s in snapshot.get('sessions', []) for w in s['windows'] for p in w['panes']]
    identities = [(p.get('codex') or {}).get('session_id') for p in panes if p.get('codex')]
    windows = [window for profile in snapshot.get('browsers', {}).get('google_chrome', {}).get('profiles', [])
               for window in profile.get('windows', [])]
    captured = {'terminals': len(snapshot.get('terminals', [])), 'sessions': len(snapshot.get('sessions', [])),
                'conversations': len(identities), 'chrome_windows': len(windows),
                'chrome_tabs': sum(len(w.get('tabs', [])) for w in windows),
                'chrome_groups': sum(len(w.get('groups', [])) for w in windows),
                'nemo_windows': len(snapshot.get('file_manager', {}).get('windows', [])),
                'code_windows': len(snapshot.get('vscode', {}).get('windows', []))}
    required = {'terminals': 6, 'sessions': 10, 'conversations': 23, 'chrome_windows': 7,
                'chrome_tabs': 42, 'chrome_groups': 3, 'nemo_windows': 4, 'code_windows': 1}
    if captured != required:
        raise RuntimeError('Fixture capture counts differ: ' + repr({'expected': required, 'actual': captured}))
    if len(set(identities)) != 23 or set(identities) != set(conversation_ids):
        raise RuntimeError('Fixture requires 23 exact, unique synthetic conversation identities')
    native = shell['windows']
    if len({window['id'] for window in native}) != len(native):
        raise RuntimeError('Native window inventory has duplicate identities')
    claims = Counter()
    counts = {}
    categories = {
        'Alacritty': (6, lambda w: str(w.get('wm_class')).lower() == 'alacritty'),
        'Chrome': (7, lambda w: 'google-chrome' in str(w.get('app_ids'))),
        'Nemo': (4, lambda w: str(w.get('wm_class')).lower() == 'nemo'),
        'Code': (1, lambda w: str(w.get('wm_class')).lower() == 'com.microsoft.vscode'),
        'viewer': (1, lambda w: str(w.get('wm_class')).lower() == 'remote-viewer'),
    }
    for name, (count, predicate) in categories.items():
        selected = [window for window in native if predicate(window)]
        counts[name] = len(selected)
        if len(selected) != count:
            raise RuntimeError(f'Fixture needs exactly {count} native {name} windows; observed {len(selected)}')
        claims.update(window['id'] for window in selected)
    for app in apps:
        selected = matching_windows(app, shell)
        counts[app.id] = len(selected)
        if len(selected) != 1:
            raise RuntimeError(f'Fixture needs exactly one native {app.id} window; observed {len(selected)}')
        claims.update(window['id'] for window in selected)
    invalid = [window for window in native if claims[window['id']] != 1]
    total = 23 + len(apps) - len(APPS)
    if invalid or len(native) != total:
        raise RuntimeError('Unmanaged or ambiguously owned native fixture windows: ' + repr(invalid))
    return {'captured': captured, 'native': counts, 'configured_apps': sorted(app_ids),
            'native_windows': len(native), 'required_native_windows': total}


def native_placement_failures(expected, actual):
    from workspace_state.provider_results import placement_matches
    remaining = list(actual['windows'])
    failures = []
    for window in expected['windows']:
        index = next((i for i, candidate in enumerate(remaining)
                      if native_inventory({'windows': [candidate]}) == native_inventory({'windows': [window]})
                      and placement_matches(candidate, window, tolerance=3)
                      and all(candidate.get('monitor_identity', {}).get(key)
                              == window.get('monitor_identity', {}).get(key)
                              for key in ('connector', 'edid_hash'))), None)
        if index is None:
            failures.append({'reason': 'native app placement missing', 'expected': window})
        else:
            remaining.pop(index)
    failures.extend({'reason': 'unexpected native app window', 'actual': window} for window in remaining)
    return failures


def chrome_identity(observed):
    if not isinstance(observed, list) or not observed:
        raise RuntimeError('Browser observation is not a window list')
    return sorted([{
        'id': w['id'],
        'groups': sorted((g['id'], g.get('title', ''), g.get('color'), g.get('collapsed')) for g in w['groups']),
        'tabs': [(t['id'], t['url'], t['groupId'], t['index']) for t in w['tabs']],
    } for w in observed], key=lambda w: w['id'])


def evolve():
    before = f.observe_chrome()
    result = f.observe_chrome('evolve-synthetic')
    if not isinstance(result, dict) or 'error' in result:
        raise RuntimeError('Browser evolution failed: ' + repr(result))
    after = f.observe_chrome()
    first, second = chrome_identity(before), chrome_identity(after)
    if (len(first) != 7 or len(second) != 7
            or sum(len(w['tabs']) for w in first) != 42
            or sum(len(w['tabs']) for w in second) != 42
            or [(w['id'], w['groups']) for w in first] != [(w['id'], w['groups']) for w in second]):
        raise RuntimeError('Evolution changed native window/group identities or fixture counts')
    before_tabs = {t[0] for w in first for t in w['tabs']}
    after_tabs = {t[0] for w in second for t in w['tabs']}
    if len(before_tabs - after_tabs) != 1 or len(after_tabs - before_tabs) != 1 or first == second:
        raise RuntimeError('Evolution did not produce the required measured browsing changes')
    return {'before': before, 'after': after, 'reply': result,
            'measured': {'windows': 7, 'tabs': 42, 'tabs_replaced': 1,
                         'native_window_and_group_ids_preserved': True}}


def content_items(snapshot):
    """Content keys retain multiplicity; process IDs/timestamps never identify intent."""
    items = []
    for terminal in snapshot.get('terminals', []):
        items.append(('terminals', {'session': terminal['session']}, terminal.get('placement')))
    for key, fields in (
        ('file_manager', ('locations', 'active_tab')),
        ('vscode', ('window_key', 'folders', 'editor_uris', 'dirty_count', 'profile', 'workspace_file')),
    ):
        for window in snapshot.get(key, {}).get('windows', []):
            items.append((key, {field: window.get(field) for field in fields}, window.get('placement')))
    for app, record in sorted(snapshot.get('social_apps', {}).items()):
        items.append(('social_apps', {'app': app, 'mode': record['mode'], 'running': record['running']}, None))
        for placement in record.get('windows', []):
            items.append(('social_apps', {'app': app}, placement))
    for profile in snapshot.get('browsers', {}).get('google_chrome', {}).get('profiles', []):
        for window in profile['windows']:
            group_positions = {group['id']: index for index, group in enumerate(window.get('groups', []))}
            content = {
                'profile': profile['profile'], 'type': window.get('type'),
                'tabs': [{key: tab.get(key) for key in ('url', 'pinned', 'active')} |
                         {'group': group_positions.get(tab.get('group'))} for tab in window['tabs']],
                'groups': [{key: group.get(key) for key in ('title', 'color', 'collapsed')}
                           for group in window.get('groups', [])],
            }
            monitor = window.get('monitor') or {}
            placement = {'workspace': window.get('workspace_index'), 'workspace_name': window.get('workspace'),
                         'monitor': monitor.get('index'), 'monitor_identity': monitor,
                         'state': window.get('state'), 'geometry': window.get('geometry')}
            items.append(('browsers', content, placement))
    return items


def tmux_intent(snapshot):
    return sorted((s['name'], w['index'], w['name'], p['index'], p['cwd'],
                   p.get('codex', {}).get('session_id') if p.get('codex') else None,
                   p.get('label'))
                  for s in snapshot.get('sessions', []) for w in s['windows'] for p in w['panes'])


def differences(expected, actual):
    from workspace_state.provider_results import placement_matches
    failures = []
    remaining = list(content_items(actual))
    for category, content, placement in content_items(expected):
        index = next((i for i, item in enumerate(remaining) if item[:2] == (category, content)
                      and (placement is None and item[2] is None or placement is not None
                           and item[2] is not None and placement_matches(item[2], placement, tolerance=3)
                           and all(item[2].get('monitor_identity', {}).get(key)
                                   == placement.get('monitor_identity', {}).get(key)
                                   for key in ('connector', 'edid_hash')))), None)
        if index is None:
            failures.append({'category': category, 'reason': 'content/placement missing', 'expected': [content, placement]})
        else:
            remaining.pop(index)
    failures.extend({'category': category, 'reason': 'unexpected or duplicate item', 'actual': [content, placement]}
                    for category, content, placement in remaining)
    if tmux_intent(expected) != tmux_intent(actual):
        failures.append({'category': 'terminals', 'reason': 'tmux window/pane/CWD/UUID/name intent differs'})
    if expected.get('desktop', {}).get('workspace_names') != actual.get('desktop', {}).get('workspace_names'):
        failures.append({'category': 'desktop', 'reason': 'workspace names/order differ'})
    return failures


def capture():
    from workspace_state import cli, storage
    value = cli._capture_all()
    storage.validate(value)
    problems = cli._terminal_problems(value) + cli._browser_problems(value)
    for key, errors in value.get('capture_errors', {}).items():
        if errors:
            problems.append(key + ': ' + repr(errors))
    if problems:
        raise RuntimeError('Incomplete independent runtime capture: ' + '; '.join(problems))
    return value


def prepare(args):
    from workspace_state import browser, cli, operations, storage
    from workspace_state.login_status import status_path
    from workspace_state.startup import StageMarker, write_stage_marker
    if (ROOT / 'active.json').exists():
        previous = run_directory()
        if not (previous / 'result.json').exists() and not args.new_run:
            raise RuntimeError('An unfinished poweroff run exists; use --new-run explicitly to retain and supersede it')
    status = read(status_path())
    context = operations.OperationContext.from_dict(status.get('operation_context'))
    if (context.mode != 'startup' or context.boot_id != boot() or not context.matches(status)
            or context.login_generation != cli._login_generation_file()):
        raise RuntimeError('A current startup context is required for the controlled failed marker')
    stages = status.get('stages')
    if (status.get('operation_state') not in {'completed', 'failed'}
            or not isinstance(stages, list) or not stages
            or any(not isinstance(stage, dict) or stage.get('state') not in {'ready', 'skipped', 'degraded', 'failed'}
                   for stage in stages)):
        raise RuntimeError('Startup must reach a terminal operation with no pending, running or waiting stages before preparation')
    directory = ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    write(ROOT / 'active.json', {'run_id': directory.name})
    original = capture()
    original_native = f.shell()
    inventory = require_fixture_inventory(original, original_native, read(f.ROOT / 'conversation-identities.json'))
    write(directory / 'validated-fixture-inventory.json', inventory)
    write(directory / 'before-fault-native.json', original_native)
    write(directory / 'before-fault.json', original)
    write(directory / 'first-evolution.json', evolve())
    grouped = copy.deepcopy(original['browsers']['google_chrome'])
    for profile in grouped['profiles']:
        profile['windows'] = [window for window in profile['windows'] if window.get('groups')]
    if not any(profile['windows'] for profile in grouped['profiles']):
        raise RuntimeError('Controlled failure needs existing grouped browser windows')
    before_failure = chrome_identity(f.observe_chrome())
    error = None
    results = []
    try:
        results = browser.restore_browser(grouped, place=False,
                                          restore_token_prefix='fault-injection-' + directory.name)
    except browser.BrowserUnavailable as failure:
        error = str(failure)
    messages = [result.message for result in results]
    if not error and not any(not result.success for result in results):
        raise RuntimeError('Controlled stale grouped restore unexpectedly succeeded')
    if 'group' not in (error or ' '.join(messages)).lower():
        raise RuntimeError('Fault failed for an unrelated reason: ' + repr(error or messages))
    if before_failure != chrome_identity(f.observe_chrome()):
        raise RuntimeError('Refused grouped restore mutated browser windows/tabs/groups')
    marker = cli._startup_marker('browsers')
    write(directory / 'fault-injection.json', {
        'kind': 'controlled grouped stale-recipe restore; not evidence of natural startup failure',
        'error': error, 'messages': messages, 'browser_unchanged': True,
        'prior_marker': marker.read_text() if marker.exists() else None,
        'operation_context': context.to_dict(),
    })
    write_stage_marker(marker, StageMarker('browsers', 'failed', message='Controlled grouped stale-recipe fault injection',
                                         operation_context=context.to_dict()))
    marker_before = marker.read_bytes()
    log = f.run('wsctl', 'save', timeout=180)
    (directory / 'manual-save.log').write_text(log + '\n')
    manual = storage.load()
    write(directory / 'manual-baseline.json', manual)
    if marker.read_bytes() != marker_before:
        raise RuntimeError('Manual save erased the failed startup evidence')
    write(directory / 'second-evolution.json', evolve())
    session = next((s for s in manual['sessions'] if s['name'].startswith('scale-') and s['windows']), None)
    if session is None:
        raise RuntimeError('No owned synthetic tmux window is available')
    window = session['windows'][0]
    target = f"={session['name']}:{window['index']}"
    name = 'daytime-' + directory.name[:8]
    f.run('tmux', 'rename-window', '-t', target, name)
    expected = capture()
    live = f.shell()
    require_fixture_inventory(expected, live, read(f.ROOT / 'conversation-identities.json'))
    conversations = {p['codex']['session_id'] for s in expected['sessions'] for w in s['windows']
                     for p in w['panes'] if p.get('codex')}
    if conversations != set(read(f.ROOT / 'conversation-identities.json')) or len(conversations) != 23:
        raise RuntimeError('Expected 23 exact synthetic conversation identities')
    if not any(change['category'] == 'browsers' for change in differences(manual, expected)):
        raise RuntimeError('Daytime browser changes are absent')
    if not any(w['name'] == name for s in expected['sessions'] for w in s['windows']):
        raise RuntimeError('Synthetic window rename was not captured')
    pids = {w['pid'] for w in live['windows']}
    pids.update(p['codex']['pid'] for s in expected['sessions'] for w in s['windows'] for p in w['panes'] if p.get('codex'))
    identities = [f.process_identity(pid) for pid in sorted(pids)]
    if any(identity is None for identity in identities):
        raise RuntimeError('A fixture process vanished before preparation')
    write(directory / 'expected.json', expected)
    write(directory / 'expected-native.json', live)
    write(directory / 'expected-chrome.json', f.observe_chrome())
    write(directory / 'before.json', {
        'run_id': directory.name, 'expect': args.expect, 'boot_id': boot(), 'login': f.wayland_login(),
        'login_generation': context.login_generation, 'prepared_at': time.time(),
        'installed_release': args.installed_release, 'process_identities': identities,
        'canonical_digest': digest(storage.load()), 'expected_digest': digest(expected),
        'manual_baseline_digest': digest(manual),
        'native_inventory': native_inventory(live), 'synthetic_conversation_ids': sorted(conversations),
        'limitations': LIMITS,
    })
    print(json.dumps({'prepared': True, 'run_directory': str(directory), 'expect': args.expect,
                      'next': 'Start watch, then request and confirm real GNOME Power Off externally'}))


def receipt_matches(status, receipt):
    context = status.get('operation_context')
    return bool(isinstance(context, dict) and isinstance(receipt, dict)
                and receipt.get('schema_version') == 1
                and receipt.get('operation_context') == context
                and receipt.get('operation_id') == status.get('operation_id')
                and receipt.get('session_id', receipt.get('login_generation')) == status.get('session_id'))


def shutdown_evidence(directory, before):
    status_files = sorted(directory.glob('watch-*-status.json'))
    statuses = [read(path) for path in status_files]
    statuses = [s for s in statuses if s.get('mode') == 'shutdown'
                and s.get('operation_context', {}).get('boot_id') == before['boot_id']
                and s.get('session_id') == before['login_generation']
                and s.get('shutdown_action') == 'poweroff' and s.get('shutdown_origin') == 'preflight'
                and datetime.fromisoformat(s['started_at']).timestamp() >= before['prepared_at'] - 1]
    if not statuses:
        raise RuntimeError('No current real GNOME power-off transaction was observed')
    latest = statuses[-1]
    current = digest(latest['operation_context'])
    attempts = {}
    for status in statuses:
        attempts[digest(status['operation_context'])] = status
    cancelled_attempts = []
    for key, attempt in attempts.items():
        if key == current:
            continue
        if attempt.get('operation_state') != 'cancelled' or attempt.get('cancelled') is not True:
            raise RuntimeError('An earlier shutdown transaction has no completed cancellation; evidence is ambiguous')
        cancelled_attempts.append({'operation_id': attempt['operation_id'], 'state': 'cancelled'})
    statuses = [s for s in statuses if digest(s['operation_context']) == current]
    if any(status.get('cancelled') or status.get('overall_state') == 'failed' for status in statuses):
        raise RuntimeError('Observed shutdown was cancelled or failed')
    receipts = {name: read(directory / name) for name in RECEIPTS if (directory / name).exists()}
    for name in ('shutdown-hud-rendered.json', 'shutdown-worker-complete.json'):
        if not receipt_matches(latest, receipts.get(name)):
            raise RuntimeError('Missing real matching receipt: ' + name)
    committed = receipt_matches(latest, receipts.get('shutdown-commit.json'))
    authorized = receipt_matches(latest, receipts.get('shutdown-prepared.json'))
    if not committed and not authorized:
        raise RuntimeError('No matching real commit receipt or durable countdown authorization was observed')
    if not any(s.get('operation_state') in {'prepared', 'authorized', 'completed'} for s in statuses):
        raise RuntimeError('Managed checkpoint/countdown state was not observed')
    rendered_at = datetime.fromisoformat(receipts['shutdown-hud-rendered.json']['rendered_at']).timestamp()
    committed_at = (datetime.fromisoformat(receipts['shutdown-commit.json']['committed_at']).timestamp()
                    if committed else float(receipts['shutdown-prepared.json']['created_at']))
    if rendered_at < before['prepared_at'] or committed_at - rendered_at < 3:
        raise RuntimeError('Real render/countdown evidence did not span the required three seconds')
    if authorized:
        prepared, worker = receipts['shutdown-prepared.json'], receipts['shutdown-worker-complete.json']
        if (prepared.get('invocation_id') != worker.get('invocation_id') or not worker.get('invocation_id')
                or prepared.get('action') != 'poweroff' or prepared.get('origin') != 'preflight'):
            raise RuntimeError('Durable authorization does not match the managed worker invocation')
    return {'operation_id': latest['operation_id'], 'render_receipt': True,
            'worker_receipt': True, 'commit_receipt_observed': committed,
            'countdown_seconds': committed_at - rendered_at,
            'durable_authorization_observed': authorized, 'cancelled_attempts': cancelled_attempts,
            'latest_status': latest}


def watch(args):
    from workspace_state.login_status import runtime_root, status_path
    from workspace_state.storage import path_for
    directory = run_directory()
    before = read(directory / 'before.json')
    if boot() != before['boot_id']:
        raise RuntimeError('Watch must run before the prepared power-off')
    paths = {'status': status_path(), 'canonical': path_for(),
             **{name: runtime_root() / name for name in RECEIPTS}}
    last = {}
    sequence = max([int(path.name.split('-')[1]) for path in directory.glob('watch-*-status.json')] or [0])
    write(directory / f'watch-start-{time.time_ns()}.json', {'started_at': time.time(), 'boot_id': boot()})
    deadline = time.monotonic() + min(args.timeout, 120)
    while time.monotonic() < deadline:
        for name, path in paths.items():
            try:
                value = read(path)
            except (OSError, ValueError):
                continue
            fingerprint = digest(value)
            if last.get(name) == fingerprint:
                continue
            last[name] = fingerprint
            sequence += 1
            if name == 'status':
                write(directory / f'watch-{sequence:06}-status.json', value)
            else:
                write(directory / ('shutdown-canonical.json' if name == 'canonical' else name), value)
                if name in RECEIPTS:
                    write(directory / f'watch-{sequence:06}-{name}', value)
            write(directory / 'watch-progress.json', {'sequence': sequence, 'last_kind': name,
                                                      'observed_at': time.time(), 'boot_id': boot()})
        time.sleep(.2)
    raise RuntimeError('Watch expired without VM exit; no power-off success is claimed')


def vm_restore_evidence():
    """Use the production private-file validator; never execute restore jobs."""
    from workspace_state.shutdown_profiles import _read_startup_restore
    restore = _read_startup_restore()
    if restore is None:
        return {'present': False}
    document, runtimes = restore
    return {'present': True, 'document': document, 'restore_jobs': len(runtimes)}


def verified_empty_vm_skip(stage, status, before, evidence, shutdown_operation_id):
    if (stage.get('state') != 'skipped' or type(stage.get('current')) is not int or stage['current'] != 0
            or type(stage.get('total')) is not int or stage['total'] != 0
            or stage.get('provider_results')):
        return False
    if stage.get('message') == 'No committed VM restore jobs':
        return evidence == {'present': False}
    if stage.get('message') != 'No Windows VM was active at shutdown' or not isinstance(evidence, dict):
        return False
    document = evidence.get('document', {})
    return bool(evidence.get('present') is True and evidence.get('restore_jobs') == 0
                and document.get('entries') == [] and document.get('action') == 'poweroff'
                and shutdown_operation_id and document.get('operation_id') == shutdown_operation_id
                and document.get('source_boot_id') == before['boot_id']
                and document.get('session_id') == before['login_generation']
                and document.get('restored_boot_id') == boot()
                and document.get('restored_login_generation') == status.get('session_id')
                and document.get('committed_at', 0) >= before['prepared_at']
                and document.get('restored_at', 0) >= document.get('committed_at', 0))


def startup_failures(status, before, snapshot, *, vm_evidence=None, shutdown_operation_id=None):
    problems = []
    context = status.get('operation_context', {})
    if (status.get('mode') != 'startup' or context.get('mode') != 'startup'
            or status.get('operation_id') != context.get('operation_id') or context.get('boot_id') != boot()
            or context.get('login_generation') == before['login_generation']
            or status.get('session_id') != context.get('login_generation')
            or datetime.fromisoformat(status['started_at']).timestamp() < before['prepared_at']):
        problems.append('Startup status is not from the new boot/login')
    if status.get('operation_state') != 'completed':
        problems.append('Startup operation did not complete')
    required = {'gnome', 'displays', 'tmux', 'terminals', 'codex', 'browsers', 'social-apps',
                'file-manager', 'vscode', 'virtual-machines', 'workspace', 'gdrive', 'nextcloud',
                'pdrive', 'warmup', 'login-finalization'}
    stages = {stage['id']: stage for stage in status.get('stages', [])}
    if not required.issubset(stages):
        problems.append('Required startup stages are missing')
    for name, stage in stages.items():
        if name == 'virtual-machines':
            if not verified_empty_vm_skip(stage, status, before, vm_evidence, shutdown_operation_id):
                problems.append('Synthetic command-profile VM skip lacks matching empty restore evidence')
        elif stage.get('state') != 'ready':
            problems.append(f"Startup stage {name} is {stage.get('state')}")
    counts = {'browsers': sum(len(p['windows']) for p in snapshot['browsers']['google_chrome']['profiles']),
              'social-apps': sum(len(app.get('windows', [])) for app in snapshot.get('social_apps', {}).values()),
              'file-manager': len(snapshot.get('file_manager', {}).get('windows', [])),
              'vscode': len(snapshot.get('vscode', {}).get('windows', []))}
    for category, count in counts.items():
        receipts = stages.get(category, {}).get('provider_results', [])
        if len(receipts) != count or any(not result.get('success') or any(
                result.get(phase, {}).get('state') != 'verified' for phase in ('identity', 'placement'))
                for result in receipts):
            problems.append('Missing or unsuccessful startup provider receipts: ' + category)
        for result in receipts:
            content = result.get('content', {})
            if (content.get('state') != 'verified' and not (
                    category == 'social-apps' and content.get('state') == 'skipped'
                    and content.get('detail') == 'App content recovery is owned by the application')):
                problems.append('Unverified startup provider content: ' + category)
    return problems


def native_browser_identity_failures(chrome):
    original = read(f.ROOT / 'chrome-login-original.json')
    launch, observation = read(f.ROOT / 'chrome-launch.json'), read(f.ROOT / 'chrome-observation-launch.json')
    if observation.get('baseline_valid') is not True:
        return ['Native-ID baseline was not freshly validated for this browser launch']
    if launch['generation'] != observation['generation'] or f.process_identity(launch['pid']) != observation['process']:
        return ['Native-ID baseline belongs to another browser launch']
    if chrome_identity(original) != chrome_identity(chrome):
        return ['Restoration changed native browser window/tab/group IDs or contents']
    return []


def verify(args):
    from workspace_state import storage
    from workspace_state.login_status import status_path
    directory = run_directory()
    before = read(directory / 'before.json')
    expected = read(directory / 'expected.json')
    if boot() == before['boot_id']:
        raise RuntimeError('A real VM power-off and boot is required; same-boot login is insufficient')
    if digest(expected) != before['expected_digest']:
        raise RuntimeError('Independent expected state was modified')
    if args.installed_release != before['installed_release']:
        raise RuntimeError('Installed release changed within this power-off cycle')
    evidence = shutdown_evidence(directory, before)
    archived = read(directory / 'shutdown-canonical.json')
    baseline = read(directory / 'manual-baseline.json')
    if digest(baseline) != before['manual_baseline_digest']:
        raise RuntimeError('Manual baseline evidence was modified')
    # This sealed shutdown evidence does not depend on Chrome becoming available
    # again after boot (for example, it may display a native Restore pages gate).
    checks = {'shutdown_canonical': differences(expected, archived)}
    browser_regression = any(item.get('category') == 'browsers' for item in checks['shutdown_canonical'])
    archived_browser = [item for item in content_items(archived) if item[0] == 'browsers']
    baseline_browser = [item for item in content_items(baseline) if item[0] == 'browsers']
    reproduced = browser_regression and archived_browser == baseline_browser
    observations = {}

    def observe(name, function, *, artifact=None):
        try:
            value = function()
            if value is None:
                raise RuntimeError('Verification probe returned no evidence')
            if artifact:
                write(directory / artifact, value)
        except Exception as error:
            failure = {'reason': 'verification observation unavailable',
                       'error_type': type(error).__name__, 'error': str(error)}
            checks[name] = [failure]
            observations[name] = {'state': 'unavailable', **failure}
            write(directory / ('unavailable-' + name + '.json'), failure)
            return None
        observations[name] = {'state': 'observed'}
        return value

    observe('companions', f.verify_running_companions, artifact='verified-companions.json')
    actual = observe('live_capture', capture, artifact='verified-live.json')
    canonical = observe('canonical_capture', storage.load, artifact='verified-canonical.json')
    status = observe('startup_status', lambda: read(status_path()), artifact='verified-status.json')
    native = observe('native_capture', f.shell, artifact='verified-native.json')
    chrome = observe('chrome_capture', f.observe_chrome, artifact='verified-chrome.json')
    vm_evidence = observe('vm_restore_receipt', vm_restore_evidence, artifact='verified-vm-restore.json')
    if actual is not None and native is not None:
        observe('fixture_inventory', lambda: require_fixture_inventory(
            actual, native, before['synthetic_conversation_ids']), artifact='verified-fixture-inventory.json')
    for name, value in (('live', actual), ('canonical', canonical)):
        if value is not None:
            result = observe(name, lambda value=value: differences(expected, value))
            if result is not None:
                checks[name] = result
    if status is not None:
        result = observe('startup', lambda: startup_failures(
            status, before, expected, vm_evidence=vm_evidence, shutdown_operation_id=evidence['operation_id']))
        if result is not None:
            checks['startup'] = result
    if native is not None:
        result = observe('native_placement', lambda: native_placement_failures(
            read(directory / 'expected-native.json'), native))
        if result is not None:
            checks['native_placement'] = result
        result = observe('native_inventory', lambda: native_inventory(native))
        if result is not None and result != before['native_inventory']:
            checks['native_inventory'] = ['Native application inventory differs or contains duplicates']

    if chrome is not None:
        result = observe('native_browser_identity', lambda: native_browser_identity_failures(chrome))
        if result is not None:
            checks['native_browser_identity'] = result

    def processes():
        pids = {w['pid'] for w in native['windows']} if native is not None else set()
        if actual is not None:
            pids.update(p['codex']['pid'] for s in actual['sessions'] for w in s['windows']
                        for p in w['panes'] if p.get('codex'))
        identities = [f.process_identity(pid) for pid in sorted(pids)]
        if any(identity is None for identity in identities):
            raise RuntimeError('A captured process exited before verification')
        return {'boot_id': boot(), 'previous_boot_id': before['boot_id'],
                'complete': native is not None and actual is not None, 'identities': identities}

    observe('process_identities', processes, artifact='verified-process-identities.json')
    new_login = observe('login_identity', f.wayland_login)
    failures = {key: value for key, value in checks.items() if value}
    candidate_passed = not failures and before['expect'] == 'pass'
    expected_failure = before['expect'] == 'browser-retention-regression' and reproduced
    result = {'passed': candidate_passed, 'expected_failure_reproduced': expected_failure,
              'outcome': 'passed' if candidate_passed else 'expected-regression' if expected_failure else 'failed',
              'failures': failures, 'shutdown_evidence': evidence, 'new_boot_id': boot(),
              'new_login': new_login, 'before': before, 'limitations': LIMITS, 'observations': observations,
              'browser_regression_evidence': {'shutdown_browser_differs_from_expected': browser_regression,
                                              'shutdown_browser_equals_manual_baseline': archived_browser == baseline_browser},
              'installed_release': args.installed_release, 'time': time.time()}
    write(directory / 'result.json', result)
    print(json.dumps({'outcome': result['outcome'], 'passed': candidate_passed,
                      'expected_failure_reproduced': expected_failure, 'run_directory': str(directory)}))
    if not candidate_passed and not expected_failure:
        raise RuntimeError('Power-off verification failed; see result.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', required=True, action='store_true')
    parser.add_argument('--expect', choices=('pass', 'browser-retention-regression'), default='pass')
    parser.add_argument('--new-run', action='store_true')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('phase', choices=('prepare', 'watch', 'verify'))
    args = parser.parse_args()
    args.installed_release = guest_environment()
    try:
        globals()[args.phase](args)
    except Exception as error:
        if (ROOT / 'active.json').exists():
            write(run_directory() / ('failure-' + args.phase + '.json'),
                  {'error': type(error).__name__ + ': ' + str(error), 'time': time.time()})
        raise


if __name__ == '__main__':
    main()
