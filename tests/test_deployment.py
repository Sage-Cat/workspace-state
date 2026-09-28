import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import deployment as release


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.locations = release.Locations(self.root / 'home', self.root / 'data', self.root / 'config',
                                          self.root / 'state', self.root / 'prefix', self.root / 'runtime')
        self.source = self.root / 'checkout/component'
        self.source.mkdir(parents=True)
        (self.source / 'bin').mkdir()
        self.tool = self.source / 'bin/tool'
        self.tool.write_text('#!/bin/sh\nprintf first\n')
        self.tool.chmod(0o755)
        (self.source / 'extension.js').write_text("import {BUILD_REVISION} from './buildInfo.js';\n")
        (self.source / 'buildInfo.js').write_text("export const BUILD_REVISION = 'development';\n")
        self.manifest = self.root / 'manifest.toml'
        self.manifest.write_text('''schema_version = 1
[[components]]
name = "tool"
source = "component"
files = ["bin/*", "*.js"]
protocol = 1
capabilities = ["runtime_build"]
stamps = [{path="buildInfo.js", format="esm"}]
install = [{source="bin/*", target="{prefix}/bin/{name}"}, {source=".", target="{data}/extensions/tool"}]
runtime = {destination="org.gnome.Shell", path="/org/sagecat/Tool", interface="org.sagecat.Tool"}
''')

    def stage(self):
        return release.stage(self.manifest, self.source.parent, self.locations)

    def test_roundtrip_stage_install_upgrade_rollback_preserves_custom_config(self):
        target = self.locations.prefix / 'bin/tool'
        target.parent.mkdir(parents=True)
        target.write_text('old local command')
        custom = self.locations.config / 'custom.toml'
        custom.parent.mkdir(parents=True)
        custom.write_text('# custom\nvalue = 7\n')
        first = self.stage()
        self.assertFalse((self.locations.releases / 'current').exists(), 'staging cannot install')
        self.assertEqual(first['revision'], self.stage()['revision'])
        installed = release.install(first['revision'], self.locations)
        self.assertIn('first', target.read_text())
        self.assertEqual(Path(installed['backups'][str(target)]).read_text(), 'old local command')
        self.assertIn(first['revision'], (self.locations.data / 'extensions/tool/buildInfo.js').read_text())
        self.assertIn('development', (self.source / 'buildInfo.js').read_text())
        self.tool.write_text('#!/bin/sh\nprintf second\n')
        second = self.stage()
        self.assertNotEqual(first['revision'], second['revision'])
        release.install(second['revision'], self.locations)
        self.assertIn('second', target.read_text())
        self.assertEqual((self.locations.releases / 'previous').resolve().name, first['revision'])
        release.rollback(self.locations)
        self.assertIn('first', target.read_text())
        self.assertEqual(custom.read_text(), '# custom\nvalue = 7\n')
        self.assertEqual((self.locations.releases / 'current').resolve().name, first['revision'])

    def test_install_refuses_user_replacement_of_owned_link(self):
        first = self.stage()
        release.install(first['revision'], self.locations)
        target = self.locations.prefix / 'bin/tool'
        target.unlink()
        target.write_text('my replacement')
        with self.assertRaisesRegex(ValueError, 'changed; preserved'):
            release.install(first['revision'], self.locations)
        self.assertEqual(target.read_text(), 'my replacement')

    def test_install_failure_rolls_back_prior_links_and_pointer(self):
        first = self.stage()
        release.install(first['revision'], self.locations)
        self.tool.write_text('#!/bin/sh\nprintf second\n')
        second = self.stage()
        original = release._json
        def fail_state(path, value):
            if path.name == 'installation.json':
                raise OSError('disk full')
            return original(path, value)
        with patch.object(release, '_json', side_effect=fail_state):
            with self.assertRaisesRegex(OSError, 'disk full'):
                release.install(second['revision'], self.locations)
        self.assertIn('first', (self.locations.prefix / 'bin/tool').read_text())
        self.assertEqual((self.locations.releases / 'current').resolve().name, first['revision'])

    def test_stage_rejects_source_change_and_symlinks(self):
        original = release.shutil.copyfile
        def changed(source, target):
            result = original(source, target)
            if Path(source) == self.tool:
                self.tool.write_text('changed concurrently')
            return result
        with patch.object(release.shutil, 'copyfile', side_effect=changed):
            with self.assertRaisesRegex(ValueError, 'changed while staging'):
                self.stage()
        self.assertFalse(list(self.locations.releases.glob('r-*')))
        self.tool.unlink()
        self.tool.symlink_to('/etc/passwd')
        with self.assertRaisesRegex(ValueError, 'source symlink'):
            self.stage()

    def test_runtime_build_and_unknown_identity_are_not_inferred_from_install(self):
        first = self.stage()
        release.install(first['revision'], self.locations)
        (self.locations.runtime / 'workspace-state').mkdir(parents=True)
        (self.locations.runtime / 'workspace-state/current-operation.json').write_text(json.dumps({'operation_context': {'operation_id': 'exact'}}))
        checkpoint = self.locations.data / 'workspace-state/snapshots/current.json'
        checkpoint.parent.mkdir(parents=True)
        checkpoint.write_text('{"version":5}')
        reader = lambda spec: {'build': {'revision': 'development'}, 'capabilities': []}
        unknown = release.doctor(self.manifest, self.source.parent, self.locations, runtime_reader=reader)
        self.assertTrue(unknown['components'][0]['pending_activation'])
        self.assertIsNone(unknown['components'][0]['running_revision'])
        self.assertEqual(unknown['components'][0]['missing_capabilities'], ['runtime_build'])
        self.assertEqual(unknown['operation_context'], {'operation_id': 'exact'})
        self.assertEqual(unknown['checkpoint_version'], 5)
        reader = lambda spec: {'build': {'revision': first['revision']}, 'capabilities': ['runtime_build']}
        known = release.doctor(self.manifest, self.source.parent, self.locations, runtime_reader=reader)
        self.assertFalse(known['components'][0]['pending_activation'])
        self.assertEqual(known['components'][0]['missing_capabilities'], [])

    def test_release_tampering_is_detected_before_install(self):
        staged = self.stage()
        file = self.locations.releases / staged['revision'] / 'components/tool/bin/tool'
        file.chmod(0o755)
        file.write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'content changed'):
            release.install(staged['revision'], self.locations)
        self.assertFalse((self.locations.releases / 'current').exists())

    def test_private_profiles_and_node_modules_are_not_staged(self):
        for name in ('profiles', 'node_modules', '.git', 'logs'):
            directory = self.source / name
            directory.mkdir()
            (directory / 'private.js').write_text('secret')
        self.manifest.write_text(self.manifest.read_text().replace('"*.js"', '"**/*.js"'))
        staged = self.stage()
        self.assertFalse(any('private.js' in path for path in staged['files']))

    def test_development_mode_is_explicit_and_does_not_claim_release_identity(self):
        staged = self.stage()
        release.install(staged['revision'], self.locations, development=True)
        target = self.locations.prefix / 'bin/tool'
        self.assertEqual(target.resolve(), self.tool)
        self.tool.write_text('changed in checkout')
        self.assertEqual(target.read_text(), 'changed in checkout')
        info = release.doctor(self.manifest, self.source.parent, self.locations, runtime_reader=lambda _: {})
        self.assertEqual(info['mode'], 'development')
        self.assertIsNone(info['components'][0]['installed_revision'])

    def test_inventory_migration_preserves_comments_custom_fields_and_is_idempotent(self):
        path = self.root / 'inventory.toml'
        original = '''# user heading
schema_version = 1
[[sources]]
id = "input-source-popup-guard"
kind = "gnome-extension"
ownership = "first-party"
uuid = "input-source-popup-guard@sagecat.local" # keep this comment
source_ref = "~/.local/share/gnome-shell/extensions/input-source-popup-guard@sagecat.local" # keep path comment
custom = "leave me"
[[sources]]
id = "mine"
uuid = "input-source-popup-guard@sagecat.local"
'''
        path.write_text(original)
        self.assertTrue(release.migrate_inventory(path))
        self.assertFalse(release.migrate_inventory(path))
        self.assertEqual(path.read_text(), original.replace('input-source-popup-guard@sagecat.local" #', 'input-source-popup-guard-v2@sagecat.local" #'))
        self.assertEqual(path.with_name(path.name + '.before-input-guard-v2').read_text(), original)

    def test_inventory_migration_repairs_partial_migration_and_preserves_custom_paths(self):
        old_path = '~/.local/share/gnome-shell/extensions/input-source-popup-guard@sagecat.local'
        new_path = '~/.local/share/gnome-shell/extensions/input-source-popup-guard-v2@sagecat.local'
        for uuid in ('input-source-popup-guard@sagecat.local', 'input-source-popup-guard-v2@sagecat.local'):
            for source_ref in (old_path, '/custom/input-source-popup-guard@sagecat.local'):
                with self.subTest(uuid=uuid, source_ref=source_ref):
                    original = ("[[sources]]\nid = 'input-source-popup-guard'\n"
                                "kind = 'gnome-extension'\nownership = 'first-party'\n"
                                f"uuid = '{uuid}' # identity\nsource_ref = '{source_ref}' # location\n"
                                "custom = 'preserved'\n")
                    expected = original.replace(f"uuid = '{uuid}'", "uuid = 'input-source-popup-guard-v2@sagecat.local'")
                    if source_ref == old_path:
                        expected = expected.replace(old_path, new_path)
                    updated = release._migrated_inventory(original)
                    self.assertEqual(updated, expected)
                    self.assertEqual(release._migrated_inventory(updated), updated)
        unrelated = ("[[sources]]\nid = 'mine'\nkind = 'gnome-extension'\n"
                     "ownership = 'first-party'\nuuid = 'input-source-popup-guard-v2@sagecat.local'\n"
                     f"source_ref = '{old_path}'\n")
        self.assertEqual(release._migrated_inventory(unrelated), unrelated)

    def test_real_manifest_never_installs_cleaner_and_host_policy_is_opt_in(self):
        manifest = release.load_manifest(Path(__file__).resolve().parents[1] / 'config/desktop-release.toml')
        components = {item['name']: item for item in manifest['components']}
        self.assertEqual(components['codex-cache-cleaner']['install'], [])
        self.assertEqual(components['host-integration']['profile'], 'host')
        self.assertEqual(components['lg-edge-warp']['profile'], 'host')
        self.assertNotIn('50-nvidia-kms-compat.conf', str(components['host-integration']['install']))

    def test_runtime_query_is_only_scoped_get_state(self):
        response = type('Response', (), {'returncode': 0, 'stdout': '(\'{"build":{"revision":"x"}}\',)', 'stderr': ''})()
        with patch.object(release.subprocess, 'run', return_value=response) as run:
            result = release._runtime_state({'destination': 'org.gnome.Shell', 'path': '/org/sagecat/Tool', 'interface': 'org.sagecat.Tool'})
        self.assertEqual(result['build']['revision'], 'x')
        self.assertEqual(run.call_args.args[0][-1], 'org.sagecat.Tool.GetState')
        self.assertGreater(run.call_args.kwargs['timeout'], 0)
        self.assertLessEqual(run.call_args.kwargs['timeout'], 2)

    def test_generated_launcher_render_and_missing_asset_are_validated(self):
        (self.source / 'worker.py').write_text("print('isolated worker')\n")
        (self.source / 'host.in').write_text('{"path":"@PREFIX@/bin/tool"}')
        self.manifest.write_text(self.manifest.read_text().replace('"*.js"]', '"*.js", "worker.py", "host.in"]') +
            '\nlaunchers = [{path="bin/worker", python="worker.py"}]\n' +
            'renders = [{source="host.in", target="host.json", replacements={"@PREFIX@"="{prefix}"}}]\n')
        staged = self.stage()
        directory = self.locations.releases / staged['revision'] / 'components/tool'
        self.assertTrue(os.access(directory / 'bin/worker', os.X_OK))
        self.assertEqual(json.loads((directory / 'host.json').read_text())['path'], str(self.locations.prefix / 'bin/tool'))
        self.manifest.write_text(self.manifest.read_text().replace('source="bin/*"', 'source="missing"'))
        broken = self.stage()
        with self.assertRaisesRegex(ValueError, 'asset missing'):
            release.install(broken['revision'], self.locations)
        self.assertFalse((self.locations.releases / 'current').exists())

    def test_doctor_reports_missing_source_and_damaged_installed_code(self):
        staged = self.stage()
        release.install(staged['revision'], self.locations)
        file = self.locations.releases / staged['revision'] / 'components/tool/bin/tool'
        file.chmod(0o755)
        file.write_text('tampered')
        result = release.doctor(self.manifest, self.root / 'absent', self.locations, runtime_reader=lambda _: {})
        self.assertIn('missing required', result['source_error'])
        self.assertIn('content changed', result['installed_integrity_error'])
        self.assertIsNone(result['components'][0]['installed_revision'])

    def test_rollback_restores_original_file_for_newly_removed_owned_path(self):
        first = self.stage()
        release.install(first['revision'], self.locations)
        custom = self.locations.prefix / 'bin/extra'
        custom.write_text('user original')
        (self.source / 'bin/extra').write_text('new managed tool')
        second = self.stage()
        release.install(second['revision'], self.locations)
        self.assertEqual(custom.read_text(), 'new managed tool')
        release.rollback(self.locations)
        self.assertFalse(custom.is_symlink())
        self.assertEqual(custom.read_text(), 'user original')

    def test_real_container_replaces_only_alias_and_keeps_checkout_unchanged(self):
        original = self.tool.read_text()
        container = self.locations.data / 'stable-unpacked'
        container.parent.mkdir(parents=True)
        container.symlink_to(self.source / 'bin', target_is_directory=True)
        self.manifest.write_text(self.manifest.read_text().replace('install = [', 'containers = ["{data}/stable-unpacked"]\ninstall = [{source="bin/*", target="{data}/stable-unpacked/{name}"}, '))
        staged = self.stage()
        release.install(staged['revision'], self.locations)
        self.assertTrue(container.is_dir())
        self.assertFalse(container.is_symlink())
        self.assertTrue((container / 'tool').is_symlink())
        self.assertEqual(self.tool.read_text(), original)
        self.assertFalse(self.tool.is_symlink())

    def test_coordinator_receipt_requires_private_file_exact_boot_and_process_start(self):
        with patch.object(release.Locations, 'environment', return_value=self.locations), patch.object(release, '_boot_id', return_value='this-boot'), patch.object(release, '_process_start', return_value='123'), patch.object(release, 'build_fingerprint', return_value={'revision': 'loaded-coordinator'}):
            release.record_coordinator_build('generation-one')
            path = self.locations.runtime / 'workspace-state/coordinator-build.json'
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(release._coordinator_state(path)['build']['revision'], 'loaded-coordinator')
            with patch.object(release, '_process_start', return_value='456'):
                self.assertIn('expired', release._coordinator_state(path)['unavailable'])
            with patch.object(release, '_boot_id', return_value='other-boot'):
                self.assertIn('expired', release._coordinator_state(path)['unavailable'])
            path.chmod(0o644)
            self.assertIn('private', release._coordinator_state(path)['unavailable'])

    def test_companion_endpoint_scan_shares_absolute_budget(self):
        from workspace_state import browser
        now = [0.0]
        def request(path, action, *, timeout):
            now[0] += min(.6, timeout)
            return {'build': {'revision': 'one'}}
        with patch.object(release.time, 'monotonic', side_effect=lambda: now[0]), patch.object(browser, '_host_paths', return_value=[Path(f'endpoint-{i}') for i in range(20)]), patch.object(browser, '_request_path', side_effect=request) as probe:
            result = release._runtime_state({'kind': 'chrome', '_deadline': 8})
        self.assertLessEqual(now[0], 2)
        self.assertLess(probe.call_count, 20)
        self.assertIn('deadline', result['unavailable'])
        self.assertNotIn('build', result)

    def test_doctor_flags_checkout_registered_chrome_for_reregistration(self):
        prefs = self.locations.config / 'google-chrome/Default/Preferences'
        prefs.parent.mkdir(parents=True)
        prefs.write_text(json.dumps({'extensions': {'settings': {'gnccboicpdhhhpdcogeleiegokieocmn': {'path': str(self.source)}}}}))
        result = release._chrome_registration(self.locations)
        self.assertFalse(result[0]['managed'])
        self.assertIn('Re-register', result[0]['activation_action'])
        self.assertIn('cannot activate', result[0]['activation_action'])

    def test_old_python_gets_explicit_prerequisite_error_before_imports(self):
        import runpy
        import sys
        script = Path(__file__).resolve().parents[1] / 'scripts/desktop-release'
        with patch.object(sys, 'version_info', (3, 10)):
            with self.assertRaisesRegex(SystemExit, 'Python 3.11'):
                runpy.run_path(str(script), run_name='__main__')

    def scheduling_fixture(self):
        (self.source / 'scripts').mkdir(exist_ok=True)
        (self.source / 'scripts/desktop-release').write_text('#!/usr/bin/python3\n# isolated fixture\n')
        self.manifest.write_text(self.manifest.read_text().replace('name = "tool"', 'name = "workspace-state"').replace('"*.js"]', '"*.js", "scripts/*"]'))

    def test_schedule_is_deferred_and_reapplies_on_same_boot_next_login(self):
        self.scheduling_fixture()
        first = self.stage()
        release.install(first['revision'], self.locations)
        self.tool.write_text('second release')
        second = self.stage()
        with patch.object(release.subprocess, 'run') as commands:
            pending = release.schedule(second['revision'], self.locations)
        commands.assert_not_called()
        self.assertEqual(pending['state'], 'scheduled')
        self.assertEqual((self.locations.releases / 'current').resolve().name, first['revision'])
        unit = (self.locations.config / 'systemd/user/wsctl-release-activate.service').read_text()
        dropin = (self.locations.config / 'systemd/user/org.gnome.Shell@wayland.service.d/40-wsctl-release-activation.conf').read_text()
        self.assertIn('Before=graphical-session-pre.target', unit)
        self.assertIn('PartOf=graphical-session.target', unit)
        self.assertNotIn('RemainAfterExit=yes', unit)
        self.assertIn('Wants=wsctl-release-activate.service', dropin)
        self.assertNotIn('Requires=', dropin)
        info = release.doctor(self.manifest, self.source.parent, self.locations, runtime_reader=lambda _: {})
        self.assertEqual(info['current'], first['revision'])
        self.assertEqual(info['scheduled_revision'], second['revision'])
        self.assertTrue(info['activation_pending'])
        self.assertIn('scheduled for next graphical login', info['activation'])
        self.assertTrue(info['components'][0]['source_matches_scheduled'])
        release.apply_pending(self.locations, blocker_reader=lambda _: [])
        self.assertEqual((self.locations.releases / 'current').resolve().name, second['revision'])
        applied_info = release.doctor(self.manifest, self.source.parent, self.locations, runtime_reader=lambda _: {})
        self.assertFalse(applied_info['activation_pending'])
        self.assertIn('no release installation scheduled', applied_info['activation'])
        self.tool.write_text('third release, same boot')
        third = self.stage()
        release.schedule(third['revision'], self.locations)
        release.apply_pending(self.locations, blocker_reader=lambda _: [])
        self.assertEqual((self.locations.releases / 'current').resolve().name, third['revision'])

    def test_failed_or_live_session_activation_keeps_previous_and_can_retry(self):
        self.scheduling_fixture()
        first = self.stage()
        release.install(first['revision'], self.locations)
        self.tool.write_text('next')
        second = self.stage()
        release.schedule(second['revision'], self.locations)
        with self.assertRaisesRegex(release.ActivationDeferred, 'deferred') as deferred:
            release.apply_pending(self.locations, blocker_reader=lambda _: ['old coordinator is running'])
        receipt = self.locations.releases / 'pending-install.json'
        self.assertEqual(json.loads(receipt.read_text())['state'], 'waiting')
        self.assertEqual(deferred.exception.pending, json.loads(receipt.read_text()))
        with patch.object(release, 'install', side_effect=OSError('disk full')):
            with self.assertRaisesRegex(OSError, 'disk full'):
                release.apply_pending(self.locations, blocker_reader=lambda _: [])
        self.assertEqual((self.locations.releases / 'current').resolve().name, first['revision'])
        self.assertEqual(json.loads(receipt.read_text())['state'], 'failed')
        self.assertEqual(release.apply_pending(self.locations, blocker_reader=lambda _: [])['state'], 'applied')
        self.assertEqual(json.loads(receipt.read_text())['attempts'], 2)

    def test_apply_pending_cli_defers_cleanly_but_installation_failures_remain_nonzero(self):
        self.scheduling_fixture()
        first = self.stage()
        release.install(first['revision'], self.locations)
        self.tool.write_text('next release')
        second = self.stage()
        receipt = self.locations.releases / 'pending-install.json'
        installation = (self.locations.releases / 'installation.json').read_bytes()
        # Exercise the actual CLI process and receipt path while replacing only
        # desktop probes/install actions; this test cannot touch the live host.
        script = '''
import sys
from unittest.mock import patch
from workspace_state import deployment as release
apply = release.apply_pending
blockers = ['GNOME Shell is still running'] if sys.argv[2] == 'blocked' else []
def isolated_apply(locations, **kwargs):
    return apply(locations, blocker_reader=lambda _: blockers, **kwargs)
with patch.object(release, 'apply_pending', side_effect=isolated_apply), \\
     patch.object(release, 'install', side_effect=OSError('injected installation failure')), \\
     patch.object(release, '_reload_user_manager', side_effect=AssertionError('unexpected manager reload')):
    raise SystemExit(release.main(['deployment', 'apply-pending', '--receipt', sys.argv[1]]))
'''
        for mode in ('blocked', 'install-failure'):
            with self.subTest(mode=mode):
                release.schedule(second['revision'], self.locations)
                result = subprocess.run([sys.executable, '-c', script, str(receipt), mode],
                                        capture_output=True, text=True, timeout=5,
                                        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')})
                pending = json.loads(receipt.read_text())
                self.assertEqual((self.locations.releases / 'current').resolve().name, first['revision'])
                self.assertEqual((self.locations.releases / 'installation.json').read_bytes(), installation)
                if mode == 'blocked':
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, '')
                    self.assertEqual(json.loads(result.stdout), {**pending, 'deferred': True})
                    self.assertEqual(pending['state'], 'waiting')
                    self.assertEqual(pending['attempts'], 0)
                    self.assertNotIn('installed_revision', pending)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn('injected installation failure', result.stderr)
                    self.assertEqual(pending['state'], 'failed')
                    self.assertEqual(pending['attempts'], 1)

    def test_activation_guard_allows_queued_shell_with_no_process(self):
        def response(command, **kwargs):
            stdout = '(false,)' if command[0] == 'gdbus' else 'LoadState=loaded\nActiveState=activating\nMainPID=0\n'
            return type('Result', (), {'returncode': 0, 'stdout': stdout, 'stderr': ''})()
        with patch.object(release.subprocess, 'run', side_effect=response), patch.object(release, '_coordinator_state', return_value={'unavailable': 'absent'}):
            self.assertEqual(release._activation_blockers(self.locations), [])

    def test_make_install_rejects_nondefault_prefix_before_writes(self):
        import subprocess
        repo = Path(__file__).resolve().parents[1]
        target = self.root / 'rejected-prefix'
        result = subprocess.run(['make', 'install', f'PREFIX={target}'], cwd=repo, capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('requires PREFIX=', result.stderr)
        self.assertFalse(target.exists())

    def test_manager_reload_failure_retries_without_reinstall_or_losing_previous(self):
        self.scheduling_fixture()
        first = self.stage()
        release.install(first['revision'], self.locations)
        self.tool.write_text('next release')
        second = self.stage()
        release.schedule(second['revision'], self.locations)
        with patch.object(release, '_reload_user_manager', side_effect=RuntimeError('manager unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'manager unavailable'):
                release.apply_pending(self.locations, blocker_reader=lambda _: [], reload_manager=True)
        receipt = self.locations.releases / 'pending-install.json'
        self.assertEqual(json.loads(receipt.read_text())['state'], 'installed-pending-reload')
        self.assertEqual((self.locations.releases / 'previous').resolve().name, first['revision'])
        with patch.object(release, '_reload_user_manager') as reload_manager, patch.object(release, 'install') as install:
            release.apply_pending(self.locations, blocker_reader=lambda _: [], reload_manager=True)
        install.assert_not_called()
        reload_manager.assert_called_once()
        self.assertEqual((self.locations.releases / 'previous').resolve().name, first['revision'])
        self.assertEqual(json.loads(receipt.read_text())['state'], 'applied')
        release.install(second['revision'], self.locations)
        self.assertEqual((self.locations.releases / 'previous').resolve().name, first['revision'])
