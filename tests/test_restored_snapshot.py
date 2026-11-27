"""The status document names the snapshot a login restores from."""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import login_status, operations
from workspace_state.stage_state import stage_document, stage_verified


class RestoredSnapshotTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": root}))
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        operations.bind(None)
        login_status.initialize("a" * 16, login_key="b" * 16)

    def test_the_snapshot_is_a_top_level_field_and_the_schema_stays_one(self):
        self.assertTrue(login_status.record_restored_snapshot("2026-01-01T00:00:00+00:00"))
        document = json.loads(login_status.status_path().read_text())
        self.assertEqual(document["restored_snapshot"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(document["schema_version"], 1)
        self.assertEqual(len(document["operation_context"]), 6)

    def test_only_the_first_startup_of_an_operation_records_the_snapshot(self):
        self.assertTrue(login_status.record_restored_snapshot("S1"))
        self.assertTrue(login_status.record_restored_snapshot("S2"))
        self.assertEqual(json.loads(login_status.status_path().read_text())["restored_snapshot"], "S1")

    def test_a_missing_snapshot_time_is_never_recorded_as_text(self):
        for value in (None, "", 5):
            self.assertFalse(login_status.record_restored_snapshot(value))
        self.assertNotIn("restored_snapshot", json.loads(login_status.status_path().read_text()))

    def test_a_stage_that_is_not_final_is_never_verified(self):
        verified = {"state": "verified"}
        item = {"provider": "chrome", "item_id": "a", "identity": verified, "content": verified,
                "placement": verified}
        for state in ("failed", "waiting", "running", "pending"):
            with self.subTest(state=state):
                self.assertFalse(stage_verified({"state": state, "provider_results": [item]}))
        self.assertTrue(stage_verified({"state": "ready", "provider_results": [item]}))

    def test_a_stage_is_verified_by_its_evidence_not_its_displayed_state(self):
        self.assertFalse(stage_verified(stage_document({}, "browsers")))
        self.assertTrue(stage_verified({"state": "skipped"}))
        self.assertFalse(stage_verified({"state": "degraded"}))
        verified = {"state": "verified"}
        item = {"provider": "chrome", "item_id": "a", "identity": verified, "content": verified,
                "placement": verified, "attention": ["Advisory: old recipe"]}
        # An advisory note shows the stage as degraded but restored everything.
        self.assertTrue(stage_verified({"state": "degraded", "provider_results": [item]}))
        item["placement"] = {"state": "failed", "detail": "gone"}
        self.assertFalse(stage_verified({"state": "ready", "provider_results": [item]}))
