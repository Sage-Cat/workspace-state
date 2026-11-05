from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_upgrade_test_module', HERE / 'run_vm_upgrade.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class UpgradeHarnessTests(unittest.TestCase):
    def fixture(self):
        def state(url):
            return {'browsers': {'google_chrome': {'profiles': [
                {'profile': 'Default', 'windows': [{'id': 'window-1', 'tabs': [
                    {'url': url, 'index': 0, 'group': 4}],
                    'groups': [{'id': 4, 'title': 'Synthetic group', 'color': 'blue'}]}]}]}}}
        original, expected = state('https://example.test/old'), state('https://example.test/new')
        return original, expected, copy.deepcopy(original)

    OLD = {'inhibit_delay_max_usec': 20_000_000, 'inhibitors': [
        {'line': 'workspace-state 1000 u 5 py shutdown transaction block', 'mode': 'block'}]}
    NEW = {'inhibit_delay_max_usec': 20_000_000, 'inhibitors': [
        {'line': 'workspace-state 1000 u 5 py shutdown final save delay', 'mode': 'delay'}]}

    def test_preparation_on_the_old_release_may_hold_a_block_lock(self):
        with patch.object(harness.p, 'logind_facts', return_value=self.OLD), \
             patch.object(harness.p, 'shutdown_files', return_value=[]):
            self.assertEqual(harness.p.cycle_facts()['logind'], self.OLD)

    def test_lock_mode_is_judged_on_the_new_release_after_boot(self):
        with patch.object(harness.p, 'logind_facts', return_value=self.NEW):
            self.assertEqual(harness.new_release_logind(), (self.NEW, []))
        with patch.object(harness.p, 'logind_facts', return_value=self.OLD):
            facts, failures = harness.new_release_logind()
        self.assertEqual(facts, self.OLD)
        self.assertIn('The session agent holds a blocking inhibitor', failures)
        with patch.object(harness.p, 'logind_facts', side_effect=RuntimeError('no bus')):
            facts, failures = harness.new_release_logind()
        self.assertIsNone(facts)
        self.assertIn('unavailable', failures[0])

    def test_six_window_intent_does_not_mutate_independent_live_catalog(self):
        original, _, _ = self.fixture()
        window = original['browsers']['google_chrome']['profiles'][0]['windows'][0]
        window['groups'] = []
        windows = original['browsers']['google_chrome']['profiles'][0]['windows']
        windows[:] = [dict(copy.deepcopy(window), id=f'window-{i}') for i in range(7)]
        live = copy.deepcopy(original)
        receipt = harness.omit_original_ungrouped_window(original)
        self.assertFalse(receipt['live_window_closed'])
        self.assertEqual(len(windows), 6)
        self.assertEqual(len(live['browsers']['google_chrome']['profiles'][0]['windows']), 7)

    def test_viewer_fixture_requires_exact_single_placement(self):
        placement = {'workspace': 2, 'monitor': 1, 'state': 'normal',
                     'coordinate_space': 'global',
                     'geometry': {'x': 1280, 'y': 32, 'width': 960, 'height': 600}}
        window = {'wm_class': 'remote-viewer', **copy.deepcopy(placement)}
        harness.require_viewer_fixture({'windows': [window]}, placement)
        for windows in ([], [window, window], [{**window, 'workspace': 0}],
                        [{**window, 'geometry': {**window['geometry'], 'x': 0}}]):
            with self.assertRaisesRegex(RuntimeError, 'Align'):
                harness.require_viewer_fixture({'windows': windows}, placement)

    def test_retained_recipe_needs_no_observation_or_newer_changes(self):
        original, expected, checkpoint = self.fixture()
        self.assertNotIn('latest_observation', checkpoint['browsers']['google_chrome'])
        self.assertEqual(harness.retained_failures(original, expected, checkpoint), [])
        self.assertEqual(harness.retained_failures(original, original, checkpoint), [])

    def test_adopted_checkpoint_is_not_upgrade_proof(self):
        original, expected, checkpoint = self.fixture()
        checkpoint['browsers']['google_chrome']['profiles'] = expected['browsers']['google_chrome']['profiles']
        self.assertIn('Final save did not retain the original browser recipe',
                      harness.retained_failures(original, expected, checkpoint))

    def test_shutdown_release_follows_the_lock_mode_recorded_at_preparation(self):
        self.assertEqual(harness.shutdown_release({'logind': self.OLD}), 'legacy')
        self.assertEqual(harness.shutdown_release({'logind': self.NEW}), 'current')
        self.assertEqual(harness.shutdown_release({}), 'current')

    BOOT = {'started_at': 1000.0}
    BEFORE = {'prepared_at': 100.0}

    @staticmethod
    def snap(at):
        from datetime import datetime, timezone
        return {'created_at': datetime.fromtimestamp(at, timezone.utc).isoformat()}

    def legacy(self, restored, others, shutdown_at=500.0):
        return harness.legacy_snapshot_failures(
            self.BEFORE, self.snap(restored), [self.snap(x) for x in [restored, *others]], shutdown_at, self.BOOT)

    def test_legacy_shutdown_restores_the_last_published_snapshot(self):
        self.assertEqual(self.legacy(400, [300, 200, 1200]), [])  # later ones are new-release checkpoints
        self.assertIn('A newer snapshot was published before the power-off than the one restored',
                      self.legacy(300, [400]))
        self.assertIn('Restored snapshot was created at or after the power-off', self.legacy(600, []))
        self.assertIn('Restored snapshot predates preparation', self.legacy(50, []))
        self.assertIn('No verified power-off time to bound the restored snapshot', self.legacy(400, [], None))

    def run_verify(self, logind):
        """Run verify() with the journal and guest mocked; return the checks reached."""
        import json, tempfile
        from pathlib import Path
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            before = {'scenario': 'retained-upgrade-without-manual-save', 'manual_save_called': False,
                      'boot_id': 'old', 'expected_digest': 'd', 'installed_release': 'old-release',
                      'prepared_at': 100.0, 'logind': logind, 'native_inventory': None}
            files = {'before.json': before, 'expected.json': {}, 'original.json': {}}
            for name, value in files.items():
                (directory / name).write_text(json.dumps(value))
            p = harness.p
            def stop(*_args):
                raise StopIteration
            patches = [
                patch.object(p, 'run_directory', return_value=directory),
                patch.object(p, 'boot', return_value='new'), patch.object(p, 'digest', return_value='d'),
                patch.object(p, 'read', side_effect=lambda path: json.loads(Path(path).read_text())
                             if Path(path).exists() else {'operation_state': 'completed', 'stages': []}),
                patch.object(p, 'capture', return_value={}), patch.object(p, 'write'),
                patch.object(p, 'current_boot_evidence', return_value=self.BOOT),
                patch.object(p, 'restored_snapshot', return_value=self.snap(400)),
                patch.object(p, 'differences', return_value=[]),
                patch.object(p, 'native_placement_failures', return_value=[]),
                patch.object(p, 'native_inventory', return_value=None),
                patch.object(p, 'native_browser_identity_failures', return_value=[]),
                patch.object(p, 'qmp_exit_evidence', return_value={'failures': [], 'shutdown_at': 500.0}),
                patch.object(p, 'user_manager_shutdown_evidence', return_value={'failures': []}),
                patch.object(p, 'method_evidence_probe', return_value={}),
                patch.object(p.f, 'shell', return_value={}), patch.object(p.f, 'observe_chrome', return_value={}),
                patch.object(p, 'final_save_evidence', side_effect=lambda *a: calls.append('final_save') or {'failures': []}),
                patch.object(p, 'shutdown_files_failures', side_effect=lambda *a: calls.append('files') or []),
                patch.object(p, 'snapshot_source_failures', side_effect=lambda *a: calls.append('source') or []),
                patch.object(harness, 'new_release_logind', return_value=(self.NEW, [])),
                patch.object(harness, 'published_snapshots', return_value=[self.snap(400)]),
                patch.object(harness, 'retained_failures', return_value=[]),
                patch.object(harness.p, 'startup_failures', return_value=[]),
            ]
            import contextlib
            import io
            import types
            import workspace_state.storage as storage
            import workspace_state.login_status as login_status
            with contextlib.ExitStack() as stack:
                for item in patches:
                    stack.enter_context(item)
                stack.enter_context(patch.object(storage, 'load', return_value={'browsers': {'google_chrome': {}}}))
                stack.enter_context(patch.object(login_status, 'status_path', return_value=directory / 'status.json'))
                (directory / 'status.json').write_text(json.dumps({'operation_state': 'completed', 'stages': []}))
                args = types.SimpleNamespace(installed_release='new-release', expect_failure=True,
                                             same_release_retry=False)
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    try:
                        harness.verify(args)
                    except RuntimeError:
                        pass
            return calls, json.loads(out.getvalue()) if out.getvalue() else {}

    def test_verify_requires_a_final_save_only_from_a_current_release(self):
        calls, result = self.run_verify(self.OLD)
        self.assertEqual(result.get('shutdown_release'), 'legacy')
        self.assertNotIn('final_save', calls)
        self.assertNotIn('files', calls)
        self.assertIsNone(result['final_save'])
        calls, result = self.run_verify(self.NEW)
        self.assertEqual(result.get('shutdown_release'), 'current')
        self.assertIn('final_save', calls)


if __name__ == '__main__':
    unittest.main()
