"""A Chrome recipe that capture kept failing to refresh is reported, never trapped."""
import argparse
import io
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, cli
from workspace_state.provider_progress import evidence_state
from workspace_state.provider_results import (ADVISORY_PREFIX, EvidenceState as S, PhaseEvidence as P,
                                              ProviderCount, ProviderItemResult, ProviderRestoreError,
                                              RECIPE_AGE_ITEM)
from workspace_state.startup import StageMarker, write_stage_marker

BASE = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def snapshot(lag_minutes, state="retained", *, published_after_hours=0):
    published = BASE + timedelta(minutes=lag_minutes, hours=published_after_hours)
    record = {"state": state, "captured_at": BASE.isoformat(),
              "retained_reason": "Chrome windows changed while matching titles"}
    return {"created_at": published.isoformat(), "category_provenance": {"browsers": record}}


def verified(label="Default/window-1"):
    return ProviderItemResult("chrome", label, P(S.VERIFIED), P(S.VERIFIED), P(S.VERIFIED))


class RecipeAgeTests(unittest.TestCase):
    def test_only_a_long_lag_of_a_retained_recipe_is_stale(self):
        limit = reconciliation.STALE_RECIPE_SECONDS // 60
        self.assertIsNone(reconciliation.stale_recipe_message(snapshot(limit - 1)))
        self.assertIsNone(reconciliation.stale_recipe_message(snapshot(600, "captured")))
        self.assertIsNone(reconciliation.stale_recipe_message({}))
        message = reconciliation.stale_recipe_message(snapshot(19 * 60 + 5))
        self.assertTrue(message.startswith(ADVISORY_PREFIX))
        self.assertIn("19 h 5 min", message)

    def test_time_between_the_last_save_and_reading_it_is_not_staleness(self):
        # Retained five minutes before logout; read ten hours later.
        self.assertIsNone(reconciliation.stale_recipe_message(snapshot(5)))

    def restore(self, items, *, stale="Advisory: Chrome recipe lagged 19 h"):
        args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=False)
        with patch.object(cli, "_restore_browsers", return_value=ProviderCount(len(items), items)), \
                patch.object(cli, "stale_recipe_message", return_value=stale), redirect_stderr(io.StringIO()):
            return cli._restore(snapshot(0), args)["browsers"]

    def test_restore_adds_the_age_note_and_a_failed_window_does_not_hide_it(self):
        result = self.restore((verified(),))
        self.assertEqual([item.item_id for item in result.results], ["Default/window-1", RECIPE_AGE_ITEM])
        failed = ProviderItemResult("chrome", "Default/window-2", P(S.FAILED), P(S.SKIPPED), P(S.SKIPPED))
        args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=False)
        with patch.object(cli, "_restore_browsers", side_effect=ProviderRestoreError("x", (verified(), failed))), \
                patch.object(cli, "stale_recipe_message", return_value="Advisory: old"), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(ProviderRestoreError) as raised:
                cli._restore(snapshot(0), args)
        self.assertEqual([item.item_id for item in raised.exception.results][-1], RECIPE_AGE_ITEM)

    def test_the_age_note_degrades_the_stage_without_counting_as_an_item(self):
        results = tuple(verified(f"Default/window-{index}") for index in range(1, 12))
        result = self.restore(results)
        document = {"stages": [{"id": "browsers", "state": "running",
                                "provider_results": [item.to_dict() for item in result.results]}]}
        from workspace_state import provider_progress
        provider_progress.refresh_stage_evidence(document)
        stage = document["stages"][0]
        self.assertEqual((stage["state"], stage["current"], stage["total"]), ("degraded", 11, 11))
        self.assertIn("0 of 11 item(s) need attention", stage["message"])
        self.assertEqual(evidence_state([item.to_dict() for item in result.results]), "failed")

    def test_login_then_periodic_capture_is_not_blocked_by_the_age_note(self):
        result = self.restore((verified(),))
        written = {}

        def marker(path, category, snap, state, results=(), message=""):
            written.update(state=state, results=tuple(item.to_dict() for item in results))
        with patch.object(cli, "update_stage") as stage, patch.object(cli, "publish_provider_results"), \
                patch.object(cli, "_record_seen"), patch.object(cli, "_write_attempt_marker", side_effect=marker), \
                patch("workspace_state.placement_guard.record"):
            state = cli._publish_category_outcome("browsers", snapshot(0), count=result)
        self.assertEqual(state, "degraded")
        self.assertEqual(stage.call_args.args[1], "degraded")
        self.assertEqual(written["state"], "ready")
        boot, generation = "0f0e0d0c-0000-4000-8000-000000000001", "abcdef01"
        login = {"boot_id": boot, "login_generation": generation}
        context = {"boot_id": boot, "login_generation": generation, "operation_id": "op1",
                   "mode": "startup", "attempt": 1, "deadline": time.monotonic() + 600}
        directory = cli._seen_directory(login)
        write_stage_marker(directory / "browsers.done",
                           StageMarker("browsers", written["state"], "", "", context, written["results"]))
        self.assertTrue(cli._browsers_capturable(login))

    def test_dry_run_and_empty_restores_add_nothing(self):
        for dry_run, count in ((True, 1), (False, 0)):
            args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=dry_run)
            with patch.object(cli, "_restore_browsers", return_value=ProviderCount(count, ())), \
                    patch.object(cli, "stale_recipe_message", return_value="Advisory: old"):
                result = cli._restore({}, args)["browsers"]
            self.assertEqual(getattr(result, "results", ()), ())


if __name__ == "__main__":
    unittest.main()
