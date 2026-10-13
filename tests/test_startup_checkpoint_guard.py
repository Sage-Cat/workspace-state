from __future__ import annotations

import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import operations, startup_control
from workspace_state.util import atomic_json


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


def snapshot(*identities):
    return {"sessions": [{"name": "synthetic", "windows": [{"panes": [
        {"codex": {"session_id": identity}} for identity in identities
    ]}]}]}


class StartupCheckpointGuardTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        env = patch.dict(os.environ, {"XDG_RUNTIME_DIR": temporary.name})
        env.start(); self.addCleanup(env.stop)
        self.root = Path(temporary.name) / "workspace-state"
        self.root.mkdir(mode=0o700)
        self.context = operations.OperationContext.create("a" * 16, "startup")
        (self.root / "login-generation").write_text(self.context.login_generation)
        self.receipt = self.root / "startup-abort.json"
        self.record = self.root / "codex-starts" / f"{FIRST}.json"
        self.previous = snapshot(FIRST, SECOND)
        self.live = snapshot(SECOND)

    def write_receipt(self, context=None, **changes):
        context = context or self.context
        atomic_json(self.receipt, {"schema_version": 1, "mode": "startup",
            "session_id": context.login_generation, "operation_id": context.operation_id,
            "operation_context": context.to_dict(), "requested_at": "2026-01-01T00:00:00Z", **changes})

    def write_record(self, context=None, **changes):
        context = context or self.context
        atomic_json(self.record, {"session_id": FIRST, "state": "aborted-before-launch",
            "operation_context": context.to_dict(), "pid": 123, "start_ticks": "456", "pane": "%7", **changes})

    def check(self):
        startup_control.check_shutdown_checkpoint(self.previous, self.live)

    def test_exact_cancelled_queued_ids_block_replacement_without_mutating_snapshots(self):
        self.write_receipt(); self.write_record()
        before = copy.deepcopy((self.previous, self.live))
        with self.assertRaisesRegex(RuntimeError, "previous checkpoint is preserved.*recover.*manual wsctl save"):
            self.check()
        self.assertEqual((self.previous, self.live), before)

    def test_captured_ids_are_not_replaced_or_inferred_from_abort_evidence(self):
        self.write_receipt(); self.write_record()
        self.live = snapshot(FIRST, SECOND)
        self.check()
        self.assertEqual(self.live, snapshot(FIRST, SECOND))
        self.previous = None; self.live = snapshot()
        self.check()

    def test_no_abort_or_unrelated_record_does_not_change_ordinary_capture_policy(self):
        self.write_record(); self.check()
        self.write_receipt(); self.record.unlink()
        atomic_json(self.record.with_name(f"{SECOND}.json"), {"session_id": SECOND,
            "state": "aborted-before-launch", "operation_context": self.context.to_dict()})
        self.check()

    def test_stale_boot_and_login_evidence_does_not_block_current_checkpoint(self):
        for context in (replace(self.context, boot_id="previous-boot"),
                        replace(self.context, login_generation="b" * 16)):
            with self.subTest(context=context):
                self.write_receipt(context); self.write_record(context)
                self.check()

    def test_full_record_context_must_match_every_receipt_field(self):
        self.write_receipt()
        for field, value in (("boot_id", "previous-boot"), ("login_generation", "b" * 16),
                             ("operation_id", "other-operation"), ("attempt", 2),
                             ("deadline", self.context.deadline + 1), ("mode", "shutdown")):
            with self.subTest(field=field):
                self.write_record(replace(self.context, **{field: value}))
                self.check()

    def test_expired_work_deadline_still_protects_cancelled_saved_intent(self):
        expired = replace(self.context, deadline=1)
        self.write_receipt(expired); self.write_record(expired)
        with self.assertRaises(RuntimeError): self.check()

    def test_incomplete_exact_owned_queue_records_block_capture_race(self):
        self.write_receipt()
        for state in ("queued", "waiting-directory", "starting", "exited", "unverified"):
            with self.subTest(state=state):
                self.write_record(state=state)
                with self.assertRaisesRegex(RuntimeError, "records are still incomplete"):
                    self.check()

    def test_ready_and_independent_manual_records_do_not_grant_cancelled_ownership(self):
        self.write_receipt()
        self.write_record(state="ready"); self.check()
        self.write_record(state="exited", operation_context=None); self.check()

    def test_matching_incomplete_cancelled_record_gets_recoverable_diagnostic(self):
        self.write_receipt()
        self.write_record(operation_context=None)
        with self.assertRaisesRegex(RuntimeError, "Incomplete cancelled Codex startup record"):
            self.check()
        self.write_record(session_id=SECOND)
        with self.assertRaisesRegex(RuntimeError, "Inconsistent cancelled Codex startup record"):
            self.check()

    def test_missing_login_identity_is_not_treated_as_authority_to_drop_saved_ids(self):
        self.write_receipt(); self.write_record()
        (self.root / "login-generation").unlink()
        with self.assertRaisesRegex(RuntimeError, "Cannot verify the current login"):
            self.check()

    def test_inconsistent_or_malformed_receipt_never_silently_allows_replacement(self):
        for changes in ({"session_id": "other-login"}, {"operation_id": "other-operation"},
                        {"schema_version": 2}, {"operation_context": {"mode": "startup"}}):
            with self.subTest(changes=changes):
                self.write_receipt(**changes)
                with self.assertRaises(RuntimeError): self.check()
        self.receipt.write_text("{")
        with self.assertRaisesRegex(RuntimeError, "Cannot verify startup cancellation evidence"):
            self.check()

    def test_unsafe_receipt_and_record_files_cannot_authorize_checkpoint_loss(self):
        self.write_receipt(); self.write_record()
        self.receipt.chmod(0o644)
        with self.assertRaises(RuntimeError): self.check()
        self.write_receipt(); self.record.chmod(0o644)
        with self.assertRaises(RuntimeError): self.check()
        self.write_record(); target = self.record.with_suffix(".target")
        self.record.rename(target); self.record.symlink_to(target)
        with self.assertRaises(RuntimeError): self.check()

    def test_oversized_and_malformed_target_records_get_diagnostics(self):
        self.write_receipt(); self.write_record()
        self.record.write_text(" " * 8193)
        with self.assertRaises(RuntimeError): self.check()
        self.record.write_text(json.dumps([]))
        with self.assertRaises(RuntimeError): self.check()

    def test_snapshot_missing_invalid_id_never_reads_an_arbitrary_runtime_path(self):
        self.write_receipt(); self.previous = snapshot("../../other-file")
        self.live = snapshot()
        with self.assertRaisesRegex(RuntimeError, "Invalid saved Codex conversation identity"):
            self.check()

    def test_shutdown_save_stops_before_any_checkpoint_write(self):
        from argparse import Namespace
        from contextlib import nullcontext
        from workspace_state import cli
        self.write_receipt(); self.write_record()
        with patch.object(cli, "_shutdown_allows_unresolved_codex", return_value=True), \
             patch.object(cli, "state_lock", return_value=nullcontext()), \
             patch.object(cli, "_checkpoint_login", return_value=None), \
             patch.object(cli, "load", return_value=self.previous), \
             patch.object(cli, "configured_apps", return_value=[]), \
             patch.object(cli, "update_stage"), \
             patch.object(cli, "_capture_all", return_value=self.live), \
             patch.object(cli, "save") as write:
            with self.assertRaisesRegex(RuntimeError, "previous checkpoint is preserved"):
                cli.cmd_save(Namespace(shutdown_safe=True, allow_partial=True))
            write.assert_not_called()

    def test_periodic_tmux_save_retains_queued_cancelled_recipe(self):
        from workspace_state import cli
        self.write_receipt(); self.write_record()
        with patch("workspace_state.shutdown_checkpoint_guard.protected", return_value=False), \
             patch.object(cli, "load", return_value=self.previous) as read, \
             patch.object(cli, "capture", return_value=self.live):
            self.assertEqual(cli.cmd_tmux_save_allowed(None), 1)
            read.return_value = self.live
            self.assertEqual(cli.cmd_tmux_save_allowed(None), 0)


if __name__ == "__main__":
    unittest.main()
