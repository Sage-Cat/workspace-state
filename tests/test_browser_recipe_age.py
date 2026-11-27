"""A Chrome recipe that capture kept failing to refresh is reported, never trapped."""
import argparse
import io
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, cli
from workspace_state.provider_results import (ADVISORY_PREFIX, EvidenceState as S, PhaseEvidence as P,
                                              ProviderCount, ProviderItemResult, ProviderRestoreError,
                                              RECIPE_AGE_ITEM)

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

    def restore(self, items, *, stale="Advisory: lagged 19 h"):
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

    def test_dry_run_and_empty_restores_add_nothing(self):
        for dry_run, count in ((True, 1), (False, 0)):
            args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=dry_run)
            with patch.object(cli, "_restore_browsers", return_value=ProviderCount(count, ())), \
                    patch.object(cli, "stale_recipe_message", return_value="Advisory: old"):
                result = cli._restore({}, args)["browsers"]
            self.assertEqual(getattr(result, "results", ()), ())


if __name__ == "__main__":
    unittest.main()
