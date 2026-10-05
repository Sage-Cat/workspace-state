from __future__ import annotations

import base64
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_retry_test_module', HERE / 'run_vm_retry.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class RetryEvidenceTests(unittest.TestCase):
    def records(self):
        context = {'mode': 'shutdown', 'operation_id': 'a' * 32,
                   'boot_id': 'old-boot', 'login_generation': 'old-login'}
        status = {'operation_id': context['operation_id'], 'operation_context': context}
        ledger = {'schema_version': 1, 'operation_context': dict(context),
                  'settled': True, 'status': 'failed', 'requests': {'app.service': 'done'}}
        return status, ledger

    def test_partial_drain_needs_exact_settled_owner(self):
        status, ledger = self.records()
        self.assertEqual(harness.owned_ledger(status, ledger, terminal='failed'), status['operation_context'])
        for change in ({'settled': False}, {'status': 'running'},
                       {'requests': {'app.service': 'issuing'}},
                       {'operation_context': dict(status['operation_context'], operation_id='b' * 32)}):
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                harness.owned_ledger(status, ledger | change, terminal='failed')

    def test_final_success_cannot_be_replaced_by_failed_settlement(self):
        status, ledger = self.records()
        with self.assertRaises(RuntimeError):
            harness.owned_ledger(status, ledger, terminal='succeeded')
        harness.owned_ledger(status, ledger | {'status': 'succeeded'}, terminal='succeeded')

    def test_bundle_payload_requires_independent_digest(self):
        raw = b'synthetic verified checkpoint\n'
        record = {'bytes': base64.b64encode(raw).decode(), 'digest': hashlib.sha256(raw).hexdigest()}
        self.assertEqual(harness.checkpoint_bytes({'canonical': record}, 'canonical'), raw)
        with self.assertRaises(RuntimeError):
            harness.checkpoint_bytes({'canonical': record | {'bytes': base64.b64encode(b'empty').decode()}}, 'canonical')


if __name__ == '__main__':
    unittest.main()
