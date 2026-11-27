"""A Chrome recipe that capture keeps failing to refresh is reported as stale."""
import argparse
import io
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from workspace_state import browser_reconciliation as reconciliation, cli
from workspace_state.provider_results import EvidenceState, ProviderCount
from workspace_state.provider_progress import evidence_state

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def snapshot(age_minutes, state="retained"):
    record = {"state": state, "captured_at": (NOW - timedelta(minutes=age_minutes)).isoformat(),
              "retained_reason": "Chrome windows changed while matching titles"}
    return {"category_provenance": {"browsers": record}}


class RecipeAgeTests(unittest.TestCase):
    def test_only_an_old_retained_recipe_is_stale(self):
        limit = reconciliation.STALE_RECIPE_SECONDS // 60
        self.assertIsNone(reconciliation.stale_recipe_message(snapshot(limit - 1), now=NOW))
        self.assertIsNone(reconciliation.stale_recipe_message(snapshot(600, "captured"), now=NOW))
        self.assertIsNone(reconciliation.stale_recipe_message({}, now=NOW))
        message = reconciliation.stale_recipe_message(snapshot(19 * 60 + 5), now=NOW)
        self.assertIn("19 h 5 min old", message)
        self.assertIn("windows changed while matching titles", message)

    def test_restore_marks_the_chrome_category_as_needing_attention(self):
        args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=False)
        ready = ProviderCount(1, ())
        with patch.object(cli, "_restore_browsers", return_value=ready), \
                patch.object(cli, "stale_recipe_message", return_value="Chrome recipe is 19 h 5 min old"), \
                redirect_stderr(io.StringIO()):
            result = cli._restore(snapshot(0), args)["browsers"]
        self.assertEqual(int(result), 1)
        item, = result.results
        self.assertEqual(item.attention, ("Chrome recipe is 19 h 5 min old",))
        self.assertFalse(item.success)
        self.assertEqual(evidence_state([item.to_dict()]), "failed")

    def test_dry_run_and_empty_restores_add_nothing(self):
        for dry_run, count in ((True, 1), (False, 0)):
            args = argparse.Namespace(category="browsers", session=None, select=None, dry_run=dry_run)
            with patch.object(cli, "_restore_browsers", return_value=ProviderCount(count, ())), \
                    patch.object(cli, "stale_recipe_message", return_value="old"):
                result = cli._restore({}, args)["browsers"]
            self.assertEqual(getattr(result, "results", ()), ())

if __name__ == "__main__":
    unittest.main()
