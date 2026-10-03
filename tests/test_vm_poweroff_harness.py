from __future__ import annotations

import copy
import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_poweroff_test_module', HERE / 'run_vm_poweroff.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class PoweroffHarnessTests(unittest.TestCase):
    def test_prepare_rejects_active_startup_before_capture_or_mutation(self):
        from workspace_state import cli, login_status, operations, startup
        context = operations.OperationContext('test-boot', 'test-login', 'test-operation', 'startup', 1, 100)
        for operation, stage in [('running', 'ready'), ('preparing', 'ready'), ('completed', 'pending'),
                                 ('completed', 'running'), ('failed', 'waiting')]:
            with self.subTest(operation=operation, stage=stage), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                status_path = root / 'status.json'
                harness.write(status_path, {'mode': 'startup', 'session_id': 'test-login',
                    'operation_id': 'test-operation', 'operation_context': context.to_dict(),
                    'operation_state': operation, 'stages': [{'id': 'browsers', 'state': stage}]})
                with patch.object(harness, 'ROOT', root / 'poweroff'), \
                     patch.object(login_status, 'status_path', return_value=status_path), \
                     patch.object(cli, '_login_generation_file', return_value='test-login'), \
                     patch.object(harness, 'boot', return_value='test-boot'), \
                     patch.object(harness, 'capture') as capture, \
                     patch.object(harness, 'evolve') as evolve, \
                     patch.object(harness.f, 'run') as run, \
                     patch.object(startup, 'write_stage_marker') as marker:
                    with self.assertRaisesRegex(RuntimeError, 'Startup must reach a terminal operation'):
                        harness.prepare(argparse.Namespace(new_run=False))
                capture.assert_not_called()
                evolve.assert_not_called()
                run.assert_not_called()
                marker.assert_not_called()
                self.assertFalse((root / 'poweroff').exists())

    def full_fixture(self, extras=()):
        from workspace_state.social_apps import APPS, App
        apps = (*APPS, *(App(name, name, (name,), (name,), (name,)) for name in extras))
        identities = [f'uuid-{index}' for index in range(23)]
        snapshot = {
            'terminals': [{} for _ in range(6)],
            'sessions': [{'name': f'scale-{index}', 'windows': [{'panes': [
                {'codex': {'session_id': identity}} for identity in identities[index::10]]}]}
                for index in range(10)],
            'browsers': {'google_chrome': {'profiles': [{'windows': [
                {'tabs': [{} for _ in range(6)], 'groups': [{}] if index < 3 else []}
                for index in range(7)]}]}},
            'file_manager': {'windows': [{} for _ in range(4)]},
            'vscode': {'windows': [{}]},
            'social_apps': {app.id: {'running': True, 'mode': 'windowed', 'windows': [{}]} for app in apps},
        }
        native = []
        for name, count in [('Alacritty', 6), ('google-chrome', 7), ('nemo', 4),
                            ('com.microsoft.VSCode', 1), ('remote-viewer', 1),
                            *((app.aliases[0], 1) for app in apps)]:
            for _ in range(count):
                native.append({'id': len(native) + 1, 'wm_class': name, 'app_ids': [name]})
        return snapshot, {'windows': native}, identities, apps

    def test_fixture_guard_accepts_only_explicit_configured_extra_applications(self):
        from workspace_state import social_apps
        for extras in ((), ('chatgpt', 'remmina')):
            with self.subTest(extras=extras):
                snapshot, shell, identities, apps = self.full_fixture(extras)
                with patch.object(social_apps, 'configured_apps', return_value=apps):
                    result = harness.require_fixture_inventory(snapshot, shell, identities)
                self.assertEqual(result['native_windows'], 23 + len(extras))

    def test_fixture_guard_rejects_extra_terminal_and_unmanaged_firefox(self):
        from workspace_state import social_apps
        for extra in ('Alacritty', 'firefox'):
            with self.subTest(extra=extra):
                snapshot, shell, identities, apps = self.full_fixture(('chatgpt', 'remmina'))
                shell['windows'].append({'id': 99, 'wm_class': extra, 'app_ids': [extra]})
                with patch.object(social_apps, 'configured_apps', return_value=apps):
                    with self.assertRaises(RuntimeError):
                        harness.require_fixture_inventory(snapshot, shell, identities)

    def test_fixture_guard_rejects_duplicate_uuid_missing_app_and_double_claim(self):
        from workspace_state import social_apps
        from dataclasses import replace
        for corruption in ('duplicate-uuid', 'missing-app', 'double-claim', 'wrong-tab-count', 'extra-captured-terminal'):
            with self.subTest(corruption=corruption):
                snapshot, shell, identities, apps = self.full_fixture(('chatgpt',))
                if corruption == 'duplicate-uuid':
                    snapshot['sessions'][0]['windows'][0]['panes'][0]['codex']['session_id'] = identities[1]
                elif corruption == 'missing-app':
                    snapshot['social_apps']['chatgpt']['windows'] = []
                elif corruption == 'double-claim':
                    apps = (*apps[:-1], replace(apps[-1], aliases=('remote-viewer',)))
                elif corruption == 'wrong-tab-count':
                    snapshot['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'].append({})
                else:
                    snapshot['terminals'].append({})
                with patch.object(social_apps, 'configured_apps', return_value=apps):
                    with self.assertRaises(RuntimeError):
                        harness.require_fixture_inventory(snapshot, shell, identities)

    def recipe(self):
        placement = {'workspace': 0, 'monitor': 0, 'state': 'normal',
                     'monitor_identity': {'connector': 'Virtual-1', 'edid_hash': 'display'},
                     'geometry': {'x': 1, 'y': 1, 'width': 800, 'height': 600}}
        return {'created_at': 'old', 'desktop': {'workspace_names': ['One']},
                'sessions': [{'name': 'scale-02', 'windows': [{'index': 0, 'name': 'work',
                    'panes': [{'index': 0, 'cwd': '/tmp', 'label': 'owned', 'codex': {'session_id': 'uuid', 'pid': 42}}]}]}],
                'terminals': [{'session': 'scale-02', 'placement': placement}],
                'social_apps': {'chatgpt': {'mode': 'windowed', 'running': True, 'windows': [placement]},
                                'remmina': {'mode': 'windowed', 'running': True, 'windows': [placement]}},
                'browsers': {'google_chrome': {'profiles': [{'profile': 'Default', 'windows': [{
                    'type': 'normal', 'workspace_index': 0, 'monitor': {'index': 0, **placement['monitor_identity']},
                    'state': 'normal', 'geometry': placement['geometry'],
                    'groups': [{'id': 'group-1', 'title': 'Group', 'color': 'blue', 'collapsed': False}],
                    'tabs': [{'url': 'about:blank#a', 'group': 'group-1', 'active': True, 'pinned': False}],
                }]}]}}}

    def test_comparison_ignores_capture_metadata_but_checks_names_groups_and_added_apps(self):
        expected = self.recipe()
        actual = copy.deepcopy(expected)
        actual['created_at'] = 'new'
        actual['category_provenance'] = {'any': 'new'}
        actual['sessions'][0]['windows'][0]['panes'][0]['codex']['pid'] = 123
        self.assertEqual(harness.differences(expected, actual), [])
        changes = [
            lambda value: value['sessions'][0]['windows'][0].update(name='changed'),
            lambda value: value['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0].update(url='about:blank#wrong'),
            lambda value: value['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0].update(group=None),
            lambda value: value['social_apps']['remmina']['windows'].clear(),
            lambda value: value['social_apps']['chatgpt']['windows'].append(copy.deepcopy(value['social_apps']['chatgpt']['windows'][0])),
        ]
        for change in changes:
            actual = copy.deepcopy(expected)
            change(actual)
            self.assertTrue(harness.differences(expected, actual))

    def test_evolve_rejects_socket_error_instead_of_treating_it_as_truthy_success(self):
        with patch.object(harness.f, 'observe_chrome', side_effect=[[], {'error': 'CDP failed'}]):
            with self.assertRaisesRegex(RuntimeError, 'evolution failed'):
                harness.evolve()

    def test_evolve_requires_measured_changes_not_declared_constants(self):
        windows = [{'id': i, 'groups': [], 'tabs': [{'id': i * 6 + j, 'url': 'about:blank',
                    'groupId': -1, 'index': j} for j in range(6)]} for i in range(7)]
        with patch.object(harness.f, 'observe_chrome', side_effect=[windows, {'netTabChange': 0}, windows]):
            with self.assertRaisesRegex(RuntimeError, 'measured browsing'):
                harness.evolve()

    def evidence(self, root):
        context = {'boot_id': 'old-boot', 'login_generation': 'old-login', 'operation_id': 'operation',
                   'mode': 'shutdown', 'attempt': 1, 'deadline': 500}
        status = {'mode': 'shutdown', 'operation_context': context, 'operation_id': 'operation',
                  'session_id': 'old-login', 'shutdown_action': 'poweroff', 'shutdown_origin': 'preflight',
                  'operation_state': 'prepared', 'overall_state': 'ready',
                  'started_at': datetime.fromtimestamp(95, timezone.utc).isoformat()}
        before = {'boot_id': 'old-boot', 'login_generation': 'old-login', 'prepared_at': 90}
        harness.write(root / 'watch-000001-status.json', status)
        base = {'schema_version': 1, 'operation_context': context, 'operation_id': 'operation', 'session_id': 'old-login'}
        rendered = {**base, 'rendered_at': datetime.fromtimestamp(100, timezone.utc).isoformat()}
        committed = {**base, 'committed_at': datetime.fromtimestamp(104, timezone.utc).isoformat()}
        worker = {**base, 'invocation_id': 'worker'}
        del worker['session_id']
        worker['login_generation'] = 'old-login'
        for name, value in [('shutdown-hud-rendered.json', rendered), ('shutdown-commit.json', committed),
                            ('shutdown-worker-complete.json', worker)]:
            harness.write(root / name, value)
        return before, base

    def test_requires_real_matching_render_worker_and_countdown_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, _ = self.evidence(root)
            self.assertEqual(harness.shutdown_evidence(root, before)['countdown_seconds'], 4)
            (root / 'shutdown-hud-rendered.json').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Missing real matching'):
                harness.shutdown_evidence(root, before)

    def test_durable_authorization_can_prove_deleted_commit_but_not_missing_render(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, base = self.evidence(root)
            (root / 'shutdown-commit.json').unlink()
            harness.write(root / 'shutdown-prepared.json', {**base, 'created_at': 104,
                          'invocation_id': 'worker', 'action': 'poweroff', 'origin': 'preflight'})
            evidence = harness.shutdown_evidence(root, before)
            self.assertFalse(evidence['commit_receipt_observed'])
            self.assertTrue(evidence['durable_authorization_observed'])

    def test_stale_operation_and_short_countdown_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, base = self.evidence(root)
            harness.write(root / 'shutdown-commit.json', {**base,
                          'committed_at': datetime.fromtimestamp(101, timezone.utc).isoformat()})
            with self.assertRaisesRegex(RuntimeError, 'three seconds'):
                harness.shutdown_evidence(root, before)
            self.evidence(root)
            worker = harness.read(root / 'shutdown-worker-complete.json')
            worker['operation_context']['attempt'] = 2
            harness.write(root / 'shutdown-worker-complete.json', worker)
            with self.assertRaisesRegex(RuntimeError, 'Missing real matching'):
                harness.shutdown_evidence(root, before)

    def test_native_inventory_counts_duplicate_and_extra_application_windows(self):
        shell = {'windows': [{'wm_class': 'Remmina', 'app_ids': ['org.remmina']}]}
        expected = harness.native_inventory(shell)
        shell['windows'].append(copy.deepcopy(shell['windows'][0]))
        self.assertNotEqual(harness.native_inventory(shell), expected)

    def test_native_viewer_placement_is_checked_even_outside_capture_categories(self):
        placement = self.recipe()['terminals'][0]['placement']
        expected = {'windows': [{'wm_class': 'remote-viewer', 'app_ids': ['remote-viewer'], **placement}]}
        actual = copy.deepcopy(expected)
        self.assertEqual(harness.native_placement_failures(expected, actual), [])
        actual['windows'][0]['workspace'] = 1
        self.assertTrue(harness.native_placement_failures(expected, actual))

    def test_completed_cancel_then_new_committed_transaction_is_explicitly_recorded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            before, _ = self.evidence(root)
            current = harness.read(root / 'watch-000001-status.json')
            harness.write(root / 'watch-000003-status.json', current)
            cancelled = copy.deepcopy(current)
            cancelled.update(operation_id='cancelled-operation', cancelled=True, operation_state='cancelled')
            cancelled['operation_context']['operation_id'] = 'cancelled-operation'
            harness.write(root / 'watch-000001-status.json', cancelled)
            evidence = harness.shutdown_evidence(root, before)
            self.assertEqual(evidence['cancelled_attempts'], [{'operation_id': 'cancelled-operation', 'state': 'cancelled'}])
            cancelled['operation_state'] = 'recovering'
            harness.write(root / 'watch-000001-status.json', cancelled)
            with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
                harness.shutdown_evidence(root, before)

    def test_host_guard_runs_before_environment_or_application_mutation(self):
        with patch.object(harness.socket, 'gethostname', return_value='working-desktop'), \
             patch.object(harness.f, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Refusing outside'):
                harness.guest_environment()
        run.assert_not_called()

    def test_ready_labels_cannot_hide_failed_provider_evidence(self):
        names = ('gnome', 'displays', 'tmux', 'terminals', 'codex', 'browsers', 'social-apps',
                 'file-manager', 'vscode', 'virtual-machines', 'workspace', 'gdrive', 'nextcloud',
                 'pdrive', 'warmup', 'login-finalization')
        verified = {'success': True, 'identity': {'state': 'verified'},
                    'placement': {'state': 'verified'}, 'content': {'state': 'verified'}}
        stages = [{'id': name, 'state': 'ready', 'provider_results': []} for name in names]
        mapping = {stage['id']: stage for stage in stages}
        mapping['virtual-machines'].update(state='skipped', message='No committed VM restore jobs', current=0, total=0)
        mapping['browsers']['provider_results'] = [copy.deepcopy(verified)]
        mapping['social-apps']['provider_results'] = [copy.deepcopy(verified), copy.deepcopy(verified)]
        status = {'mode': 'startup', 'operation_id': 'new', 'session_id': 'new-login',
                  'operation_context': {'boot_id': 'new-boot', 'mode': 'startup',
                                        'operation_id': 'new', 'login_generation': 'new-login'},
                  'operation_state': 'completed', 'started_at': '2026-01-01T00:00:00+00:00',
                  'stages': stages}
        before = {'login_generation': 'old-login', 'prepared_at': 1}
        with patch.object(harness, 'boot', return_value='new-boot'):
            self.assertEqual(harness.startup_failures(status, before, self.recipe(), vm_evidence={'present': False}), [])
            mapping['browsers']['provider_results'][0]['placement']['state'] = 'waiting'
            self.assertTrue(harness.startup_failures(status, before, self.recipe(), vm_evidence={'present': False}))

    def test_empty_committed_vm_skip_requires_exact_shutdown_and_restore_ownership(self):
        stage = {'state': 'skipped', 'current': 0, 'total': 0, 'message': 'No Windows VM was active at shutdown'}
        status = {'session_id': 'new-login'}
        before = {'boot_id': 'old-boot', 'login_generation': 'old-login', 'prepared_at': 100}
        evidence = {'present': True, 'restore_jobs': 0, 'document': {
            'entries': [], 'action': 'poweroff', 'operation_id': 'shutdown-op',
            'source_boot_id': 'old-boot', 'session_id': 'old-login', 'restored_boot_id': 'new-boot',
            'restored_login_generation': 'new-login', 'committed_at': 101, 'restored_at': 200,
        }}
        with patch.object(harness, 'boot', return_value='new-boot'):
            self.assertTrue(harness.verified_empty_vm_skip(stage, status, before, evidence, 'shutdown-op'))
            for key, value in [('entries', [{'profile': 'unexpected-job'}]), ('operation_id', 'other-operation'),
                               ('source_boot_id', 'other-boot'), ('session_id', 'other-login'),
                               ('restored_boot_id', 'old-boot'), ('restored_login_generation', 'stale-login')]:
                invalid = copy.deepcopy(evidence)
                invalid['document'][key] = value
                with self.subTest(key=key):
                    self.assertFalse(harness.verified_empty_vm_skip(stage, status, before, invalid, 'shutdown-op'))
            self.assertFalse(harness.verified_empty_vm_skip(stage, status, before, None, 'shutdown-op'))
            self.assertFalse(harness.verified_empty_vm_skip(dict(stage, current=1), status, before, evidence, 'shutdown-op'))
            self.assertFalse(harness.verified_empty_vm_skip(dict(stage, state='ready'), status, before, evidence, 'shutdown-op'))

    def test_vm_receipt_observer_validates_without_executing_restore(self):
        from workspace_state import shutdown_profiles
        with patch.object(shutdown_profiles, '_read_startup_restore', return_value=({'entries': []}, [])) as read, \
             patch.object(shutdown_profiles, 'restore_startup_profiles') as restore:
            self.assertEqual(harness.vm_restore_evidence(),
                             {'present': True, 'document': {'entries': []}, 'restore_jobs': 0})
        read.assert_called_once()
        restore.assert_not_called()

    def verify_with_unavailable_live_browser(self, root, *, expectation, regression, live_available=False):
        from workspace_state import storage
        original = self.recipe()
        expected = copy.deepcopy(original)
        expected['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0]['url'] = 'about:blank#later'
        before = {'boot_id': 'old-boot', 'installed_release': '/installed/release', 'expect': expectation,
                  'expected_digest': harness.digest(expected), 'manual_baseline_digest': harness.digest(original),
                  'native_inventory': [], 'login_generation': 'old-login', 'prepared_at': 1,
                  'synthetic_conversation_ids': ['uuid']}
        for name, value in [('before', before), ('expected', expected), ('manual-baseline', original),
                            ('shutdown-canonical', original if regression else expected), ('expected-native', {'windows': []})]:
            harness.write(root / (name + '.json'), value)
        with ExitStack() as stack:
            for target, value in [('run_directory', root), ('boot', 'new-boot'),
                                  ('shutdown_evidence', {'operation_id': 'shutdown-op'}),
                                  ('vm_restore_evidence', {'present': False})]:
                stack.enter_context(patch.object(harness, target, return_value=value))
            stack.enter_context(patch.object(harness, 'capture', return_value=expected,
                side_effect=None if live_available else RuntimeError('Native Restore pages gate blocks companion')))
            stack.enter_context(patch.object(harness.f, 'verify_running_companions', side_effect=RuntimeError('Companion readiness unavailable')))
            stack.enter_context(patch.object(harness.f, 'observe_chrome', side_effect=TimeoutError('Chrome observation unavailable')))
            stack.enter_context(patch.object(harness.f, 'shell', return_value={'windows': []}))
            stack.enter_context(patch.object(harness.f, 'wayland_login', return_value='new-login'))
            stack.enter_context(patch.object(storage, 'load', return_value=expected))
            stack.enter_context(redirect_stdout(io.StringIO()))
            args = argparse.Namespace(installed_release='/installed/release')
            if expectation == 'browser-retention-regression' and regression:
                harness.verify(args)
            else:
                with self.assertRaisesRegex(RuntimeError, 'verification failed'):
                    harness.verify(args)
        return harness.read(root / 'result.json')

    def test_negative_run_records_proven_overwrite_even_when_live_browser_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.verify_with_unavailable_live_browser(Path(temporary),
                expectation='browser-retention-regression', regression=True)
        self.assertFalse(result['passed'])
        self.assertEqual(result['outcome'], 'expected-regression')
        self.assertTrue(result['expected_failure_reproduced'])
        self.assertTrue(all(result['browser_regression_evidence'].values()))
        for check in ('live_capture', 'companions', 'chrome_capture'):
            self.assertIn(check, result['failures'])
            self.assertEqual(result['observations'][check]['state'], 'unavailable')
        self.assertIn('Restore pages gate', result['failures']['live_capture'][0]['error'])

    def test_candidate_cannot_pass_when_any_live_or_companion_probe_raises(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.verify_with_unavailable_live_browser(Path(temporary), expectation='pass', regression=False)
        self.assertFalse(result['passed'])
        self.assertEqual(result['outcome'], 'failed')
        self.assertFalse(result['expected_failure_reproduced'])
        self.assertIn('live_capture', result['failures'])

    def test_unavailable_browser_alone_does_not_prove_expected_regression(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.verify_with_unavailable_live_browser(Path(temporary),
                expectation='browser-retention-regression', regression=False)
        self.assertFalse(result['expected_failure_reproduced'])
        self.assertEqual(result['outcome'], 'failed')

    def test_final_verification_rechecks_fixture_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.verify_with_unavailable_live_browser(Path(temporary),
                expectation='pass', regression=False, live_available=True)
        self.assertFalse(result['passed'])
        self.assertIn('fixture_inventory', result['failures'])

    def test_failed_browser_gate_cannot_reuse_stale_original_ids(self):
        chrome = [{'id': 1, 'groups': [], 'tabs': [{'id': 1, 'url': 'about:blank', 'groupId': -1, 'index': 0}]}]
        identity = {'pid': 5, 'start_time': 10}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness.write(root / 'chrome-login-original.json', chrome)
            harness.write(root / 'chrome-launch.json', {'generation': 'new', 'pid': 5})
            for baseline_valid in (None, False, True):
                harness.write(root / 'chrome-observation-launch.json', {
                    'generation': 'new', 'process': identity, 'baseline_valid': baseline_valid})
                with patch.object(harness.f, 'ROOT', root), \
                     patch.object(harness.f, 'process_identity', return_value=identity):
                    failures = harness.native_browser_identity_failures(chrome)
                self.assertEqual(not failures, baseline_valid is True)

    def test_same_boot_verification_fails_before_any_live_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            harness.write(root / 'before.json', {'boot_id': 'same-boot'})
            harness.write(root / 'expected.json', self.recipe())
            with patch.object(harness, 'run_directory', return_value=root), \
                 patch.object(harness, 'boot', return_value='same-boot'), \
                 patch.object(harness, 'capture') as capture:
                with self.assertRaisesRegex(RuntimeError, 'real VM power-off'):
                    harness.verify(None)
            capture.assert_not_called()


if __name__ == '__main__':
    unittest.main()
