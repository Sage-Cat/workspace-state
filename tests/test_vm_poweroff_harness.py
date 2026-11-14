from __future__ import annotations

import copy
import argparse
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_poweroff_test_module', HERE / 'run_vm_poweroff.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class JournalRecordsTests(unittest.TestCase):
    def failure(self, returncode, stdout='', stderr=''):
        import subprocess
        return subprocess.CalledProcessError(returncode, ['journalctl'], stdout, stderr)

    def test_grep_without_matches_is_an_empty_result(self):
        with patch.object(harness.f, 'run', side_effect=self.failure(1)):
            self.assertEqual(harness.journal_records('ab-cd', '_PID=1', grep='Reached'), [])

    def test_real_failures_still_raise(self):
        import subprocess
        for returncode, stderr, grep in ((1, 'sudo: a password is required', 'Reached'),
                                         (1, '', None), (2, '', 'Reached')):
            with patch.object(harness.f, 'run', side_effect=self.failure(returncode, stderr=stderr)), \
                 self.assertRaises(subprocess.CalledProcessError):
                harness.journal_records('ab-cd', '_PID=1', grep=grep)

    def test_no_match_exit_with_output_still_raises(self):
        import subprocess
        with patch.object(harness.f, 'run', side_effect=self.failure(1, stdout='{"MESSAGE": "a"}')), \
             self.assertRaises(subprocess.CalledProcessError):
            harness.journal_records('ab-cd', '_PID=1', grep='Reached')

    def test_records_are_parsed_and_the_boot_is_exact(self):
        with patch.object(harness.f, 'run', return_value='{"MESSAGE": "a"}\n\n{"MESSAGE": "b"}') as run:
            self.assertEqual(harness.journal_records('AB-CD', '_PID=1'), [{'MESSAGE': 'a'}, {'MESSAGE': 'b'}])
        self.assertIn('--boot=abcd', run.call_args.args)
        self.assertEqual(run.call_args.args[-1], '_PID=1')


class PoweroffHarnessTests(unittest.TestCase):
    def catalog_probe_fixture(self):
        catalog = {'profiles': [{'profile': 'Default', 'windows': [
            {'id': 'grouped', 'groups': [{'id': 4}]}, {'id': 'single', 'groups': []}]}]}
        live = [{'id': 8, 'groups': [{'id': 4, 'title': 'Work', 'color': 'blue', 'collapsed': False}],
                 'tabs': [{'id': 20, 'url': 'https://example.test/changed', 'groupId': 4, 'index': 0}]}]
        return catalog, live

    def test_stale_catalog_probe_exercises_ungrouped_refusal_with_entire_catalog(self):
        from workspace_state import browser
        catalog, live = self.catalog_probe_fixture()
        original = copy.deepcopy(catalog)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'test-run'
            with patch.object(harness.f, 'observe_chrome', side_effect=[live, live]), \
                 patch.object(browser, 'restore_browser', side_effect=browser.BrowserUnavailable(
                     'Original Chrome window identity is unresolved: existing tabs may have changed.')) as restore:
                receipt = harness.stale_catalog_probe(root, catalog, 'pass')
            self.assertTrue(receipt['refused'])
            self.assertTrue(receipt['browser_unchanged'])
            self.assertTrue(receipt['ungrouped_identity_refusal'])
            self.assertEqual(harness.read(root / 'stale-catalog-probe.json'), json.loads(json.dumps(receipt)))
            self.assertEqual([w['id'] for w in restore.call_args.args[0]['profiles'][0]['windows']],
                             ['single', 'grouped'])
            self.assertEqual(restore.call_args.kwargs,
                             {'place': False, 'restore_token_prefix': 'catalog-fault-injection-test-run'})
            self.assertEqual(catalog, original)

    def test_stale_catalog_probe_rejects_duplicates_or_unrelated_refusal(self):
        from workspace_state import browser
        catalog, live = self.catalog_probe_fixture()
        for mode in ('duplicate', 'tab-change', 'group-change', 'unrelated', 'success'):
            after = copy.deepcopy(live)
            failure = browser.BrowserUnavailable('Original Chrome window identity is unresolved')
            if mode == 'duplicate':
                after.append({**copy.deepcopy(live[0]), 'id': 9})
            elif mode == 'tab-change':
                after[0]['tabs'][0]['url'] = 'https://example.test/replaced'
            elif mode == 'group-change':
                after[0]['groups'][0]['id'] = 10
            elif mode == 'unrelated':
                failure = browser.BrowserUnavailable('Original grouped Chrome window does not match')
            else:
                failure = None
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with patch.object(harness.f, 'observe_chrome', side_effect=[live, after]), \
                     patch.object(browser, 'restore_browser', side_effect=failure,
                                  return_value=[browser.BrowserRestoreResult('restored')]):
                    with self.assertRaises(RuntimeError):
                        harness.stale_catalog_probe(root, catalog, 'pass')
                receipt = harness.read(root / 'stale-catalog-probe.json')
                self.assertFalse(receipt['browser_unchanged'] if mode in {'duplicate', 'tab-change', 'group-change'}
                                 else receipt['ungrouped_identity_refusal'])

    def test_old_negative_explicitly_skips_stale_catalog_probe_without_browser_calls(self):
        from workspace_state import browser
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(harness.f, 'observe_chrome') as observe, \
                 patch.object(browser, 'restore_browser') as restore:
                receipt = harness.stale_catalog_probe(root, {}, 'browser-retention-regression')
            self.assertTrue(receipt['skipped'])
            self.assertEqual(harness.read(root / 'stale-catalog-probe.json'), receipt)
            observe.assert_not_called()
            restore.assert_not_called()

    def os_shutdown_records(self):
        return [{'_BOOT_ID': 'oldboot', '_PID': '1', 'UNIT': 'user@1000.service',
                 '__REALTIME_TIMESTAMP': '110000000', 'JOB_TYPE': 'stop', 'JOB_RESULT': 'done',
                 'MESSAGE': 'Stopped user@1000.service - User Manager for UID 1000.'}]

    def os_evidence(self, records):
        with patch.object(harness.os, 'getuid', return_value=1000), \
             patch.object(harness.f, 'run', return_value='\n'.join(json.dumps(item) for item in records)) as run:
            result = harness.user_manager_shutdown_evidence(
                {'boot_id': 'old-boot', 'prepared_at': 100}, {'boot_id': 'new-boot', 'started_at': 200})
        run.assert_called_once_with('sudo', '-n', 'journalctl', '--boot=oldboot', '_PID=1',
                                    'UNIT=user@1000.service', '--output=json', '--no-pager',
                                    '--lines=2000', timeout=15)
        return result

    def test_os_shutdown_requires_exact_previous_boot_clean_service_end(self):
        self.assertTrue(self.os_evidence(self.os_shutdown_records())['verified'])
        for change in ('missing', 'foreign-boot', 'old-stop', 'future-stop', 'app-only'):
            records = self.os_shutdown_records()
            if change == 'missing':
                records = []
            elif change == 'foreign-boot':
                records[0]['_BOOT_ID'] = 'unrelated'
            elif change == 'old-stop':
                records[0]['__REALTIME_TIMESTAMP'] = '90000000'
            elif change == 'future-stop':
                records[0]['__REALTIME_TIMESTAMP'] = '201000000'
            else:
                records[0].update(_PID='999', UNIT='slack.service')
            with self.subTest(change=change):
                self.assertFalse(self.os_evidence(records)['verified'])

    def test_stopped_job_cannot_hide_user_manager_timeout_or_signal_exit(self):
        for failure in ({'UNIT_RESULT': 'timeout', 'MESSAGE': "Failed with result 'timeout'."},
                        {'EXIT_CODE': 'killed', 'EXIT_STATUS': '9', 'MESSAGE': 'Main process exited'},
                        {'EXIT_CODE': 'exited', 'EXIT_STATUS': '1'},
                        {'MESSAGE': 'user@1000.service: State stop-sigterm timed out. Killing.'},
                        {'MESSAGE': 'Killing process 123 (bash) with signal SIGKILL.'}):
            with self.subTest(failure=failure):
                records = self.os_shutdown_records()
                records.insert(0, {**records[0], 'JOB_TYPE': None, 'JOB_RESULT': None,
                                   '__REALTIME_TIMESTAMP': '109000000', **failure})
                result = self.os_evidence(records)
                self.assertFalse(result['verified'])
                self.assertEqual(len(result['failure_records']), 1)

    def qmp_receipt(self):
        return {'vm_name': 'wsctl-ubuntu-validation', 'run_id': 'run', 'previous_boot_id': 'old-boot',
                'qmp_eof': True, 'systemd_state': 'ActiveState=inactive\nSubState=dead',
                'observed_at': datetime.fromtimestamp(111, timezone.utc).isoformat(),
                'events': [{'event': 'SHUTDOWN', 'data': {'guest': True, 'reason': 'guest-shutdown'},
                            'timestamp': {'seconds': 110, 'microseconds': 500000}}]}

    def qmp_evidence(self, root, receipt):
        if receipt is not None:
            harness.write(root / 'qmp-exit.json', receipt)
        return harness.qmp_exit_evidence(root, {'run_id': 'run', 'boot_id': 'old-boot', 'prepared_at': 100},
                                         {'boot_id': 'new-boot', 'started_at': 200})

    def test_qmp_exit_requires_matching_host_guest_shutdown_and_eof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                self.qmp_evidence(root, None)
            self.assertTrue(self.qmp_evidence(root, self.qmp_receipt())['verified'])
            for key, value in [('vm_name', 'another-vm'), ('run_id', 'another-run'),
                               ('previous_boot_id', 'another-boot'), ('qmp_eof', False),
                               ('qmp_eof', 'true'), ('systemd_state', ''), ('events', [])]:
                invalid = self.qmp_receipt()
                invalid[key] = value
                with self.subTest(key=key, value=value):
                    self.assertFalse(self.qmp_evidence(root, invalid)['verified'])

    def test_qmp_exit_rejects_host_quit_and_stale_or_new_boot_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for change in ('host-quit', 'reboot', 'stale', 'new-boot', 'bad-micros', 'observed-before', 'naive-time', 'duplicate'):
                invalid = self.qmp_receipt()
                event = invalid['events'][0]
                if change == 'host-quit':
                    event['data']['guest'] = False
                elif change == 'reboot':
                    event['data']['reason'] = 'guest-reset'
                elif change in {'stale', 'new-boot'}:
                    event['timestamp']['seconds'] = 99 if change == 'stale' else 200
                elif change == 'bad-micros':
                    event['timestamp']['microseconds'] = 1000000
                elif change == 'observed-before':
                    invalid['observed_at'] = datetime.fromtimestamp(109, timezone.utc).isoformat()
                elif change == 'naive-time':
                    invalid['observed_at'] = '1970-01-01T00:01:51'
                else:
                    invalid['events'].append(copy.deepcopy(event))
                with self.subTest(change=change):
                    self.assertFalse(self.qmp_evidence(root, invalid)['verified'])

    def test_abrupt_cycle_may_end_by_a_host_quit_but_never_a_reset(self):
        before = {'run_id': 'run', 'boot_id': 'old-boot', 'prepared_at': 100, 'abrupt': True}
        boot = {'boot_id': 'new-boot', 'started_at': 200}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            receipt = self.qmp_receipt()
            receipt['events'][0]['data'].update(guest=False, reason='host-qmp-quit')
            harness.write(root / 'qmp-exit.json', receipt)
            self.assertTrue(harness.qmp_exit_evidence(root, before, boot)['verified'])
            self.assertFalse(harness.qmp_exit_evidence(root, {**before, 'abrupt': False}, boot)['verified'])
            receipt['events'][0]['data'].update(guest=False, reason='guest-reset')
            harness.write(root / 'qmp-exit.json', receipt)
            self.assertFalse(harness.qmp_exit_evidence(root, before, boot)['verified'])

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
        one = [{'id': 1, 'groups': [], 'tabs': []}]
        with patch.object(harness, 'settled_chrome', return_value=one), \
             patch.object(harness.f, 'observe_chrome',
                          side_effect=[one, {'reloaded': []}, {'error': 'CDP failed'}]):
            with self.assertRaisesRegex(RuntimeError, 'evolution failed'):
                harness.evolve()

    def test_evolve_rejects_warm_up_error_before_settling_or_mutating(self):
        for reply in ({'error': 'reload failed'}, []):
            with self.subTest(reply=reply), \
                 patch.object(harness, 'settled_chrome') as settled, \
                 patch.object(harness.f, 'observe_chrome', side_effect=[[], reply]) as observe:
                with self.assertRaisesRegex(RuntimeError, 'Browser warm-up failed'):
                    harness.evolve()
                settled.assert_not_called()
                self.assertEqual([c.args for c in observe.call_args_list], [(), ('warm-synthetic',)])

    def test_evolve_rejects_warm_up_that_changes_fixture_identities(self):
        settled = self.settled_fixture()
        pre = copy.deepcopy(settled)
        pre[0]['tabs'][0]['url'] = 'http://127.0.0.1:18765/scale/other'
        with patch.object(harness, 'settled_chrome', return_value=settled) as settle, \
             patch.object(harness.f, 'observe_chrome', side_effect=[pre, {'reloaded': [1]}]) as observe:
            with self.assertRaisesRegex(RuntimeError, 'Browser warm-up changed fixture identities'):
                harness.evolve()
        settle.assert_called_once_with(timeout=60)
        self.assertEqual([c.args for c in observe.call_args_list], [(), ('warm-synthetic',)])

    def test_evolve_requires_measured_changes_not_declared_constants(self):
        windows = [{'id': i, 'groups': [], 'tabs': [{'id': i * 6 + j, 'url': 'about:blank',
                    'groupId': -1, 'index': j} for j in range(6)]} for i in range(7)]
        with patch.object(harness, 'settled_chrome', return_value=windows), \
             patch.object(harness.f, 'observe_chrome', side_effect=[windows, {'reloaded': []}, {'netTabChange': 0, 'expected_urls': []}]):
            with self.assertRaisesRegex(RuntimeError, 'measured browsing'):
                harness.evolve()

    def settled_fixture(self):
        return [{'id': i, 'groups': [], 'tabs': [
            {'id': i * 6 + j, 'url': f'http://127.0.0.1:18765/scale/{i}-{j}',
             'groupId': -1, 'index': j, 'pendingUrl': '', 'status': 'complete'} for j in range(6)]} for i in range(7)]

    def test_slow_replacement_waits_for_empty_or_old_valid_url_with_pending_navigation(self):
        for old_url in ('', 'http://127.0.0.1:18765/scale/old'):
            ready = self.settled_fixture()
            pending = copy.deepcopy(ready)
            tab = pending[0]['tabs'][0]
            url, tab['url'] = tab['url'], old_url
            tab.update(pendingUrl=url, status='loading')
            with self.subTest(old_url=old_url), \
                 patch.object(harness.f, 'observe_chrome', side_effect=[pending, pending, ready, ready]) as observe, \
                 patch.object(harness.time, 'sleep'):
                self.assertEqual(harness.settled_chrome(), ready)
                self.assertEqual(observe.call_count, 4)

    def test_legacy_controller_and_completed_wrong_mutation_cannot_pass_settling(self):
        ready = self.settled_fixture()
        intended = [{'id': t['id'], 'url': t['url']} for w in ready for t in w['tabs']]
        for change in ('legacy', 'wrong-url', 'incomplete-intent'):
            observed = copy.deepcopy(ready)
            expected = copy.deepcopy(intended)
            if change == 'legacy':
                del observed[0]['tabs'][0]['status']
            elif change == 'wrong-url':
                expected[0]['url'] += '-requested'
            else:
                expected.pop()
            with self.subTest(change=change), patch.object(harness.f, 'observe_chrome', return_value=observed):
                with self.assertRaises(RuntimeError):
                    harness.settled_chrome(expected_urls=expected)

    def test_settling_rejects_unknown_urls_changed_identity_and_pending_mismatch(self):
        for change in ('unknown', 'unknown-pending', 'identity', 'count', 'pending-mismatch'):
            before = self.settled_fixture()
            before[0]['tabs'][0].update(url='', pendingUrl='http://127.0.0.1:18765/scale/intended')
            after = self.settled_fixture()
            if change == 'unknown':
                before[0]['tabs'][0]['url'] = 'https://private.example/'
            elif change == 'unknown-pending':
                before[0]['tabs'][0]['pendingUrl'] = 'https://private.example/'
            elif change == 'identity':
                after[0]['tabs'][0]['id'] = 999
            elif change == 'count':
                after.append(copy.deepcopy(after[0]))
            with self.subTest(change=change), \
                 patch.object(harness.f, 'observe_chrome', side_effect=[before, after]), \
                 patch.object(harness.time, 'sleep'):
                with self.assertRaises(RuntimeError):
                    harness.settled_chrome()

    def test_settling_blank_url_deadline_prevents_mutation(self):
        pending = self.settled_fixture()
        pending[0]['tabs'][0]['url'] = ''
        with patch.object(harness.f, 'observe_chrome',
                          side_effect=[pending, {'reloaded': []}, pending]) as observe, \
             patch.object(harness.time, 'monotonic', side_effect=[0, 61]), \
             patch.object(harness.time, 'sleep') as sleep:
            with self.assertRaisesRegex(RuntimeError, 'bounded deadline'):
                harness.evolve()
            self.assertEqual([call.args for call in observe.call_args_list],
                             [(), ('warm-synthetic',), ()])
            sleep.assert_not_called()

    def test_evolve_settles_both_sides_before_measuring_replacement(self):
        before = self.settled_fixture()
        after = copy.deepcopy(before)
        after[0]['tabs'][0].update(id=999, url='http://127.0.0.1:18765/scale/replacement')
        pending = copy.deepcopy(after)
        pending[0]['tabs'][0].update(url='', status='loading',
                                   pendingUrl=after[0]['tabs'][0]['url'])
        reply = {'expected_urls': [{'id': t['id'], 'url': t['url']} for w in after for t in w['tabs']]}
        with patch.object(harness.f, 'observe_chrome',
                          side_effect=[before, {'reloaded': [1, 2]}, before, before, reply, pending, after, after]) as observe, \
             patch.object(harness.time, 'sleep'):
            result = harness.evolve()
        self.assertEqual(result['after'], after)
        self.assertEqual(result['measured']['tabs_replaced'], 1)
        self.assertEqual(result['warm'], {'reloaded': [1, 2]})
        self.assertEqual([call.args for call in observe.call_args_list],
                         [(), ('warm-synthetic',), (), (), ('evolve-synthetic',), (), (), ()])

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

    def test_host_guard_runs_before_environment_or_application_mutation(self):
        with patch.object(harness.socket, 'gethostname', return_value='working-desktop'), \
             patch.object(harness.f, 'run') as run:
            with self.assertRaisesRegex(RuntimeError, 'Refusing outside'):
                harness.guest_environment()
        run.assert_not_called()

    def test_ready_labels_cannot_hide_failed_provider_evidence(self):
        names = ('gnome', 'displays', 'tmux', 'terminals', 'codex', 'browsers', 'social-apps',
                 'file-manager', 'vscode', 'workspace', 'gdrive', 'nextcloud',
                 'pdrive', 'warmup', 'login-finalization')
        verified = {'success': True, 'identity': {'state': 'verified'},
                    'placement': {'state': 'verified'}, 'content': {'state': 'verified'}}
        stages = [{'id': name, 'state': 'ready', 'provider_results': []} for name in names]
        mapping = {stage['id']: stage for stage in stages}
        mapping['browsers']['provider_results'] = [copy.deepcopy(verified)]
        mapping['social-apps']['provider_results'] = [copy.deepcopy(verified), copy.deepcopy(verified)]
        status = {'mode': 'startup', 'operation_id': 'new', 'session_id': 'new-login',
                  'operation_context': {'boot_id': 'new-boot', 'mode': 'startup',
                                        'operation_id': 'new', 'login_generation': 'new-login'},
                  'operation_state': 'completed', 'started_at': '2026-01-01T00:00:00+00:00',
                  'stages': stages}
        before = {'login_generation': 'old-login', 'prepared_at': 1}
        with patch.object(harness, 'boot', return_value='new-boot'):
            self.assertEqual(harness.startup_failures(status, before, self.recipe()), [])
            mapping['browsers']['provider_results'][0]['placement']['state'] = 'waiting'
            self.assertTrue(harness.startup_failures(status, before, self.recipe()))

    def test_login_generation_is_scoped_to_boot_not_globally_unique(self):
        # D-Bus unique owner names may repeat when the bus restarts at boot.
        # Production authority identifies a login by both boot and generation.
        status = {'mode': 'startup', 'operation_id': 'new', 'session_id': 'same-generation',
                  'operation_context': {'boot_id': 'new-boot', 'mode': 'startup',
                                        'operation_id': 'new', 'login_generation': 'same-generation'},
                  'operation_state': 'completed', 'started_at': '2026-01-01T00:00:00+00:00',
                  'stages': []}
        before = {'boot_id': 'old-boot', 'login_generation': 'same-generation', 'prepared_at': 1}
        reason = 'Startup status is not from the new boot/login'
        with patch.object(harness, 'boot', return_value='new-boot'):
            self.assertNotIn(reason, harness.startup_failures(status, before, self.recipe()))
            before['boot_id'] = 'new-boot'
            self.assertIn(reason, harness.startup_failures(status, before, self.recipe()))
            before['boot_id'] = 'old-boot'
            status['operation_context']['boot_id'] = 'old-boot'
            self.assertIn(reason, harness.startup_failures(status, before, self.recipe()))

    def verify_with_unavailable_live_browser(self, root, *, expectation, regression, live_available=False,
                                             healthy=False, os_receipt=None, qmp_receipt=None,
                                             final_save=True, abrupt=False, snapshot_failures=None,
                                             periodic_failures=None, watch_failure=None, archived_extra=None):
        from workspace_state import login_status, storage
        original = self.recipe()
        expected = copy.deepcopy(original)
        expected['browsers']['google_chrome']['profiles'][0]['windows'][0]['tabs'][0]['url'] = 'about:blank#later'
        before = {'boot_id': 'old-boot', 'installed_release': '/installed/release', 'expect': expectation,
                  'method': 'abrupt' if abrupt else 'systemctl-poweroff', 'shutdown_files': [],
                  'expected_digest': harness.digest(expected), 'manual_baseline_digest': harness.digest(original),
                  'native_inventory': [], 'login_generation': 'old-login', 'prepared_at': 1,
                  'synthetic_conversation_ids': ['uuid']}
        for name, value in [('before', before), ('expected', expected), ('manual-baseline', original),
                            ('shutdown-canonical', original if regression else expected),
                            ('expected-native', {'windows': []}), ('startup-status', {}),
                            ('watch-shutdown-files', [])]:
            harness.write(root / (name + '.json'), value)
        with ExitStack() as stack:
            for target, value in [('run_directory', root), ('boot', 'new-boot'), ('shutdown_files', []),
                                  ('restored_snapshot', {**(original if regression else expected),
                                                         **(archived_extra or {})})]:
                stack.enter_context(patch.object(harness, target, return_value=value))
            stack.enter_context(patch.object(harness, 'capture', return_value=expected,
                side_effect=None if live_available or healthy else RuntimeError('Native Restore pages gate blocks companion')))
            stack.enter_context(patch.object(harness.f, 'verify_running_companions', return_value={},
                side_effect=None if healthy else RuntimeError('Companion readiness unavailable')))
            stack.enter_context(patch.object(harness.f, 'observe_chrome', return_value=[],
                side_effect=None if healthy else TimeoutError('Chrome observation unavailable')))
            stack.enter_context(patch.object(harness.f, 'shell', return_value={'windows': []}))
            stack.enter_context(patch.object(harness.f, 'wayland_login', return_value='new-login'))
            stack.enter_context(patch.object(harness, 'current_boot_evidence', return_value={
                'boot_id': 'new-boot', 'started_at': 200}))
            stack.enter_context(patch.object(harness.f, 'run', side_effect=RuntimeError('Guest journal unavailable')))
            stack.enter_context(patch.object(login_status, 'status_path', return_value=root / 'startup-status.json'))
            stack.enter_context(patch.object(harness.f, 'process_identity', return_value={'pid': 42, 'start_time': 100}))
            if healthy:
                for name, value in [('startup_failures', []), ('native_browser_identity_failures', []),
                                    ('require_fixture_inventory', {'verified': True})]:
                    stack.enter_context(patch.object(harness, name, return_value=value))
            if os_receipt is not None:
                stack.enter_context(patch.object(harness, 'user_manager_shutdown_evidence', return_value=os_receipt))
            if qmp_receipt is not None:
                stack.enter_context(patch.object(harness, 'qmp_exit_evidence', return_value=qmp_receipt))
            if healthy and final_save is not None:
                stack.enter_context(patch.object(harness, 'final_save_evidence', return_value=(
                    {'verified': True, 'failures': [], 'started_at': 0} if final_save is True else final_save)))
            if healthy:
                stack.enter_context(patch.object(harness, 'snapshot_source_failures',
                                                 return_value=snapshot_failures or []))
                stack.enter_context(patch.object(harness, 'periodic_checkpoint_evidence',
                                                 return_value=periodic_failures or []))
            if watch_failure:
                harness.write(root / 'failure-watch.json', watch_failure)
            stack.enter_context(patch.object(storage, 'load', return_value=expected))
            stack.enter_context(redirect_stdout(io.StringIO()))
            args = argparse.Namespace(installed_release='/installed/release')
            if ((expectation == 'browser-retention-regression' and regression) or
                    (healthy and os_receipt and os_receipt.get('verified') and
                     qmp_receipt and qmp_receipt.get('verified') and
                     (final_save is True or isinstance(final_save, dict) and final_save.get('verified')) and
                     not snapshot_failures and not periodic_failures and not watch_failure)):
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
        for check in ('live_capture', 'companions', 'chrome_capture', 'os_shutdown', 'final_save', 'host_qmp_exit'):
            self.assertIn(check, result['failures'])
            self.assertEqual(result['observations'][check]['state'], 'unavailable')
        self.assertIn('Restore pages gate', result['failures']['live_capture'][0]['error'])

    def test_green_desktop_cannot_hide_missing_or_failed_os_shutdown_evidence(self):
        good = {'verified': True, 'failures': []}
        failed = {'verified': False, 'failures': ['User manager stop timed out']}
        for os_receipt, qmp_receipt, failure in [(good, good, None), (failed, good, 'os_shutdown'),
                                               (good, None, 'host_qmp_exit')]:
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                result = self.verify_with_unavailable_live_browser(root, expectation='pass', regression=False,
                    healthy=True, os_receipt=os_receipt, qmp_receipt=qmp_receipt)
                self.assertEqual(result['passed'], failure is None)
                if failure:
                    self.assertEqual(set(result['failures']), {failure})
                if os_receipt is not None:
                    self.assertEqual(harness.read(root / 'verified-os_shutdown.json'), os_receipt)

    def test_green_desktop_requires_final_save_and_snapshot_source_proof(self):
        good = {'verified': True, 'failures': []}
        failed = {'verified': False, 'failures': ['Final save finished after the first application unit was stopped']}
        for check, kwargs in (('final_save', {'final_save': failed}),
                              ('snapshot_source', {'snapshot_failures': ['Terminals provenance source is auto-save']})):
            with self.subTest(check=check), tempfile.TemporaryDirectory() as temporary:
                result = self.verify_with_unavailable_live_browser(Path(temporary), expectation='pass',
                    regression=False, healthy=True, os_receipt=good, qmp_receipt=good, **kwargs)
                self.assertFalse(result['passed'])
                self.assertEqual(set(result['failures']), {check})

    def test_abrupt_cycle_skips_the_orderly_user_manager_stop_check(self):
        good = {'verified': True, 'failures': []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = self.verify_with_unavailable_live_browser(root, expectation='pass', regression=False,
                healthy=True, os_receipt=good, qmp_receipt=good, abrupt=True)
            self.assertTrue(result['passed'])
            self.assertFalse((root / 'verified-os_shutdown.json').exists())

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

class FinalSaveEvidenceTests(unittest.TestCase):
    # InhibitDelayMaxSec 20 gives the agent a 17 second budget.
    LOGIND = {'inhibit_delay_max_usec': 20_000_000,
              'inhibitors': [{'line': 'workspace-state 1000 u 5 py shutdown final save delay', 'mode': 'delay'}]}
    BEFORE = {'boot_id': 'old-boot', 'prepared_at': 100, 'method': 'systemctl-poweroff', 'logind': LOGIND}
    BOOT = {'started_at': 1000}

    def agent(self, message, at, **extra):
        return {'_BOOT_ID': 'oldboot', 'MESSAGE': message, '__MONOTONIC_TIMESTAMP': str(int(at * 1e6)),
                '__REALTIME_TIMESTAMP': str(int((500 + at) * 1e6)), **extra}

    def stop(self, at, message='Stopping app-gnome-Alacritty-1234.scope - Alacritty.'):
        return self.agent(message, at)

    def logind(self, message, at):
        return self.agent(message, at, _SYSTEMD_UNIT='systemd-logind.service', _PID='650')

    def pid1(self, message, at):
        return self.agent(message, at, _PID='1')

    def clean(self, **change):
        records = {'started': self.agent('Started wsctl-gnome-session.service', 1),
                   'start': self.agent('wsctl: final save started; trigger=prepare-for-shutdown; budget=17.0s', 10),
                   'end': self.agent('wsctl: final save finished; rc=0; elapsed=2.5s', 12.5),
                   'lock': self.agent('wsctl: logind delay lock released', 12.6),
                   'stop': self.stop(13), 'orderly': self.logind('System is powering down.', 14)}
        records.update(change)
        return records

    def judge(self, records, *, abrupt=False, **before):
        agent = [value for key, value in records.items()
                 if key not in ('stop', 'orderly') and value is not None]
        stops = [records['stop']] if records.get('stop') else []
        system = [records['orderly']] if records.get('orderly') else []
        return harness.final_save_failures({**self.BEFORE, **before}, self.BOOT, agent, stops, system,
                                           abrupt=abrupt)

    def test_clean_cycle_passes_with_one_finished_save_before_the_first_application_stop(self):
        result = self.judge(self.clean())
        self.assertEqual(result['failures'], [])
        self.assertTrue(result['verified'])
        self.assertEqual((result['trigger'], result['budget'], result['elapsed']), ('prepare-for-shutdown', 17.0, 2.5))
        self.assertEqual(result['method'], 'systemctl-poweroff')

    def test_end_session_trigger_is_accepted_for_the_gnome_menu(self):
        records = self.clean(start=self.agent('wsctl: final save started; trigger=end-session; budget=17.0s', 10),
                             gnome=self.agent('wsctl: GNOME EndSession', 9.9))
        self.assertTrue(self.judge(records, method='gnome-menu')['verified'])

    def test_method_must_match_the_observed_trigger(self):
        self.assertIn('The GNOME menu method logged no GNOME EndSession',
                      self.judge(self.clean(), method='gnome-menu')['failures'])
        late = self.clean(start=self.agent('wsctl: final save started; trigger=end-session; budget=17.0s', 10))
        for method in ('systemctl-poweroff', 'sudo-shutdown'):
            self.assertTrue(any('prepare-for-shutdown' in failure
                                for failure in self.judge(late, method=method)['failures']))
        self.assertTrue(self.judge(self.clean(), method='sudo-shutdown')['verified'])

    def test_cancel_end_session_fails(self):
        records = self.clean()
        records['cancel'] = self.agent('wsctl: GNOME CancelEndSession; cancelled by user', 11)
        self.assertTrue(any('CancelEndSession' in failure for failure in self.judge(records)['failures']))

    def test_save_finished_after_the_first_application_stop_fails(self):
        result = self.judge(self.clean(stop=self.stop(11, 'Stopping wsctl-app-chrome-Default-1.service')))
        self.assertIn('Final save finished after the first application unit was stopped', result['failures'])

    def test_every_application_unit_kind_counts_as_the_first_stop(self):
        for message in ('Stopping app-gnome-x-1.scope', 'Stopping wsctl-app-chrome-1.service',
                        'Stopping tmux-spawn-1234.scope', 'Stopping snap.firefox.firefox-1.scope',
                        'Stopping org.gnome.Shell@wayland.service'):
            with self.subTest(message=message):
                result = self.judge(self.clean(stop=self.stop(11, message)))
                self.assertIn('Final save finished after the first application unit was stopped',
                              result['failures'])

    def test_missing_application_stop_cannot_prove_order(self):
        self.assertIn('No application unit stop was observed after preparation',
                      self.judge(self.clean(stop=None))['failures'])

    def test_unrelated_stopping_lines_are_not_application_stops(self):
        result = self.judge(self.clean(stop=self.stop(11, 'Stopping dbus.service - D-Bus User Message Bus.')))
        self.assertIn('No application unit stop was observed after preparation', result['failures'])

    def test_logged_budget_must_follow_the_logind_delay(self):
        start = self.agent('wsctl: final save started; trigger=prepare-for-shutdown; budget=60.0s', 10)
        self.assertTrue(any('Logged budget' in failure for failure in self.judge(self.clean(start=start))['failures']))
        # A short delay lowers the budget, and the minimum is one second.
        for delay, budget in ((10_000_000, 7.0), (3_000_000, 1.0), (60_000_000, 20.0)):
            logind = {**self.LOGIND, 'inhibit_delay_max_usec': delay}
            start = self.agent(f'wsctl: final save started; trigger=prepare-for-shutdown; budget={budget}s', 10)
            with self.subTest(delay=delay):
                quick = self.clean(start=start, end=self.agent('wsctl: final save finished; rc=0; elapsed=0.5s', 10.5),
                                   lock=self.agent('wsctl: logind delay lock released', 10.6))
                self.assertTrue(self.judge(quick, logind=logind)['verified'])
        self.assertIn('Preparation recorded no logind InhibitDelayMaxUSec',
                      self.judge(self.clean(), logind=None)['failures'])

    def test_logged_elapsed_must_agree_with_the_journal_clock(self):
        end = self.agent('wsctl: final save finished; rc=0; elapsed=1.0s', 40)
        self.assertIn('Logged elapsed time disagrees with the journal timestamps',
                      self.judge(self.clean(end=end, lock=self.agent('wsctl: logind delay lock released', 40.1),
                                            stop=self.stop(41)))['failures'])

    def test_journal_gap_beyond_budget_plus_slack_fails_even_if_the_log_claims_less(self):
        end = self.agent('wsctl: final save finished; rc=0; elapsed=18.2s', 28.2)
        records = self.clean(end=end, lock=self.agent('wsctl: logind delay lock released', 28.3), stop=self.stop(29))
        self.assertIn('Final save elapsed time exceeded its budget', self.judge(records)['failures'])
        end = self.agent('wsctl: final save finished; rc=0; elapsed=18.0s', 28.0)
        records = self.clean(end=end, lock=self.agent('wsctl: logind delay lock released', 28.1), stop=self.stop(29))
        self.assertEqual(self.judge(records)['failures'], [])

    def test_deadline_stop_or_nonzero_rc_fails_an_unforced_cycle(self):
        end = self.agent('wsctl: final save stopped at deadline; elapsed=2.5s', 12.5)
        self.assertTrue(any('deadline' in failure for failure in self.judge(self.clean(end=end))['failures']))
        end = self.agent('wsctl: final save finished; rc=1; elapsed=2.5s', 12.5)
        self.assertTrue(any('non-zero' in failure for failure in self.judge(self.clean(end=end))['failures']))

    def test_exactly_one_start_and_one_end_are_required(self):
        records = self.clean(second=self.agent('wsctl: final save started; trigger=end-session; budget=17.0s', 11))
        self.assertTrue(any('exactly one final save start' in failure for failure in self.judge(records)['failures']))
        records = self.clean(second=self.agent('wsctl: final save finished; rc=0; elapsed=1.0s', 12.7))
        self.assertTrue(any('exactly one final save end' in failure for failure in self.judge(records)['failures']))
        self.assertTrue(self.judge({'stop': self.stop(13)})['failures'])

    def test_lock_must_be_released_after_the_save(self):
        self.assertIn('The logind delay lock was not released after the final save',
                      self.judge(self.clean(lock=None))['failures'])

    def test_clean_cycle_requires_orderly_systemd_shutdown_lines(self):
        missing = 'No orderly shutdown lines were logged by systemd-logind or PID 1'
        self.assertIn(missing, self.judge(self.clean(orderly=None))['failures'])
        for message in ('Reached target shutdown.target - System Shutdown.',
                        'Reached target poweroff.target - System Power Off.'):
            self.assertTrue(self.judge(self.clean(orderly=self.pid1(message, 14)))['verified'])

    def test_logind_powering_down_line_is_accepted_but_only_from_logind(self):
        self.assertTrue(self.judge(self.clean(orderly=self.logind('System is powering down.', 14)))['verified'])
        missing = 'No orderly shutdown lines were logged by systemd-logind or PID 1'
        for record in (self.agent('System is powering down.', 14),                       # no source
                       self.pid1('System is powering down.', 14),                        # not PID 1's line
                       self.logind('Reached target poweroff.target - System Power Off.', 14),  # not logind's
                       self.agent('Reached target poweroff.target - System Power Off.', 14),   # no source
                       self.logind('Session 2 logged out.', 14)):
            self.assertIn(missing, self.judge(self.clean(orderly=record))['failures'])
        for unit in ('user@1000.service', 'tmux.scope'):
            record = self.agent('System is powering down.', 14, _SYSTEMD_UNIT=unit)
            self.assertIn(missing, self.judge(self.clean(orderly=record))['failures'])

    def test_records_outside_the_prepared_boot_window_are_ignored(self):
        records = self.clean()
        records['start']['_BOOT_ID'] = 'another'
        self.assertTrue(self.judge(records)['failures'])
        records = self.clean()
        records['end']['__REALTIME_TIMESTAMP'] = str(int(2000 * 1e6))
        self.assertTrue(self.judge(records)['failures'])

    def test_empty_journal_is_not_evidence_of_an_abrupt_cycle(self):
        result = self.judge({'stop': self.stop(13), 'orderly': None}, abrupt=True, method='abrupt')
        self.assertIn('The prepared boot has no session-agent journal records; it was not observed',
                      result['failures'])
        self.assertFalse(result['verified'])

    def test_abrupt_cycle_passes_only_without_a_final_save_or_orderly_shutdown(self):
        quiet = {'started': self.agent('Started wsctl-gnome-session.service', 1), 'stop': self.stop(13)}
        self.assertEqual(self.judge(quiet, abrupt=True, method='abrupt')['failures'], [])
        self.assertTrue(self.judge(self.clean(), abrupt=True, method='abrupt')['failures'])
        start = {'started': quiet['started'], 'start': self.clean()['start']}
        self.assertTrue(self.judge(start, abrupt=True, method='abrupt')['failures'])
        orderly = {**quiet, 'orderly': self.logind('System is powering down.', 14)}
        for record in (self.logind('System is powering down.', 14),
                       self.pid1('Reached target poweroff.target - System Power Off.', 14)):
            self.assertIn('Orderly systemd shutdown lines were logged; the power-off was not abrupt',
                          self.judge({**quiet, 'orderly': record}, abrupt=True, method='abrupt')['failures'])
        self.assertIn('Orderly systemd shutdown lines were logged; the power-off was not abrupt',
                      self.judge(orderly, abrupt=True, method='abrupt')['failures'])

    def test_journal_queries_use_the_prepared_boot_id_and_user(self):
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            return ''
        with patch.object(harness.f, 'run', side_effect=run), patch.object(harness.os, 'getuid', return_value=1000):
            harness.final_save_evidence(self.BEFORE, self.BOOT)
        self.assertEqual(len(calls), 4)
        for call in calls:
            self.assertIn('--boot=oldboot', call)
            self.assertNotIn('--boot=-1', call)
        self.assertIn('_UID=1000', calls[0])
        self.assertIn('_SYSTEMD_USER_UNIT=wsctl-gnome-session.service', calls[0])
        self.assertIn('_PID=1', calls[2])
        self.assertIn('_SYSTEMD_UNIT=systemd-logind.service', calls[3])
        self.assertTrue(any('System is powering down' in argument for argument in calls[3]))

    def test_periodic_checkpoint_run_must_be_recent_and_after_preparation(self):
        def run(at, message='wsctl: checkpoint saved to /x'):
            return self.agent(message, at)
        before = {**self.BEFORE, 'method': 'abrupt'}
        # Shutdown at real time 1000 (monotonic is irrelevant here).
        self.assertEqual(harness.periodic_checkpoint_failures(before, 1000, [run(500)]), [])
        self.assertEqual(harness.periodic_checkpoint_failures(
            before, 1000, [run(400, 'wsctl: checkpoint unchanged'), run(540)]), [])
        self.assertTrue(harness.periodic_checkpoint_failures(before, 1000, [run(600)]))      # 400 s old
        self.assertTrue(harness.periodic_checkpoint_failures(before, 1000, [run(-450)]))     # before prepare
        self.assertTrue(harness.periodic_checkpoint_failures(before, 1000, [run(500, 'wsctl: checkpoint skipped')]))
        self.assertTrue(harness.periodic_checkpoint_failures(before, 1000, []))
        self.assertTrue(harness.periodic_checkpoint_failures(before, None, [run(500)]))


class SnapshotSourceTests(unittest.TestCase):
    def snapshot(self, source, created):
        return {'created_at': datetime.fromtimestamp(created, timezone.utc).isoformat(timespec='seconds'),
                'category_provenance': {'terminals': {'source': source}}}

    def test_clean_cycle_requires_final_save_created_after_the_request_and_before_the_boot(self):
        before = {'prepared_at': 100, 'method': 'systemctl-poweroff'}
        final, boot = {'started_at': 500.0}, {'started_at': 1000}
        check = lambda snapshot, final=final: harness.snapshot_source_failures(before, snapshot, final, None, boot)
        self.assertEqual(check(self.snapshot('final-save', 502)), [])
        self.assertTrue(check(self.snapshot('auto-save', 502)))
        self.assertTrue(check(self.snapshot('final-save', 400)))
        self.assertTrue(check(self.snapshot('final-save', 1200)))   # a post-boot rewrite is not the restored one
        self.assertTrue(check(self.snapshot('final-save', 502), None))

    def test_abrupt_cycle_accepts_periodic_or_manual_snapshot_before_the_power_off(self):
        before = {'prepared_at': 100, 'method': 'abrupt'}
        check = lambda snapshot, at=1000: harness.snapshot_source_failures(before, snapshot, None, at, {'started_at': 2000})
        self.assertEqual(check(self.snapshot('auto-save', 700)), [])
        self.assertEqual(check(self.snapshot('manual-save', 90)), [])
        self.assertTrue(check(self.snapshot('final-save', 700)))
        self.assertTrue(check(self.snapshot('auto-save', 1200)))    # created after the power-off
        self.assertTrue(check(self.snapshot('auto-save', 700), None))

    def test_find_snapshot_selects_by_the_restore_markers_created_at(self):
        old, new = self.snapshot('final-save', 200), self.snapshot('auto-save', 900)
        self.assertEqual(harness.find_snapshot(old['created_at'], [new, old]), old)
        with self.assertRaisesRegex(RuntimeError, 'not found'):
            harness.find_snapshot('2001-01-01T00:00:00+00:00', [new, old])

    def test_restored_snapshot_uses_the_marker_then_history_not_the_live_canonical(self):
        from workspace_state import checkpoint, cli, startup, storage
        old, new = self.snapshot('final-save', 200), self.snapshot('auto-save', 900)
        marker = startup.StageMarker('terminals', 'ready', old['created_at'])
        with patch.object(cli, '_startup_marker', return_value=Path('/unused')), \
             patch.object(startup, 'read_stage_marker', return_value=marker), \
             patch.object(storage, 'load', return_value=new), \
             patch.object(checkpoint, 'history_generations', return_value=[('name', old)]):
            self.assertEqual(harness.restored_snapshot(), old)
        with patch.object(cli, '_startup_marker', return_value=Path('/unused')), \
             patch.object(startup, 'read_stage_marker', return_value=startup.StageMarker('terminals')), \
             patch.object(storage, 'load', return_value=new):
            with self.assertRaisesRegex(RuntimeError, 'no terminals restore snapshot'):
                harness.restored_snapshot()


class ShutdownFilesTests(unittest.TestCase):
    def test_no_new_transaction_file_may_be_watched_or_remain(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old = '/data/shutdown-prepared-old.json'   # left by an older coordinator
            before = {'shutdown_files': [old]}
            self.assertTrue(harness.shutdown_files_failures(root, before))  # watcher never recorded
            harness.write(root / 'watch-shutdown-files.json', [])
            with patch.object(harness, 'shutdown_files', return_value=[old]) as listing:
                self.assertEqual(harness.shutdown_files_failures(root, before), [])
                listing.assert_called_once_with(runtime=False)   # the runtime tmpfs is not judged after boot
            with patch.object(harness, 'shutdown_files', return_value=[old, '/data/shutdown-prepared-x.json']):
                self.assertTrue(harness.shutdown_files_failures(root, before))
            harness.write(root / 'watch-shutdown-files.json', ['/run/shutdown-commit.json'])
            with patch.object(harness, 'shutdown_files', return_value=[old]):
                self.assertTrue(harness.shutdown_files_failures(root, before))


class PrepareFactsTests(unittest.TestCase):
    def facts(self, listing, delay='t 20000000'):
        def run(*args, **kwargs):
            return delay if args[0] == 'busctl' else listing
        with patch.object(harness.f, 'run', side_effect=run):
            return harness.logind_facts()

    def test_delay_and_the_agents_inhibitor_are_recorded(self):
        facts = self.facts('workspace-state  1000 tester 77 python3  shutdown  final save  delay\n'
                           'NetworkManager  0 root 5 nm  sleep  nm  delay\n')
        self.assertEqual(facts['inhibit_delay_max_usec'], 20_000_000)
        self.assertEqual([item['mode'] for item in facts['inhibitors']], ['delay'])
        self.assertEqual(harness.logind_failures(facts), [])
        self.assertEqual(harness.expected_budget(20.0), 17.0)

    def test_missing_delay_lock_or_a_block_inhibitor_is_refused(self):
        self.assertTrue(harness.logind_failures(self.facts('')))
        blocking = self.facts('workspace-state 1000 u 7 py shutdown x block\n'
                              'workspace-state 1000 u 7 py shutdown x delay\n')
        self.assertIn('The session agent holds a blocking inhibitor', harness.logind_failures(blocking))
        with self.assertRaisesRegex(RuntimeError, 'Unexpected InhibitDelayMaxUSec'):
            self.facts('', delay='garbage')


class PrepareFlowTests(unittest.TestCase):
    """Preparation records its method and never changes Chrome after the manual save by default."""

    def recipe(self, url):
        window = {'groups': [{'id': 'g'}], 'tabs': [{'url': url, 'group': 'g'}]}
        return {'created_at': 'now', 'desktop': {'workspace_names': ['One']}, 'terminals': [],
                'sessions': [{'name': 'scale-01', 'windows': [{'index': 0, 'name': 'work', 'panes': [
                    {'index': index, 'cwd': '/tmp', 'codex': {'session_id': f'uuid-{index}', 'pid': index}}
                    for index in range(23)]}]}],
                'browsers': {'google_chrome': {'profiles': [{'profile': 'Default', 'windows': [window]}]}}}

    NEW_AGENT = [{'line': 'workspace-state 1000 u 5 py shutdown final save delay', 'mode': 'delay'}]
    OLD_AGENT = [{'line': 'workspace-state 1000 u 5 py shutdown transaction block', 'mode': 'block'}]

    def run_prepare(self, root, *, method='systemctl-poweroff', mutate=False, inhibitors=None, expect='pass'):
        from workspace_state import browser, cli, login_status, operations, startup, storage
        context = operations.OperationContext('test-boot', 'test-login', 'test-operation', 'startup', 1, 100)
        status = root / 'status.json'
        harness.write(status, {'mode': 'startup', 'session_id': 'test-login', 'operation_id': 'test-operation',
                               'operation_context': context.to_dict(), 'operation_state': 'completed',
                               'stages': [{'id': 'browsers', 'state': 'ready'}]})
        harness.write(root / 'conversation-identities.json', [f'uuid-{index}' for index in range(23)])
        manual, live = self.recipe('about:blank#manual'), self.recipe('about:blank#manual')
        if mutate:
            live = self.recipe('about:blank#later')
        captures = iter([self.recipe('about:blank#manual'), live])
        evolutions = []
        marker = root / 'marker.json'

        def write_marker(path, value):
            path.write_text('failed')
        refusal = [Mock(success=False, message='stale group')]
        def capture():
            value = next(captures)
            if value is live:   # the daytime capture sees the preparation's tmux rename
                run = next(path for path in (root / 'poweroff').iterdir() if path.is_dir())
                value['sessions'][0]['windows'][0]['name'] = 'daytime-' + run.name[:8]
            return value
        facts = {'logind': {'inhibit_delay_max_usec': 20_000_000,
                            'inhibitors': copy.deepcopy(self.NEW_AGENT if inhibitors is None else inhibitors)},
                 'shutdown_files': ['/data/shutdown-prepared-old.json']}
        with ExitStack() as stack:
            stack.enter_context(patch.object(harness, 'ROOT', root / 'poweroff'))
            stack.enter_context(patch.object(harness.f, 'ROOT', root))
            for owner, name, value in [(harness, 'boot', 'test-boot'), (harness, 'require_fixture_inventory', {}),
                                       (harness, 'stale_catalog_probe', {}), (harness, 'chrome_identity', []),
                                       (harness.f, 'observe_chrome', []), (harness.f, 'shell', {'windows': []}),
                                       (harness.f, 'process_identity', {'pid': 1}), (harness.f, 'wayland_login', 'l'),
                                       (harness, 'native_inventory', [])]:
                stack.enter_context(patch.object(owner, name, return_value=value))
            stack.enter_context(patch.object(harness, 'capture', side_effect=capture))
            stack.enter_context(patch.object(harness, 'evolve', side_effect=lambda: evolutions.append(1) or {'e': 1}))
            stack.enter_context(patch.object(harness.f, 'run', return_value='saved'))
            stack.enter_context(patch.object(login_status, 'status_path', return_value=status))
            stack.enter_context(patch.object(cli, '_login_generation_file', return_value='test-login'))
            stack.enter_context(patch.object(cli, '_startup_marker', return_value=marker))
            stack.enter_context(patch.object(startup, 'write_stage_marker', side_effect=write_marker))
            stack.enter_context(patch.object(browser, 'restore_browser', return_value=refusal))
            stack.enter_context(patch.object(storage, 'load', return_value=manual))
            stack.enter_context(patch.object(harness, 'cycle_facts', return_value=facts))
            output = io.StringIO()
            stack.enter_context(redirect_stdout(output))
            args = argparse.Namespace(new_run=False, expect=expect, method=method, mutate_chrome=mutate,
                                      installed_release='/release')
            harness.prepare(args)
            self.output = json.loads(output.getvalue())
        run = next(path for path in (root / 'poweroff').iterdir() if path.is_dir())
        return run, evolutions, manual

    def test_default_preparation_leaves_chrome_alone_after_the_manual_save(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, evolutions, manual = self.run_prepare(root)
            self.assertEqual(len(evolutions), 1)   # only the pre-save evolution
            before = harness.read(run / 'before.json')
            self.assertEqual((before['method'], before['abrupt'], before['mutate_chrome']),
                             ('systemctl-poweroff', False, False))
            self.assertEqual(before['shutdown_files'], ['/data/shutdown-prepared-old.json'])
            self.assertFalse((run / 'second-evolution.json').exists())

    def test_mutated_chrome_keeps_the_live_changed_state_as_the_oracle(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run, evolutions, manual = self.run_prepare(root, method='abrupt', mutate=True)
            self.assertEqual(len(evolutions), 2)
            self.assertTrue((run / 'second-evolution.json').exists())
            expected = harness.read(run / 'expected.json')
            urls = [tab['url'] for profile in expected['browsers']['google_chrome']['profiles']
                    for window in profile['windows'] for tab in window['tabs']]
            self.assertEqual(urls, ['about:blank#later'])
            self.assertNotEqual(expected['browsers'], manual['browsers'])
            self.assertTrue(harness.read(run / 'before.json')['abrupt'])
            # Chrome is not captured before M8, so the restored manual recipe differs
            # from the oracle, equals the manual baseline and reproduces the regression.
            differences = harness.differences(expected, manual)
            self.assertTrue(any(item['category'] == 'browsers' for item in differences))
            browsers = lambda value: [item for item in harness.content_items(value) if item[0] == 'browsers']
            self.assertNotEqual(browsers(expected), browsers(manual))

    def test_default_preparation_still_expects_the_manual_save_recipe(self):
        with tempfile.TemporaryDirectory() as temporary:
            run, _, manual = self.run_prepare(Path(temporary))
            self.assertEqual(harness.read(run / 'expected.json')['browsers'], manual['browsers'])

    def test_candidate_preparation_requires_the_delay_lock_and_no_block_before_any_capture(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RuntimeError, 'blocking inhibitor'):
                self.run_prepare(root, inhibitors=self.OLD_AGENT)
            with self.assertRaisesRegex(RuntimeError, 'no shutdown delay lock'):
                self.run_prepare(root, inhibitors=[])
            self.assertFalse((root / 'poweroff').exists())
            with self.assertRaisesRegex(RuntimeError, '--method is required'):
                self.run_prepare(root, method=None)

    def test_negative_baseline_on_an_old_release_records_the_block_lock_as_expected_divergence(self):
        with tempfile.TemporaryDirectory() as temporary:
            run, _, _ = self.run_prepare(Path(temporary), inhibitors=self.OLD_AGENT,
                                         expect='browser-retention-regression')
            before = harness.read(run / 'before.json')
            self.assertIn('The session agent holds a blocking inhibitor', before['logind_expected_divergence'])
            self.assertIn('The session agent holds no shutdown delay lock (workspace-state, delay)',
                          before['logind_expected_divergence'])

    def test_candidate_preparation_records_no_divergence(self):
        with tempfile.TemporaryDirectory() as temporary:
            run, _, _ = self.run_prepare(Path(temporary))
            self.assertNotIn('logind_expected_divergence', harness.read(run / 'before.json'))

    def test_cycle_facts_only_records_so_an_old_release_can_be_prepared(self):
        old = {'inhibit_delay_max_usec': 20_000_000, 'inhibitors': self.OLD_AGENT}
        with patch.object(harness, 'logind_facts', return_value=old), \
             patch.object(harness, 'shutdown_files', return_value=['/data/shutdown-x.json']):
            facts = harness.cycle_facts()
        self.assertEqual(facts, {'logind': old, 'shutdown_files': ['/data/shutdown-x.json']})
        with self.assertRaisesRegex(RuntimeError, 'blocking'):
            harness.require_new_agent_lock(facts)

    def test_watch_hint_follows_the_default_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            self.run_prepare(Path(temporary), method='abrupt')
        self.assertIn('15 minutes', self.output['next'])
        self.assertEqual(harness.DEFAULT_WATCH_TIMEOUT, 900)
        self.assertNotIn('ten minutes', self.output['next'])


class PendingIndexTests(unittest.TestCase):
    def snapshot(self):
        return {'terminals': [], 'sessions': [], 'desktop': {'workspace_names': ['One']},
                'vscode': {'windows': [{'window_key': 'w', 'folders': ['/tmp'], 'editor_uris': [],
                                        'dirty_count': 0, 'profile': None, 'workspace_file': None,
                                        'placement': None}]}}

    def test_items_kept_by_the_final_save_are_not_differences_and_the_index_is_recorded(self):
        expected = self.snapshot()
        archived = self.snapshot()
        archived['pending'] = {'vscode': ['w']}
        self.assertEqual(harness.differences(expected, archived), [])
        # A kept item is accepted; a genuinely dropped one still differs.
        archived['vscode']['windows'] = []
        self.assertTrue(harness.differences(expected, archived))

    def test_verify_writes_the_pending_index_into_the_result(self):
        good = {'verified': True, 'failures': []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = PoweroffHarnessTests().verify_with_unavailable_live_browser(
                root, expectation='pass', regression=False, healthy=True, os_receipt=good, qmp_receipt=good,
                archived_extra={'pending': {'vscode': ['w']}})
        self.assertTrue(result['passed'])
        self.assertEqual(result['restored_pending_index'], {'vscode': ['w']})


class MethodEvidenceTests(unittest.TestCase):
    BEFORE = {'boot_id': 'old-boot', 'prepared_at': 100, 'method': 'systemctl-poweroff'}
    BOOT = {'started_at': 1000}

    def record(self, message, at, **extra):
        return {'_BOOT_ID': 'oldboot', 'MESSAGE': message, '__MONOTONIC_TIMESTAMP': str(int(at * 1e6)),
                '__REALTIME_TIMESTAMP': str(int((500 + at) * 1e6)), **extra}

    def agent(self, *messages):
        return [self.record(message, 10 + index) for index, message in enumerate(messages)]

    def judge(self, method, agent, sudo=None):
        return harness.method_evidence({**self.BEFORE, 'method': method}, self.BOOT, agent, sudo)

    def test_menu_sequence_is_recorded_in_order_and_is_consistent(self):
        agent = self.agent('wsctl: GNOME QueryEndSession', 'wsctl: GNOME EndSession',
                           'wsctl: final save started; trigger=end-session; budget=17.0s',
                           'wsctl: logind PrepareForShutdown(true)')
        result = self.judge('gnome-menu', list(reversed(agent)))
        self.assertEqual(result['sequence'], ['GNOME QueryEndSession', 'GNOME EndSession',
                                              'trigger=end-session', 'logind PrepareForShutdown(true)'])
        self.assertEqual(result['conclusion'], 'consistent')
        self.assertTrue(result['informational'])

    def test_systemctl_is_consistent_without_a_query_and_inconsistent_with_one(self):
        quiet = self.agent('wsctl: logind PrepareForShutdown(true)',
                           'wsctl: final save started; trigger=prepare-for-shutdown; budget=17.0s')
        self.assertEqual(self.judge('systemctl-poweroff', quiet)['conclusion'], 'consistent')
        menu = self.agent('wsctl: GNOME QueryEndSession') + quiet
        self.assertEqual(self.judge('systemctl-poweroff', menu)['conclusion'], 'inconsistent')
        self.assertEqual(self.judge('gnome-menu', quiet)['conclusion'], 'inconsistent')

    def test_sudo_shutdown_needs_the_sudo_command_and_no_menu_query(self):
        agent = self.agent('wsctl: logind PrepareForShutdown(true)')
        sudo = [self.record('tester : TTY=pts/0 ; PWD=/home ; USER=root ; COMMAND=/usr/sbin/shutdown now', 9)]
        result = self.judge('sudo-shutdown', agent, sudo)
        self.assertEqual((result['sudo_shutdown_command'], result['conclusion']), (True, 'consistent'))
        other = [self.record('tester : TTY=pts/0 ; USER=root ; COMMAND=/usr/bin/systemctl poweroff', 9)]
        result = self.judge('sudo-shutdown', agent, other)
        self.assertEqual((result['sudo_shutdown_command'], result['conclusion']), (False, 'inconsistent'))
        old = [self.record('tester : COMMAND=/usr/sbin/shutdown now', -450)]
        self.assertFalse(self.judge('sudo-shutdown', agent, old)['sudo_shutdown_command'])
        result = self.judge('sudo-shutdown', agent, None)
        self.assertEqual((result['sudo_shutdown_command'], result['conclusion']), (None, 'unknown'))

    def test_no_agent_records_is_unknown(self):
        self.assertEqual(self.judge('gnome-menu', [])['conclusion'], 'unknown')

    def test_abrupt_with_a_final_save_trigger_is_inconsistent(self):
        self.assertEqual(self.judge('abrupt', self.agent('Started wsctl-gnome-session.service'))['conclusion'],
                         'consistent')
        trigger = self.agent('wsctl: final save started; trigger=prepare-for-shutdown; budget=17.0s')
        self.assertEqual(self.judge('abrupt', trigger)['conclusion'], 'inconsistent')

    def test_unreadable_journal_gives_unknown_not_an_error(self):
        with patch.object(harness.f, 'run', side_effect=RuntimeError('Guest journal unavailable')):
            result = harness.method_evidence_probe(self.BEFORE, self.BOOT)
        self.assertEqual(result['conclusion'], 'unknown')
        self.assertIn('Guest journal unavailable', result['error'])

    def test_sudo_journal_failure_leaves_only_that_fact_unknown(self):
        agent = [self.record('wsctl: logind PrepareForShutdown(true)', 10, _UID='1000')]
        calls = []

        def records(boot, *filters, grep=None):
            calls.append(filters)
            if '_COMM=sudo' in filters:
                raise RuntimeError('no sudo journal')
            return agent
        with patch.object(harness, 'journal_records', side_effect=records):
            result = harness.method_evidence_probe({**self.BEFORE, 'method': 'sudo-shutdown'}, self.BOOT)
        self.assertEqual((result['sudo_shutdown_command'], result['conclusion']), (None, 'unknown'))
        self.assertEqual(result['sequence'], ['logind PrepareForShutdown(true)'])

    def test_method_evidence_never_changes_the_verdict(self):
        good = {'verified': True, 'failures': []}
        for conclusion in ('consistent', 'inconsistent', 'unknown'):
            evidence = {'informational': True, 'method': 'systemctl-poweroff', 'sequence': [],
                        'conclusion': conclusion}
            with tempfile.TemporaryDirectory() as temporary, \
                 patch.object(harness, 'method_evidence_probe', return_value=evidence):
                result = PoweroffHarnessTests().verify_with_unavailable_live_browser(
                    Path(temporary), expectation='pass', regression=False, healthy=True, os_receipt=good,
                    qmp_receipt=good)
            self.assertTrue(result['passed'])
            self.assertEqual(result['failures'], {})
            self.assertEqual(result['method_evidence'], evidence)


class WatchTests(unittest.TestCase):
    def test_verify_fails_on_a_recorded_watcher_failure(self):
        good = {'verified': True, 'failures': []}
        failure = {'error': 'RuntimeError: Watch expired without VM exit', 'time': 1}
        with tempfile.TemporaryDirectory() as temporary:
            result = PoweroffHarnessTests().verify_with_unavailable_live_browser(
                Path(temporary), expectation='pass', regression=False, healthy=True, os_receipt=good,
                qmp_receipt=good, watch_failure=failure)
        self.assertFalse(result['passed'])
        self.assertEqual(set(result['failures']), {'watch'})

    def test_default_timeout_outlasts_the_abrupt_periodic_wait(self):
        with patch.object(sys, 'argv', ['run_vm_poweroff.py', '--disposable-guest', 'watch']), \
             patch.object(harness, 'guest_environment', return_value='/release'), \
             patch.object(harness, 'watch') as watch:
            harness.main()
        self.assertGreaterEqual(watch.call_args.args[0].timeout, 600)



if __name__ == '__main__':
    unittest.main()
