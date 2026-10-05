from __future__ import annotations

import copy
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


class PendingReturnProofTests(unittest.TestCase):
    def fixture(self, *, old=False):
        initial = {'provider_results': [{'item_id': f'Default/window-{i}',
            'identity': {'state': 'verified'}, 'content': {'state': 'waiting'},
            'placement': {'state': 'failed' if old else 'waiting',
                          **({} if old else {'request_id': f'continuation-{i}'})},
            'created': False, 'reused': True} for i in range(1, 4)]}
        final = copy.deepcopy(initial)
        for item in final['provider_results']:
            item['content']['state'] = 'verified'
            item['placement']['state'] = 'failed' if old else 'verified'
        return initial, final

    def test_pending_return_count_requires_distinct_production_items(self):
        self.assertEqual(harness.pending_returns('\n'.join([
            'Chrome Default/window-1: exact tab URLs are still loading',
            'Chrome Default/window-1: exact tab URLs are still loading',
            'Chrome Default/window-2: exact tab URLs could not be verified',
            'Chrome Default/window-3: exact tab URLs are still loading'])),
            ['Default/window-1', 'Default/window-3'])

    def test_verified_only_short_delay_cannot_pass_pending_proof(self):
        initial, final = self.fixture()
        for item in initial['provider_results']:
            item['content']['state'] = 'verified'
        self.assertTrue(harness.phase_problems(initial, final, expect_failure=False))

    def test_old_failure_and_candidate_continuation_prove_different_outcomes(self):
        self.assertEqual(harness.phase_problems(*self.fixture(old=True), expect_failure=True), [])
        self.assertEqual(harness.phase_problems(*self.fixture(), expect_failure=False), [])
        self.assertTrue(harness.phase_problems(*self.fixture(old=True), expect_failure=False))

    def test_missing_continuation_unverified_content_or_replacement_fails(self):
        for location, field, value in [('initial', 'request_id', None),
                                       ('final', 'content', 'waiting'),
                                       ('final', 'created', True)]:
            initial, final = self.fixture()
            target = initial if location == 'initial' else final
            item = target['provider_results'][0]
            if field == 'request_id':
                item['placement'].pop(field)
            elif field == 'content':
                item[field]['state'] = value
            else:
                item[field] = value
            self.assertTrue(harness.phase_problems(initial, final, expect_failure=False))


if __name__ == '__main__':
    unittest.main()
