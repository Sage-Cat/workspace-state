from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_pending_test_module', HERE / 'run_vm_pending.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


def item(number, **changes):
    value = {'item_id': f'Default/window-{number}', 'created': False, 'reused': True,
             'identity': {'state': 'verified'}, 'content': {'state': 'verified'},
             'placement': {'state': 'verified'}}
    value.update(changes)
    return value


class SlowPagePlacementProofTests(unittest.TestCase):
    def test_placed_windows_with_gated_pages_pass(self):
        self.assertEqual(harness.placement_problems([item(number) for number in range(1, 4)]), [])

    def test_too_few_windows_cannot_pass(self):
        self.assertTrue(harness.placement_problems([item(1), item(2)]))

    def test_waiting_content_or_placement_fails(self):
        for phase in ('content', 'placement', 'identity'):
            results = [item(number) for number in range(1, 4)]
            results[0][phase] = {'state': 'waiting'}
            self.assertTrue(harness.placement_problems(results), phase)

    def test_a_created_window_fails(self):
        results = [item(number) for number in range(1, 4)]
        results[0]['created'] = True
        self.assertTrue(harness.placement_problems(results))


if __name__ == '__main__':
    unittest.main()
