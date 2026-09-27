from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import alerts


class AlertProbeDeadlineTests(unittest.TestCase):
    def test_global_deadline_terminates_probe_process_instead_of_joining_its_long_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'probe.pid'
            def blocked(source, since):
                path.write_text(str(os.getpid()))
                time.sleep(10)
                raise AssertionError('probe outlived the budget')
            started = time.monotonic()
            with patch.object(alerts, 'safe_probe', side_effect=blocked), patch.object(alerts, 'SCAN_BUDGET', 0.1):
                result = alerts._bounded_probes([{'id': 'one'}], {'one': 123})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 1.0)
            self.assertEqual(result[0]['coverage'], 'scan-timeout')
            self.assertEqual(result[0]['since'], 123)
            self.assertEqual(result[0]['resolved'], [])
            self.assertTrue(path.exists(), 'probe should have started before cancellation')
            self.assertFalse(Path('/proc') .joinpath(path.read_text()).exists())

    def test_completed_probe_results_survive_while_queued_work_times_out(self):
        def probe(source, since):
            if source['id'] != 'fast':
                time.sleep(10)
            return {'health': 'healthy', 'coverage': 'checked', 'issues': [], 'resolved': [], 'since': since, 'checked_at': 'now'}
        sources = [{'id': name} for name in ('fast', 'slow1', 'slow2', 'slow3', 'slow4', 'queued')]
        with patch.object(alerts, 'safe_probe', side_effect=probe), patch.object(alerts, 'SCAN_BUDGET', 0.1):
            results = alerts._bounded_probes(sources, {})
        self.assertEqual(results[0]['health'], 'healthy')
        self.assertTrue(all(item['coverage'] == 'scan-timeout' for item in results[1:]))


if __name__ == '__main__':
    unittest.main()
