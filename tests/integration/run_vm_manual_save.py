#!/usr/bin/python3
"""Verify three strict manual saves in the existing disposable desktop fixture.

Copy this file with run_vm_tmux_names.py, run_vm_poweroff.py, run_vm_scale.py and
vm_scale_fixture.py, then run inside the consented wsctl-validation KVM guest:

  python3 run_vm_manual_save.py --disposable-guest --expected-release r-<24hex>

The candidate must already be staged as an immutable desktop release. It may
be newer than the active release; this harness does not install or activate it.
Only the three explicit candidate `wsctl save` commands write checkpoints. The
independent observations use existing temporary identification leases, native
tmux queries and the fixture's read-only Chrome observer. No partial save,
shutdown, application restart, corrective move or HUD reset is performed.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

import run_vm_tmux_names as names

HERE = Path(__file__).resolve().parent
SCENARIO = 'three-strict-manual-saves-preserve-existing-desktop'


def write(directory: Path, name: str, value) -> None:
    path = directory / (name + '.json')
    temporary = directory / ('.' + name + '.tmp')
    with temporary.open('w') as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def marker_hashes() -> dict[str, str]:
    from workspace_state.login_status import runtime_root
    root = runtime_root()
    paths = [root / 'login-hud-status.json', root / 'current-operation.json',
             *(path for startup in root.glob('startup-*') if startup.is_dir()
               for path in startup.rglob('*.done'))]
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file()}


def native_ids(shell) -> list[tuple]:
    return sorted((window['id'], window.get('pid'), bool(window.get('active')))
                  for window in shell['windows'])


def tmux_layouts(native) -> list[dict]:
    return [{'session': session['name'], 'windows': [
        {'id': window['id'], 'index': window['index'], 'layout': window['layout'],
         'border_format': window['border_format'],
         'panes': [(pane['id'], pane['pid']) for pane in window['panes']]}
        for window in session['windows']]} for session in native]


def candidate_release(identifier: str, active: Path) -> Path:
    # guest_environment inserts the active module path. Bind this process to the
    # requested candidate before importing any production module instead.
    if any(key == 'workspace_state' or key.startswith('workspace_state.') for key in sys.modules):
        raise RuntimeError('Production modules were loaded before candidate selection')
    names.release_id(str(active))
    directory = active.parents[2] / identifier
    release = directory / 'components/workspace-state'
    if release.resolve() != release or not (release / 'bin/wsctl').is_file():
        raise RuntimeError('Requested immutable candidate release is missing or redirected')
    sys.path.insert(0, str(release / 'src'))
    from workspace_state import deployment
    if Path(deployment.__file__).resolve() != release / 'src/workspace_state/deployment.py':
        raise RuntimeError('Production imports do not belong to the requested candidate')
    verified = deployment.verify_release(directory)
    if verified['revision'] != identifier or deployment.build_fingerprint()['revision'] != identifier:
        raise RuntimeError('Candidate release identity does not match its immutable package')
    return release


def verify(release: Path, active: Path, directory: Path) -> dict:
    from workspace_state import cli, storage
    from workspace_state.checkpoint import CAPTURE_CATEGORIES
    result = {'scenario': SCENARIO, 'release_used': str(release), 'active_release': str(active),
              'artifact_directory': str(directory), 'iterations': [], 'passed': False,
              'no_poweroff_or_app_restart': True, 'conversations_are_synthetic_workers': True,
              'limitations': ['Synthetic UUID workers do not exercise authenticated conversation services']}
    write(directory, 'results', result)
    try:
        original_markers = marker_hashes()
        native = names.p.f.shell()
        panes = names.native_tmux()
        raw_chrome = names.p.f.observe_chrome()
        chrome = names.p.chrome_identity(raw_chrome)
        write(directory, 'before-native', native)
        write(directory, 'before-panes', panes)
        write(directory, 'before-chrome', raw_chrome)
        write(directory, 'before-markers', original_markers)
        write(directory, 'checkpoint-before', storage.load())
        baseline = names.p.capture()
        identities = [pane['content']['uuid'] for session in panes
                      for window in session['windows'] for pane in window['panes']]
        inventory = names.p.require_fixture_inventory(baseline, native, identities)
        if marker_hashes() != original_markers:
            raise RuntimeError('HUD or restore markers changed during baseline observation')
        write(directory, 'before-capture', baseline)
        write(directory, 'fixture-inventory', inventory)
        for iteration in range(1, 4):
            started = time.monotonic()
            command = subprocess.run([str(release / 'bin/wsctl'), 'save'],
                                     capture_output=True, text=True, timeout=120)
            log = directory / f'save-{iteration}.log'
            log.write_text(command.stdout + command.stderr)
            log.chmod(0o600)
            row = {'iteration': iteration, 'returncode': command.returncode,
                   'seconds': round(time.monotonic() - started, 3), 'passed': False, 'failures': []}
            result['iterations'].append(row)
            write(directory, 'results', result)
            if command.returncode:
                raise RuntimeError(f'Strict save {iteration} failed; see {log.name}')
            saved = storage.load()
            observed = names.p.capture()
            after_panes = names.native_tmux()
            after_native = names.p.f.shell()
            after_raw_chrome = names.p.f.observe_chrome()
            after_chrome = names.p.chrome_identity(after_raw_chrome)
            after_markers = marker_hashes()
            failures = names.p.differences(baseline, saved) + names.p.differences(baseline, observed)
            failures += names.p.native_placement_failures(native, after_native)
            if native_ids(native) != native_ids(after_native):
                failures.append('Native window IDs, processes or active selection changed')
            if names.snapshot_intent(saved) != names.intent(panes, geometry=False):
                failures.append('Saved literal names, policies or UUID positions differ from native oracle')
            if names.intent(panes) != names.intent(after_panes) or tmux_layouts(panes) != tmux_layouts(after_panes):
                failures.append('Native tmux names, policies, IDs, layout, geometry or selection changed')
            if chrome != after_chrome:
                failures.append('Chrome window/tab/group IDs, URLs, order or group metadata changed')
            if original_markers != after_markers:
                failures.append('HUD, current operation or restore completion markers changed')
            records = saved.get('category_provenance', {})
            if set(records) != set(CAPTURE_CATEGORIES) or any(
                    not isinstance(item, dict) or item.get('source') != 'manual-save'
                    or item.get('warnings') or item.get('retained_reason') or not item.get('adoption')
                    for item in records.values()):
                failures.append('Save lacks clean full manual adoption provenance for every category')
            row.update(counts=cli._counts(saved), native_window_count=len(after_native['windows']),
                       chrome_groups=sum(len(window['groups']) for window in after_chrome),
                       failures=failures, passed=not failures)
            for label, value in [('saved', saved), ('observed', observed), ('native', after_native),
                                 ('panes', after_panes), ('chrome', after_raw_chrome), ('markers', after_markers)]:
                write(directory, f'{label}-{iteration}', value)
            write(directory, 'results', result)
            if failures:
                raise RuntimeError(f'Save {iteration} changed observed state; see results.json')
        result['passed'] = True
        write(directory, 'results', result)
        return result
    except Exception as error:
        result['error'] = type(error).__name__ + ': ' + str(error)
        write(directory, 'results', result)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--disposable-guest', action='store_true', required=True)
    parser.add_argument('--expected-release', required=True, help='Exact staged immutable candidate r-<24hex>')
    args = parser.parse_args()
    if not re.fullmatch(r'r-[0-9a-f]{24}', args.expected_release):
        parser.error('--expected-release must be r-<24hex>')
    if Path(names.__file__).resolve().parent != HERE or names.p.f.HERE != HERE:
        raise RuntimeError('Integration helpers must come from the supplied fixture directory')
    active = Path(names.p.guest_environment())
    names.guard()
    release = candidate_release(args.expected_release, active)
    parent = names.p.f.ROOT / 'manual-save'
    if parent.is_symlink():
        raise RuntimeError('Refusing a redirected manual-save evidence directory')
    parent.mkdir(mode=0o700, exist_ok=True)
    parent.chmod(0o700)
    identifier = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:12]
    directory = parent / identifier
    directory.mkdir(mode=0o700)
    try:
        result = verify(release, active, directory)
    except Exception as error:
        print(json.dumps({'passed': False, 'artifact_directory': str(directory),
                          'error': type(error).__name__ + ': ' + str(error)}))
        return 1
    print(json.dumps({'passed': True, 'release': args.expected_release,
                      'artifact_directory': str(directory), 'iterations': result['iterations']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
