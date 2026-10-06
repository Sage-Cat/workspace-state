#!/usr/bin/python3
"""Verify named five-pane restoration through a genuine GNOME power-off.

Copy this file beside run_vm_scale.py and run_vm_poweroff.py in the disposable
guest. Install and activate the candidate production release first, then run:

  python3 run_vm_tmux_names.py --disposable-guest --expected-release r-<24hex> prepare
  python3 run_vm_tmux_names.py --disposable-guest watch

While watch runs, confirm the real GNOME Power Off dialog through the VM console.
Observe the actual host QMP guest SHUTDOWN and EOF, cold boot the same VM, and
copy that host observation to the printed run_directory/qmp-exit.json. After the
normal production login coordinator settles, run:

  python3 run_vm_tmux_names.py --disposable-guest verify

This harness never requests shutdown, stops tmux, starts a restore/coordinator,
changes HUD receipts/markers, or constructs the canonical checkpoint itself.
Preparation moves two already verified synthetic workers between existing
windows, sets native tmux names/layout/policies, and calls installed wsctl save.
Use prepare-continuity for subsequent cycles: it only observes the existing
five-pane fixture and calls no manual save or tmux mutation.
All mutation is guarded to the consented wsctl-validation KVM fixture.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shlex
import socket
import subprocess
import time
import uuid

import run_vm_poweroff as p

SCENARIO = 'five-pane-names-through-real-gnome-poweroff'
LIMITS = [*p.LIMITS,
          'Synthetic conversation screen content proves UUID ownership/readiness, not authenticated Codex behavior',
          'Pane titles and process/tmux IDs may change; durable labels, UUID positions, policies and geometry are compared',
          'Initial preparation uses manual save; continuity preparation only observes the existing fixture',
          'Pane border presentation is recorded diagnostically; restoring a custom border format is not claimed']
SESSION_NAMES = {'main', *(f'scale-{index:02}' for index in range(2, 11))}
UUID_RE = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'


def guard():
    if socket.gethostname() != 'wsctl-validation' or p.f.run('systemd-detect-virt') != 'kvm':
        raise RuntimeError('Refusing outside wsctl-validation KVM guest')
    consent = p.read(p.f.ROOT / 'consent.json')
    if consent.get('hostname') != 'wsctl-validation' or consent.get('synthetic_only') is not True:
        raise RuntimeError('Explicit synthetic-only fixture consent is missing')
    fixture = p.f.ROOT / 'fixture.py'
    expected = f'#!/bin/sh\nexec /usr/bin/python3 {shlex.quote(str(fixture))} conversation "$@"\n'
    if (Path.home() / '.local/bin/codex').read_text() != expected:
        raise RuntimeError('Refusing a noncanonical or authenticated Codex executable')
    if fixture.read_bytes() != (p.f.HERE / 'vm_scale_fixture.py').read_bytes():
        raise RuntimeError('Synthetic worker does not match the supplied guarded fixture')


def release_id(value):
    if not isinstance(value, str):
        raise RuntimeError('An exact immutable candidate release ID is required')
    path = Path(value)
    identifier = path.parts[-3] if len(path.parts) >= 3 and path.parts[-2:] == ('components', 'workspace-state') else value
    if not isinstance(identifier, str) or not re.fullmatch(r'r-[0-9a-f]{24}', identifier):
        raise RuntimeError('An exact immutable candidate release ID is required')
    return identifier


def tmux_raw(*args):
    return subprocess.run(['tmux', *map(str, args)], text=True, capture_output=True,
                          check=True, timeout=10).stdout


def tmux(*args):
    return tmux_raw(*args).removesuffix('\n')


def policy(output):
    if output not in {'on', 'off', '1', '0'}:
        raise RuntimeError('Invalid native tmux rename policy: ' + repr(output))
    return output in {'on', '1'}


def literal(value, *, expands_formats=False):
    if expands_formats:
        value = value.replace('#', '##')
    return value[:-1] + r'\;' if value.endswith(';') else value


def synthetic_worker_argv(argv, identity):
    return (argv[:3] == ['/usr/bin/python3', str(p.f.ROOT / 'fixture.py'), 'conversation']
            and bool(argv) and argv[-1] == identity
            and tuple(argv[3:-1]) in {(), ('resume',), ('resume', '--no-alt-screen')})


def synthetic_content(pane_id, pid, cwd):
    """Use terminal output as the independent UUID oracle, then prove ownership."""
    from workspace_state.capture import codex_for_pane
    text = tmux('capture-pane', '-p', '-J', '-S', '-', '-t', pane_id)
    found = set(re.findall(r'SYNTHETIC conversation (' + UUID_RE + r'): local process/UUID readiness only', text))
    if len(found) != 1 or 'Fixture ready; no authentication or model request' not in text:
        raise RuntimeError('Pane lacks one complete synthetic screen UUID/readiness record: ' + pane_id)
    identity = next(iter(found))
    codex = codex_for_pane(pid, cwd)
    if not codex or codex.get('session_id') != identity:
        raise RuntimeError('Observed screen and live worker UUID disagree: ' + pane_id)
    argv = Path(f"/proc/{int(codex['pid'])}/cmdline").read_bytes().decode().rstrip('\0').split('\0')
    if not synthetic_worker_argv(argv, identity):
        raise RuntimeError('Pane is not owned by the exact synthetic conversation worker: ' + pane_id)
    process = p.f.process_identity(codex['pid'])
    if process is None:
        raise RuntimeError('Synthetic worker exited during observation: ' + pane_id)
    return {'uuid': identity, 'ready_text': 'Fixture ready; no authentication or model request',
            'screen': text, 'worker_process': process, 'worker_argv': argv}


def native_tmux():
    """Read native pane identity, geometry and screen content without a recipe."""
    fields = '\t'.join(('#{session_name}', '#{window_index}', '#{window_id}', '#{pane_index}',
                        '#{pane_id}', '#{pane_pid}', '#{pane_current_path}', '#{pane_current_command}',
                        '#{pane_left}', '#{pane_top}', '#{pane_width}', '#{pane_height}',
                        '#{pane_active}', '#{window_active}'))
    sessions = {}
    for line in tmux('list-panes', '-a', '-F', fields).splitlines():
        row = line.split('\t')
        if len(row) != 14 or row[0] not in SESSION_NAMES:
            raise RuntimeError('Unowned or malformed native tmux inventory: ' + repr(row))
        name, index, window_id, pane_index, pane_id, pid, cwd, command = row[:8]
        if not Path(cwd).resolve().is_relative_to((p.f.ROOT / 'projects').resolve()):
            raise RuntimeError('Pane CWD is outside the synthetic fixture: ' + cwd)
        windows = sessions.setdefault(name, {})
        if int(index) not in windows:
            identity, separator, window_name = tmux('display-message', '-p', '-t', window_id,
                                                     '#{window_id}\n#{window_name}').partition('\n')
            if not separator or identity != window_id:
                raise RuntimeError('Native window identity changed while observing')
            size = tmux('display-message', '-p', '-t', window_id, '#{window_width}\t#{window_height}').split('\t')
            windows[int(index)] = {'index': int(index), 'id': window_id, 'name': window_name,
                'automatic_rename': policy(tmux('show-options', '-A', '-w', '-qv', '-t', window_id, 'automatic-rename')),
                'layout': tmux('display-message', '-p', '-t', window_id, '#{window_layout}'),
                'size': list(map(int, size)), 'active': row[13] == '1',
                'border_format': tmux('show-options', '-A', '-w', '-qv', '-t', window_id, 'pane-border-format'),
                'panes': []}
        title_id, separator, title = tmux('display-message', '-p', '-t', pane_id, '#{pane_id}\n#{pane_title}').partition('\n')
        if not separator or title_id != pane_id:
            raise RuntimeError('Native pane identity changed while observing')
        label = tmux_raw('show-options', '-p', '-qv', '-t', pane_id, '@pane_label')
        windows[int(index)]['panes'].append({'index': int(pane_index), 'id': pane_id, 'pid': int(pid),
            'cwd': cwd, 'command': command, 'title': title,
            'label': label.removesuffix('\n') if label else None,
            'allow_rename': policy(tmux('show-options', '-A', '-p', '-qv', '-t', pane_id, 'allow-rename')),
            'geometry': list(map(int, row[8:12])), 'active': row[12] == '1',
            'content': synthetic_content(pane_id, int(pid), cwd)})
    value = [{'name': name, 'windows': [dict(window, panes=sorted(window['panes'], key=lambda item: item['index']))
                                       for _, window in sorted(windows.items())]}
             for name, windows in sorted(sessions.items())]
    ids = [pane['content']['uuid'] for session in value for window in session['windows'] for pane in window['panes']]
    expected = p.read(p.f.ROOT / 'conversation-identities.json')
    if set(sessions) != SESSION_NAMES or len(ids) != 23 or len(set(ids)) != 23 or set(ids) != set(expected):
        raise RuntimeError('Native inventory must preserve exactly ten fixture sessions and 23 unique synthetic workers')
    return value


def intent(native, *, geometry=True):
    return [{'name': session['name'], 'windows': [
        {'index': window['index'], 'name': window['name'], 'automatic_rename': window['automatic_rename'],
         **({'size': window['size'], 'active': window['active']} if geometry else {}),
         'panes': [{'index': pane['index'], 'cwd': pane['cwd'], 'label': pane['label'],
                    'allow_rename': pane['allow_rename'], 'uuid': pane['content']['uuid'],
                    **({'geometry': pane['geometry'], 'active': pane['active']} if geometry else {})}
                   for pane in window['panes']]}
        for window in session['windows']]} for session in native]


def snapshot_intent(snapshot):
    result = []
    for session in sorted(snapshot['sessions'], key=lambda item: item['name']):
        windows = []
        for window in sorted(session['windows'], key=lambda item: item['index']):
            if 'automatic_rename' not in window or any(any(key not in pane for key in ('title', 'label', 'allow_rename'))
                                                      for pane in window['panes']):
                raise RuntimeError('Installed production capture lacks complete pane/window naming metadata')
            windows.append({'index': window['index'], 'name': window['name'], 'automatic_rename': window['automatic_rename'],
                'panes': [{'index': pane['index'], 'cwd': pane['cwd'], 'label': pane['label'],
                           'allow_rename': pane['allow_rename'], 'uuid': (pane.get('codex') or {}).get('session_id')}
                          for pane in sorted(window['panes'], key=lambda item: item['index'])]})
        result.append({'name': session['name'], 'windows': windows})
    return result


def settled_status():
    from workspace_state import cli, operations
    from workspace_state.login_status import status_path
    status = p.read(status_path())
    context = operations.OperationContext.from_dict(status.get('operation_context'))
    if (context.mode != 'startup' or context.boot_id != p.boot() or not context.matches(status)
            or context.login_generation != cli._login_generation_file()
            or status.get('operation_state') not in {'completed', 'failed'}
            or not status.get('stages') or any(stage.get('state') not in {'ready', 'skipped', 'failed', 'degraded'}
                                            for stage in status['stages'])):
        raise RuntimeError('A current settled production startup operation is required; no status is reset')
    return status


def hud_bytes():
    from workspace_state import cli
    from workspace_state.login_status import status_path
    paths = [status_path(), *cli._startup_directory().glob('*.done')]
    return {str(path): path.read_bytes().hex() for path in paths if path.is_file()}


def asymmetric_layout(width, height, panes):
    if width < 40 or height < 12 or len(panes) != 5:
        raise RuntimeError('Five-pane asymmetric fixture requires at least a 40x12 native window')
    left, upper = width * 3 // 5, height * 3 // 5
    bottom, right = height - upper - 1, width - left - 1
    first, lower_left = upper * 3 // 5, width * 2 // 5
    ids = [pane['id'][1:] for pane in panes]
    body = (f'{width}x{height},0,0[{width}x{upper},0,0{{{left}x{upper},0,0['
            f'{left}x{first},0,0,{ids[0]},{left}x{upper-first-1},0,{first+1},{ids[1]}],'
            f'{right}x{upper},{left+1},0,{ids[2]}}},{width}x{bottom},0,{upper+1}{{'
            f'{lower_left}x{bottom},0,{upper+1},{ids[3]},'
            f'{width-lower_left-1}x{bottom},{lower_left+1},{upper+1},{ids[4]}}}]')
    checksum = 0
    for character in body:
        checksum = ((checksum >> 1) + ((checksum & 1) << 15) + ord(character)) & 0xffff
    return f'{checksum:04x},{body}'


def chosen_window(native, name, expected_panes):
    session = next((item for item in native if item['name'] == name), None)
    if session is None or len(session['windows']) != 1 or len(session['windows'][0]['panes']) != expected_panes:
        raise RuntimeError(f'{name} must own exactly one {expected_panes}-pane synthetic window')
    return session['windows'][0]


def prepare(args, release):
    from workspace_state import storage
    guard()
    if release_id(release) != args.expected_release:
        raise RuntimeError('Activate the exact candidate before preparing this production-capture cycle')
    if (p.ROOT / 'active.json').exists():
        prior = p.run_directory()
        if not (prior / 'result.json').exists() and not args.new_run:
            raise RuntimeError('An unfinished poweroff run exists; --new-run explicitly retains and supersedes it')
    if args.session == args.donor_session or args.session not in SESSION_NAMES or args.donor_session not in SESSION_NAMES:
        raise RuntimeError('Choose two different owned fixture sessions')
    status = settled_status()
    companions = p.f.verify_running_companions()
    original, original_native, original_tmux = p.capture(), p.f.shell(), native_tmux()
    ids = p.read(p.f.ROOT / 'conversation-identities.json')
    p.require_fixture_inventory(original, original_native, ids)
    primary, donor = chosen_window(original_tmux, args.session, 3), chosen_window(original_tmux, args.donor_session, 3)
    directory = p.ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    args.run_directory = directory
    for name, value in [('original', original), ('original-native', original_native), ('original-tmux', original_tmux),
                        ('prior-status', status), ('prior-companions', companions)]:
        p.write(directory / (name + '.json'), value)
    moving = donor['panes'][-2:]
    previous = primary['panes'][-1]['id']
    for pane in moving:
        tmux('select-layout', '-t', primary['id'], 'tiled')
        tmux('join-pane', '-d', '-s', pane['id'], '-t', previous)
        previous = pane['id']
    panes = [*primary['panes'], *moving]
    name = 'Five panes #{pane_id} #(printf literal) ;'
    tmux('rename-window', '-t', primary['id'], literal(name, expands_formats=True))
    tmux('set-option', '-w', '-t', primary['id'], 'automatic-rename', 'off')
    labels = ['Research α #{pane_id};', 'Build #(printf literal)', 'Notes with spaces', 'Review Україна', 'Debug ending;']
    for index, (pane, label) in enumerate(zip(panes, labels)):
        tmux('set-option', '-p', '-t', pane['id'], '@pane_label', literal(label))
        tmux('select-pane', '-t', pane['id'], '-T', literal(label, expands_formats=True))
        tmux('set-option', '-p', '-t', pane['id'], 'allow-rename', 'on' if index in {1, 4} else 'off')
    tmux('select-layout', '-t', primary['id'], asymmetric_layout(*primary['size'], panes))
    tmux('select-pane', '-t', panes[3]['id'])
    tmux('select-window', '-t', primary['id'])
    expected_tmux = native_tmux()
    named = chosen_window(expected_tmux, args.session, 5)
    chosen_window(expected_tmux, args.donor_session, 1)
    if named['name'] != name or [pane['label'] for pane in named['panes']] != labels:
        raise RuntimeError('Native names or pane order differ from the explicit fixture mutation')
    expected, native = p.capture(), p.f.shell()
    p.require_fixture_inventory(expected, native, ids)
    if snapshot_intent(expected) != intent(expected_tmux, geometry=False):
        raise RuntimeError('Production capture disagrees with independent native pane naming/content')
    # Only terminal topology is deliberately changed. Retain all other desktop
    # content/placement and multiplicity as a measured requirement.
    nonterminal = [item for item in p.differences(original, expected) if item.get('category') != 'terminals']
    native_changes = p.native_placement_failures(original_native, native)
    if nonterminal or native_changes:
        raise RuntimeError('Naming preparation changed unrelated desktop content/placement: ' + repr([nonterminal, native_changes]))
    protected = hud_bytes()
    log = p.f.run('wsctl', 'save', timeout=180)
    (directory / 'manual-save.log').write_text(log + '\n')
    if hud_bytes() != protected:
        raise RuntimeError('Production manual save changed existing HUD status or stage evidence')
    saved = storage.load()
    if snapshot_intent(saved) != intent(expected_tmux, geometry=False) or p.differences(expected, saved):
        raise RuntimeError('Production manual checkpoint differs from the independent observed desktop')
    after_save = native_tmux()
    if intent(after_save) != intent(expected_tmux):
        raise RuntimeError('Production save changed the observed native pane content/layout')
    for filename, value in [('expected.json', expected), ('expected-native.json', native),
                            ('expected-tmux.json', expected_tmux), ('expected-chrome.json', p.f.observe_chrome()),
                            ('manual-baseline.json', saved), ('protected-hud.json', protected)]:
        p.write(directory / filename, value)
    p.write(directory / 'before.json', {
        'scenario': SCENARIO, 'run_id': directory.name, 'expect': 'pass', 'boot_id': p.boot(),
        'login': p.f.wayland_login(), 'login_generation': status['operation_context']['login_generation'],
        'prepared_at': time.time(), 'installed_release': release, 'expected_release': args.expected_release,
        'expected_digest': p.digest(expected), 'expected_tmux_digest': p.digest(expected_tmux),
        'expected_native_digest': p.digest(native), 'canonical_digest': p.digest(saved),
        'manual_baseline_digest': p.digest(saved), 'native_inventory': p.native_inventory(native),
        'synthetic_conversation_ids': ids, 'graphical_launch': p.graphical_launch_evidence(),
        'manual_save_called': True, 'hud_failures_reset': False, 'prior_operation_state': status['operation_state'],
        'preparation': 'named-fixture-and-manual-save',
        'mutation': {'session': args.session, 'donor_session': args.donor_session,
                     'moved_uuids': [pane['content']['uuid'] for pane in moving], 'new_workers': 0,
                     'workers_killed': 0, 'sessions_created': 0}, 'limitations': LIMITS})
    p.write(p.ROOT / 'active.json', {'run_id': directory.name})
    return {'prepared': True, 'run_directory': str(directory), 'release': args.expected_release,
            'sessions': 10, 'workers': 23, 'named_panes': 5, 'manual_save_called': True,
            'prior_operation_state': status['operation_state'], 'hud_failures_reset': False,
            'next': 'Run watch, confirm genuine GNOME Power Off, record host QMP exit, cold boot, then verify'}


def prepare_continuity(args, release):
    """Observe a healthy existing fixture, without making a new baseline."""
    from workspace_state import storage
    guard()
    if release_id(release) != args.expected_release:
        raise RuntimeError('Activate the exact candidate before preparing continuity')
    if (p.ROOT / 'active.json').exists():
        prior = p.run_directory()
        if not (prior / 'result.json').exists() and not args.new_run:
            raise RuntimeError('An unfinished run exists; retain it explicitly with --new-run')
    status = settled_status()
    if status.get('operation_state') != 'completed':
        raise RuntimeError('Continuity requires successful production startup')
    before_hash, protected = p.digest(storage.load()), hud_bytes()
    expected, native, panes = p.capture(), p.f.shell(), native_tmux()
    chosen_window(panes, args.session, 5)
    chosen_window(panes, args.donor_session, 1)
    ids = p.read(p.f.ROOT / 'conversation-identities.json')
    p.require_fixture_inventory(expected, native, ids)
    if snapshot_intent(expected) != intent(panes, geometry=False):
        raise RuntimeError('Native naming evidence disagrees with production capture')
    companions, chrome = p.f.verify_running_companions(), p.f.observe_chrome()
    if before_hash != p.digest(storage.load()) or hud_bytes() != protected:
        raise RuntimeError('Production state changed during continuity preparation')
    directory = p.ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    args.run_directory = directory
    for name, value in [('expected', expected), ('expected-native', native), ('expected-tmux', panes),
                        ('expected-chrome', chrome), ('prior-status', status), ('prior-companions', companions)]:
        p.write(directory / (name + '.json'), value)
    p.write(directory / 'before.json', {
        'scenario': SCENARIO, 'preparation': 'observer-only-continuity', 'run_id': directory.name,
        'expect': 'pass', 'boot_id': p.boot(), 'login': p.f.wayland_login(),
        'login_generation': status['operation_context']['login_generation'], 'prepared_at': time.time(),
        'installed_release': release, 'expected_release': args.expected_release,
        'expected_digest': p.digest(expected), 'expected_tmux_digest': p.digest(panes),
        'expected_native_digest': p.digest(native), 'canonical_digest': before_hash,
        'native_inventory': p.native_inventory(native), 'synthetic_conversation_ids': ids,
        'graphical_launch': p.graphical_launch_evidence(), 'manual_save_called': False,
        'hud_failures_reset': False, 'prior_operation_state': status['operation_state'],
        'fixture_mutation': False, 'limitations': LIMITS})
    p.write(p.ROOT / 'active.json', {'run_id': directory.name})
    return {'prepared': True, 'run_directory': str(directory), 'release': args.expected_release,
            'sessions': 10, 'workers': 23, 'named_panes': 5, 'manual_save_called': False,
            'fixture_mutation': False, 'hud_failures_reset': False}


def verify(release):
    from workspace_state import storage
    guard()
    directory = p.run_directory()
    before = p.read(directory / 'before.json')
    preparation = before.get('preparation', 'named-fixture-and-manual-save')
    valid_preparation = (
        preparation == 'named-fixture-and-manual-save' and before.get('manual_save_called') is True
        or preparation == 'observer-only-continuity' and before.get('manual_save_called') is False
        and before.get('fixture_mutation') is False and before.get('prior_operation_state') == 'completed')
    if before.get('scenario') != SCENARIO or not valid_preparation or before.get('hud_failures_reset') is not False:
        raise RuntimeError('This is not the prepared production naming cycle')
    if p.boot() == before['boot_id'] or release_id(release) != before['expected_release']:
        raise RuntimeError('An actual cold boot into the bound candidate is required')
    checks, observations = {}, {}

    def observe(key, function):
        try:
            value = function()
            if value is None:
                raise RuntimeError('No evidence returned')
            p.write(directory / ('names-verified-' + key + '.json'), value)
            observations[key] = 'observed'
            return value
        except Exception as error:
            checks[key] = [{'error': type(error).__name__ + ': ' + str(error)}]
            observations[key] = 'unavailable'
            return None

    def compare(key, function):
        try:
            checks[key] = function()
        except Exception as error:
            checks[key] = [{'error': type(error).__name__ + ': ' + str(error)}]

    expected = observe('expected', lambda: p.read(directory / 'expected.json'))
    expected_tmux = observe('expected-tmux', lambda: p.read(directory / 'expected-tmux.json'))
    expected_native = observe('expected-native', lambda: p.read(directory / 'expected-native.json'))
    for key, value, digest_key in [('expected', expected, 'expected_digest'),
                                  ('expected-tmux', expected_tmux, 'expected_tmux_digest'),
                                  ('expected-native', expected_native, 'expected_native_digest')]:
        if value is not None and p.digest(value) != before[digest_key]:
            checks[key] = ['Independent expected evidence changed']
    status = observe('status', settled_status)
    actual = observe('live', p.capture)
    native = observe('native', p.f.shell)
    panes = observe('tmux', native_tmux)
    canonical = observe('canonical', storage.load)
    chrome = observe('chrome', p.f.observe_chrome)
    observe('companions', p.f.verify_running_companions)
    boot = observe('boot', p.current_boot_evidence)
    shutdown = observe('shutdown', lambda: p.shutdown_evidence(directory, before))
    vm = observe('vm-restore', p.vm_restore_evidence)
    if boot is not None:
        for key, function in [('qmp', lambda: p.qmp_exit_evidence(directory, before, boot)),
                              ('user-manager', lambda: p.user_manager_shutdown_evidence(before, boot)),
                              ('graphical_shutdown', lambda: p.graphical_shutdown_evidence(directory, before, boot))]:
            value = observe(key, function)
            if value is not None:
                checks[key] = value['failures']
                if key == 'graphical_shutdown':
                    p.write(directory / 'verified-graphical_shutdown.json', value)
        if shutdown is not None and (directory / 'verified-graphical_shutdown.json').exists():
            value = observe('graphical-drain', lambda: p.graphical_drain_evidence(directory, before, boot, shutdown))
            if value is not None:
                checks['graphical-drain'] = value['failures']
    if actual is not None and native is not None:
        observe('fixture-inventory', lambda: p.require_fixture_inventory(actual, native, before['synthetic_conversation_ids']))
    if expected_tmux is not None and panes is not None:
        compare('native-pane-intent', lambda: [] if intent(expected_tmux) == intent(panes) else ['Independent native names/policies/UUID positions/geometry/active selection differ'])
        checks['duplicate-workers'] = [] if Counter(pane['content']['uuid'] for s in panes for w in s['windows'] for pane in w['panes']) == Counter(before['synthetic_conversation_ids']) else ['Missing or duplicated synthetic UUID workers']
    if expected is not None:
        for key, value in [('live', actual), ('canonical', canonical)]:
            if value is not None:
                compare(key + '-desktop', lambda value=value: p.differences(expected, value))
                if expected_tmux is not None:
                    compare(key + '-names', lambda value=value: [] if snapshot_intent(value) == intent(expected_tmux, geometry=False) else ['Captured naming metadata differs from independent native evidence'])
        archived = observe('shutdown-canonical', lambda: p.read(directory / 'shutdown-canonical.json'))
        if archived is not None:
            compare('shutdown-desktop', lambda: p.differences(expected, archived))
            if expected_tmux is not None:
                compare('shutdown-names', lambda: [] if snapshot_intent(archived) == intent(expected_tmux, geometry=False) else ['Shutdown checkpoint did not preserve independent native naming evidence'])
    if native is not None and expected_native is not None:
        compare('native-placement', lambda: p.native_placement_failures(expected_native, native))
        checks['native-inventory'] = [] if p.native_inventory(native) == before['native_inventory'] else ['Changed or duplicate native windows']
    if chrome is not None:
        compare('browser-identity', lambda: p.native_browser_identity_failures(chrome))
    if status is not None and shutdown is not None and expected is not None:
        compare('startup', lambda: p.startup_failures(status, before, expected, vm_evidence=vm,
                                                     shutdown_operation_id=shutdown['operation_id']))
    failures = {key: value for key, value in checks.items() if value}
    result = {'passed': not failures, 'scenario': SCENARIO, 'run_id': before['run_id'],
              'release': release_id(release), 'old_boot_id': before['boot_id'], 'new_boot_id': p.boot(),
              'native_windows': len(native['windows']) if native else None, 'failures': failures,
              'observations': observations, 'manual_save_called': before['manual_save_called'],
              'preparation': preparation, 'hud_failures_reset': False,
              'prior_operation_state': before['prior_operation_state'], 'limitations': LIMITS}
    p.write(directory / 'tmux-names-result.json', result)
    p.write(directory / 'result.json', result)
    if failures:
        raise RuntimeError('Genuine power-off naming regression failed; see tmux-names-result.json')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--expected-release', help='Exact active production candidate ID; required during prepare')
    parser.add_argument('--session', default='main', help='Existing three-pane synthetic target session')
    parser.add_argument('--donor-session', default='scale-02', help='Existing three-pane synthetic donor session')
    parser.add_argument('--new-run', action='store_true', help='Retain and explicitly supersede unfinished prior evidence')
    parser.add_argument('--timeout', type=float, default=120, help='Watch deadline, capped at 120 seconds')
    parser.add_argument('phase', choices=('prepare', 'prepare-continuity', 'watch', 'verify'))
    args = parser.parse_args()
    preparing = args.phase in {'prepare', 'prepare-continuity'}
    if preparing and (not args.expected_release or not re.fullmatch(r'r-[0-9a-f]{24}', args.expected_release)):
        parser.error('prepare requires --expected-release r-<24hex>')
    if not preparing and args.expected_release:
        parser.error('Expected release must be bound at preparation, never supplied during verification')
    release = p.guest_environment()
    guard()
    try:
        if args.phase == 'prepare':
            result = prepare(args, release)
        elif args.phase == 'prepare-continuity':
            result = prepare_continuity(args, release)
        elif args.phase == 'watch':
            if p.read(p.run_directory() / 'before.json').get('scenario') != SCENARIO:
                raise RuntimeError('Watch requires this exact prepared naming cycle')
            p.watch(args)
            return
        else:
            result = verify(release)
        print(json.dumps(result, indent=2))
    except Exception as error:
        directory = getattr(args, 'run_directory', None)
        if directory is None and (p.ROOT / 'active.json').exists():
            candidate = p.run_directory()
            if (candidate / 'before.json').is_file() and p.read(candidate / 'before.json').get('scenario') == SCENARIO:
                directory = candidate
        if directory is not None:
            p.write(directory / ('names-failure-' + args.phase + '.json'),
                    {'error': type(error).__name__ + ': ' + str(error), 'time': time.time()})
        raise


if __name__ == '__main__':
    main()
