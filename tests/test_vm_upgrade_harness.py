from __future__ import annotations

import copy
import importlib.util
from pathlib import Path
import sys
import unittest

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
        checkpoint = copy.deepcopy(original)
        checkpoint['browsers']['google_chrome']['latest_observation'] = {
            'schema_version': 1, 'captured_at': '2026-10-04T09:00:00+00:00',
            'browser_state': copy.deepcopy(expected['browsers']['google_chrome'])}
        return original, expected, checkpoint

    def test_real_retained_pair_accepted(self):
        self.assertEqual(harness.retained_failures(*self.fixture()), [])

    def test_adopted_checkpoint_is_not_upgrade_proof(self):
        original, expected, checkpoint = self.fixture()
        checkpoint['browsers']['google_chrome']['profiles'] = expected['browsers']['google_chrome']['profiles']
        self.assertIn('Shutdown did not retain the original browser recipe',
                      harness.retained_failures(original, expected, checkpoint))

    def test_missing_observation_fails(self):
        original, expected, checkpoint = self.fixture()
        del checkpoint['browsers']['google_chrome']['latest_observation']
        self.assertTrue(harness.retained_failures(original, expected, checkpoint))

    def test_observation_lost_group_or_tab_is_not_exact(self):
        for field in ('tabs', 'groups'):
            original, expected, checkpoint = self.fixture()
            checkpoint['browsers']['google_chrome']['latest_observation']['browser_state']['profiles'][0]['windows'][0][field] = []
            self.assertIn('Retained observation does not preserve the newer exact browser catalog',
                          harness.retained_failures(original, expected, checkpoint))

    def test_unchanged_browser_is_not_upgrade_proof(self):
        original, _, checkpoint = self.fixture()
        self.assertIn('No newer browser changes were measured',
                      harness.retained_failures(original, original, checkpoint))


if __name__ == '__main__':
    unittest.main()
