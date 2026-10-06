from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('capture_binding_harness', HERE / 'run_vm_capture_binding.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class CaptureBindingHarnessTests(unittest.TestCase):
    def test_measure_real_producer_timestamps_without_rewriting_them(self):
        snapshot = {'created_at': '2026-06-01T11:00:03+03:00',
                    'capture_context': {'captured_at': '2026-06-01T08:00:00.125+00:00'},
                    'browsers': {'google_chrome': {'latest_observation': {
                        'schema_version': 1, 'captured_at': '2026-06-01T11:00:03+03:00'}}}}
        self.assertEqual(harness.measured_binding(snapshot)['observation_minus_start_seconds'], 2.875)
        snapshot['browsers']['google_chrome']['latest_observation'].update(
            schema_version=2, captured_at='2026-06-01T08:00:04.500+00:00')
        self.assertEqual(harness.measured_binding(snapshot)['observation_minus_start_seconds'], 4.375)


if __name__ == '__main__':
    unittest.main()
