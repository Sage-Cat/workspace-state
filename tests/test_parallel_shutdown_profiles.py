from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import shutdown_profiles as module


class _Never:
    def requested(self) -> bool:
        return False


class _Adapter:
    def __init__(self, active: set[str], entered: threading.Barrier, counts: list[int]):
        self.active = active
        self.entered = entered
        self.counts = counts
        self.count_lock = threading.Lock()

    def probe(self, runtime):
        return runtime.profile.identifier in self.active, "active"

    def prepare(self, runtime, cancel):
        with self.count_lock:
            self.counts[0] += 1
            self.counts[1] = max(self.counts[1], self.counts[0])
        self.entered.wait(timeout=2)
        time.sleep(0.01)
        with self.count_lock:
            self.counts[0] -= 1
        return "prepared"

    def verify(self, runtime, cancel):
        return "verified"

    def rollback(self, runtime):
        return "restored"


class ParallelShutdownProfilesTests(unittest.TestCase):
    def test_defaults_and_legacy_mapping(self):
        raw = {
            "schema_version": 1, "id": "job", "label": "Job", "adapter": "command",
            "probe": ["/usr/bin/true"], "prepare": ["/usr/bin/true"],
            "verify": ["/usr/bin/true"], "rollback": ["/usr/bin/true"],
        }
        profile = module._profile_from_mapping(raw)
        self.assertFalse(profile.parallel)
        self.assertNotIn("parallel", module._profile_mapping(profile))
        profile = module._profile_from_mapping({**raw, "parallel": True})
        self.assertTrue(profile.parallel)
        self.assertTrue(module._profile_mapping(profile)["parallel"])

    def test_parallel_batch_is_journaled_before_workers_and_capped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(root)}, clear=False):
                profiles = [module._profile_from_mapping({
                    "schema_version": 1, "id": f"job-{index}", "label": str(index),
                    "adapter": "command", "parallel": True,
                    "probe": ["/usr/bin/true"], "prepare": ["/usr/bin/true"],
                    "verify": ["/usr/bin/true"], "rollback": ["/usr/bin/true"],
                }) for index in range(4)]
                barrier = threading.Barrier(4)
                counts = [0, 0]
                adapters = [_Adapter({p.identifier for p in profiles}, barrier, counts)
                            for _ in profiles]
                with patch.object(module, "_adapter_for", side_effect=adapters):
                    session = module.ShutdownProfileSession(
                        profiles, operation_id="a" * 32, session_id="b" * 16,
                        action="poweroff", cancel=_Never(), reporter=lambda *_: None,
                    )
                    session.run()
                self.assertLessEqual(counts[1], 4)
                document = json.loads(module.transaction_path().read_text())
                self.assertEqual(len(document["profiles"]), 4)


if __name__ == "__main__":
    unittest.main()
