#!/usr/bin/python3
"""Observe retained-checkpoint upgrades without preparatory manual adoption.

Only the named disposable scale guest is allowed. ``prepare`` constructs a
synthetic old baseline, changes real browser tabs, and marks a measured refused
restore. The external operator then schedules the candidate and performs genuine
GNOME Power Off/cold boot. No phase calls ``wsctl save`` or adopts live state.
"""
from __future__ import annotations

import argparse
import copy
import json
import time
import uuid

import run_vm_poweroff as p


def retained_failures(original, expected, checkpoint):
    """The shutdown must actually retain intent and independently capture changes."""
    problems = []
    old = original['browsers']['google_chrome']
    latest = checkpoint['browsers']['google_chrome']
    observation = latest.get('latest_observation', {})
    semantic = lambda value: [x for x in p.content_items({'browsers': {'google_chrome': value}})
                              if x[0] == 'browsers']
    if semantic(old) != semantic(latest):
        problems.append('Shutdown did not retain the original browser recipe')
    if semantic(old) == semantic(expected['browsers']['google_chrome']):
        problems.append('No newer browser changes were measured')
    if observation.get('schema_version') not in (1, 2) or not observation.get('captured_at'):
        problems.append('No complete timestamped retained observation')
    if semantic(observation.get('browser_state', {})) != semantic(expected['browsers']['google_chrome']):
        problems.append('Retained observation does not preserve the newer exact browser catalog')
    return problems


def omit_original_ungrouped_window(snapshot):
    """Construct six-window old intent without closing the seventh live window."""
    for profile in snapshot['browsers']['google_chrome']['profiles']:
        for index, window in enumerate(profile['windows']):
            if not window.get('groups'):
                removed = profile['windows'].pop(index)
                return {'profile': profile['profile'], 'id': removed['id'],
                        'tabs': len(removed['tabs']), 'live_window_closed': False}
    raise RuntimeError('Fixture has no ungrouped window to omit from old intent')


def require_viewer_fixture(native, placement):
    from workspace_state.provider_results import placement_frame_matches
    windows = [w for w in native['windows'] if w.get('wm_class') == 'remote-viewer']
    if len(windows) != 1 or not placement_frame_matches(windows[0], placement):
        raise RuntimeError('Align the synthetic viewer with its fixture configuration before preparation')


def prepare(args):
    from workspace_state import browser, cli, operations, storage
    from workspace_state.login_status import status_path
    from workspace_state.startup import StageMarker, write_stage_marker
    status = p.read(status_path())
    context = operations.OperationContext.from_dict(status.get('operation_context'))
    if context.mode != 'startup' or context.boot_id != p.boot() or not context.matches(status):
        raise RuntimeError('Current startup ownership is required')
    if any(s.get('state') in {'running', 'pending', 'waiting'} for s in status.get('stages', [])):
        raise RuntimeError('Wait for startup to settle before fixture preparation')
    directory = p.ROOT / uuid.uuid4().hex
    directory.mkdir(parents=True, mode=0o700)
    p.write(p.ROOT / 'active.json', {'run_id': directory.name})
    original = p.capture()
    native = p.f.shell()
    require_viewer_fixture(native, p.read(p.f.ROOT / 'viewer-placement.json'))
    graphical = p.graphical_launch_evidence()
    identities = p.read(p.f.ROOT / 'conversation-identities.json')
    p.require_fixture_inventory(original, native, identities)
    # Fixture construction deliberately excludes adoption metadata. It is not a
    # user save and must not prove the unrelated manual-save regression.
    original.pop('category_provenance', None)
    omitted = omit_original_ungrouped_window(original) if args.omit_original_ungrouped_window else None
    p.write(directory / 'fixture-construction.json', {'omitted_original_ungrouped_window': omitted,
            'live_window_mutation': False, 'manual_adoption': False})
    storage.save(original)
    p.write(directory / 'original.json', original)
    p.write(directory / 'evolution.json', p.evolve())
    grouped = copy.deepcopy(original['browsers']['google_chrome'])
    for profile in grouped['profiles']:
        profile['windows'] = [w for w in profile['windows'] if w.get('groups')]
    before = p.chrome_identity(p.f.observe_chrome())
    results, error = [], None
    try:
        results = browser.restore_browser(grouped, place=False, restore_token_prefix='upgrade-fault-' + directory.name)
    except browser.BrowserUnavailable as failure:
        error = str(failure)
    if not error and not any(not result.success for result in results):
        raise RuntimeError('Stale original grouped recipe unexpectedly succeeded')
    if 'group' not in (error or ' '.join(result.message for result in results)).lower():
        raise RuntimeError('Stale restore failed for an unrelated reason')
    p.write(directory / 'refused-restore.json', {'error': error, 'messages': [result.message for result in results]})
    if before != p.chrome_identity(p.f.observe_chrome()):
        raise RuntimeError('Refused restore mutated real browser windows, groups, or tabs')
    write_stage_marker(cli._startup_marker('browsers'), StageMarker(
        'browsers', 'failed', message='Measured synthetic stale grouped recipe refusal',
        operation_context=context.to_dict()))
    expected, native = p.capture(), p.f.shell()
    p.require_fixture_inventory(expected, native, identities)
    p.write(directory / 'expected.json', expected)
    p.write(directory / 'expected-native.json', native)
    p.write(directory / 'expected-chrome.json', p.f.observe_chrome())
    p.write(directory / 'before.json', {
        'scenario': 'retained-upgrade-without-manual-save', 'run_id': directory.name,
        'expect': 'pass', 'boot_id': p.boot(), 'login_generation': context.login_generation,
        'prepared_at': time.time(), 'installed_release': args.installed_release,
        'expected_digest': p.digest(expected), 'canonical_digest': p.digest(storage.load()),
        'native_inventory': p.native_inventory(native), 'synthetic_conversation_ids': identities,
        'manual_save_called': False, 'limitations': p.LIMITS,
        'graphical_launch': graphical, 'same_release_retry': args.same_release_retry,
    })
    print(json.dumps({'prepared': str(directory), 'manual_save_called': False,
                      'next': 'Schedule candidate; watch; genuine GNOME Power Off and cold boot'}))


def verify(args):
    from workspace_state import cli, storage
    from workspace_state.login_status import status_path
    directory = p.run_directory()
    before, expected = p.read(directory / 'before.json'), p.read(directory / 'expected.json')
    if before.get('scenario') != 'retained-upgrade-without-manual-save' or before['manual_save_called']:
        raise RuntimeError('This is not a no-manual-save upgrade fixture')
    if p.boot() == before['boot_id'] or p.digest(expected) != before['expected_digest']:
        raise RuntimeError('Real cold boot and unchanged expected evidence required')
    if args.installed_release == before['installed_release'] and not args.expect_failure and not args.same_release_retry:
        raise RuntimeError('Candidate upgrade did not activate')
    status = p.read(status_path())
    if status.get('operation_state') not in {'failed', 'completed'} or any(s.get('state') in {'running', 'pending', 'waiting'} for s in status.get('stages', [])):
        raise RuntimeError('Startup must settle before upgrade verification')
    shutdown = p.shutdown_evidence(directory, before)
    observed = p.capture()
    native, chrome, status = p.f.shell(), p.f.observe_chrome(), p.read(status_path())
    canonical = storage.load()
    for name, value in [('live', observed), ('native', native), ('chrome', chrome),
                        ('status', status), ('canonical', canonical)]:
        p.write(directory / ('upgrade-' + name + '.json'), value)
    boot = p.current_boot_evidence()
    checks = {
        'retained_checkpoint': retained_failures(p.read(directory / 'original.json'), expected,
                                                p.read(directory / 'shutdown-canonical.json')),
        'live': p.differences(expected, observed),
        'placement': p.native_placement_failures(p.read(directory / 'expected-native.json'), native),
        'inventory': [] if p.native_inventory(native) == before['native_inventory'] else ['Native inventory changed'],
        'browser_identity': p.native_browser_identity_failures(chrome),
        'qmp': p.qmp_exit_evidence(directory, before, boot)['failures'],
        'user_manager': p.user_manager_shutdown_evidence(before, boot)['failures'],
    }
    if before.get('graphical_launch'):
        graphical = p.graphical_shutdown_evidence(directory, before, boot)
        p.write(directory / 'verified-graphical_shutdown.json', graphical)
        checks['graphical_shutdown'] = graphical['failures']
        drain = p.graphical_drain_evidence(directory, before, boot, shutdown)
        p.write(directory / 'verified-graphical_drain.json', drain)
        checks['graphical_drain'] = drain['failures']
    elif not args.expect_failure:
        checks['graphical_evidence'] = ['No strict graphical launch/drain evidence; legacy run is not a full pass']
    stages = {s['id']: s for s in status['stages']}
    failed = stages.get('browsers', {}).get('state') == 'failed'
    if args.expect_failure:
        p.write(directory / 'negative-placement-differences.json', {'live': checks.pop('live'), 'placement': checks.pop('placement')})
        browser_content = lambda snapshot: sorted(json.dumps(item[1], sort_keys=True) for item in p.content_items(snapshot) if item[0] == 'browsers')
        checks['preserved_browser_content'] = [] if browser_content(expected) == browser_content(observed) else ['Negative startup changed browser content']
        checks['negative'] = [] if failed else ['Unfixed startup did not reproduce browser failure']
    else:
        marker_root = cli._startup_marker('browsers').parent
        receipt = p.read(marker_root / 'browser-reconciliation.json')
        p.write(directory / 'upgrade-reconciliation.json', receipt)
        from workspace_state.checkpoint import category_digest, migrate
        archived = p.read(directory / 'shutdown-canonical.json')
        checks['canonical_browser_preserved'] = [] if (category_digest(canonical, 'browsers') == category_digest(archived, 'browsers')
            and canonical['browsers']['google_chrome'] == archived['browsers']['google_chrome']) else ['Startup changed canonical browser recipe or observation']
        from workspace_state.browser_reconciliation import digest
        checks['source_digest'] = [] if receipt.get('source_checkpoint_digest') == digest(migrate(archived)) else ['Reconciliation source digest does not match the shutdown checkpoint']
        checks['reconciliation'] = [] if (receipt.get('state') == 'verified-reuse-only'
            and receipt.get('canonical_checkpoint_changed') is False
            and receipt.get('operation_context') == status.get('operation_context')) else ['Missing scoped reuse-only reconciliation proof']
        checks['startup'] = p.startup_failures(status, before, expected,
            vm_evidence=p.vm_restore_evidence(), shutdown_operation_id=shutdown['operation_id'])
    failures = {k: v for k, v in checks.items() if v}
    result = {'passed': not failures and not args.expect_failure,
              'harness_success': not failures, 'expected_failure_reproduced': args.expect_failure and not failures,
              'outcome': 'expected-regression' if args.expect_failure and not failures else 'passed' if not failures else 'failed',
              'expected_failure': args.expect_failure,
              'failures': failures, 'old_release': before['installed_release'],
              'new_release': args.installed_release, 'manual_save_called': False,
              'shutdown': shutdown, 'limitations': p.LIMITS}
    p.write(directory / 'upgrade-result.json', result)
    print(json.dumps(result))
    if failures:
        raise RuntimeError('Upgrade proof failed; see upgrade-result.json')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--disposable-guest', required=True, action='store_true')
    parser.add_argument('--expect-failure', action='store_true')
    parser.add_argument('--omit-original-ungrouped-window', action='store_true',
                        help='Construct old six-window intent while preserving all seven real windows')
    parser.add_argument('--same-release-retry', action='store_true',
                        help='Explicit retained replay on the already installed candidate, not a fresh upgrade')
    parser.add_argument('--timeout', type=float, default=120)
    parser.add_argument('phase', choices=('prepare', 'watch', 'verify'))
    args = parser.parse_args()
    args.installed_release = p.guest_environment()
    (p.watch if args.phase == 'watch' else globals()[args.phase])(args)


if __name__ == '__main__':
    main()
