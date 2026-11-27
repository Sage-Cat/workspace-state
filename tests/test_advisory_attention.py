"""Advisory attention through the real status and HUD machinery."""
import unittest
from unittest.mock import patch

import test_provider_progress as base
from workspace_state import cli, operations, provider_progress as progress, stage_state
from workspace_state.provider_results import (ADVISORY_PREFIX, RECIPE_AGE_ITEM, EvidenceState as S,
                                              PhaseEvidence as E, ProviderCount, ProviderItemResult,
                                              ProviderRestoreError)

AGE = ProviderItemResult("browsers", RECIPE_AGE_ITEM, E(S.SKIPPED), E(S.SKIPPED), E(S.SKIPPED),
                         attention=(ADVISORY_PREFIX + "lagged 1 h 0 min behind the desktop",))
GOOD = ProviderItemResult("chrome", "Default/window-1", E(S.VERIFIED), E(S.VERIFIED), E(S.VERIFIED))
FAILED = ProviderItemResult("chrome", "Default/window-1", E(S.FAILED, "tabs did not load", True),
                            E(S.SKIPPED), E(S.SKIPPED))
FINAL = "Restored 1/1 Chrome window(s); native window identities changed during verification"


def unplaced(number):
    return ProviderItemResult("chrome", f"Default/window-{number}", E(S.VERIFIED), E(S.VERIFIED), E(S.SKIPPED),
                              attention=(ADVISORY_PREFIX + "saved placement is unknown",))


class AdvisoryAttentionTests(unittest.TestCase):
    # The status fixture of the provider progress tests, without rerunning its tests.
    def setUp(self):
        base.ProviderProgressTests.setUp(self)
        # These tests bind their own contexts; none may outlive its test.
        self.addCleanup(operations.bind, None)

    document = base.ProviderProgressTests.document
    stage = base.ProviderProgressTests.stage
    item = base.ProviderProgressTests.item
    seed = base.ProviderProgressTests.seed

    def observed(self):
        stage = self.stage()
        login = {"boot_id": self.context.boot_id, "login_generation": self.context.login_generation,
                 "login_key": cli._login_key_file()}
        # What was restored, without the advisory notes that only change what is shown.
        real = (stage_state.stage_state(stage["provider_results"], stage.get("provider_error")).real
                if stage.get("provider_results") else stage["state"])
        return stage, real, cli._browsers_capturable(login)

    def verify_pending(self):
        with patch.object(progress, "_query_request",
                          return_value={"token": "native-handoff:tok", "status": "verified"}):
            progress.reconcile_pending()

    def test_an_age_note_arriving_with_a_waiting_window_keeps_capture_open_after_the_async_rewrite(self):
        self.seed([self.item(), AGE])
        with patch.object(progress, "_query_request", return_value={"token": "request-1", "status": "verified"}):
            progress.reconcile_pending()
        stage, real, capturable = self.observed()
        self.assertEqual((stage["state"], stage["current"], stage["total"]), ("degraded", 1, 1))
        self.assertEqual(real, "ready")
        self.assertTrue(stage_state.stage_verified(stage))
        self.assertTrue(capturable)

    def test_the_restored_state_survives_another_category_settling_later(self):
        social = ProviderItemResult("social-apps", "slack:1", E(S.VERIFIED), E(S.SKIPPED),
                                    E(S.WAITING, "Awaiting", True, "native-handoff:tok"))
        self.seed([social], category="social-apps")
        operations.bind(self.context)
        cli._publish_category_outcome("browsers", {"created_at": "saved"}, count=ProviderCount(1, (GOOD, AGE)))
        stage, real, capturable = self.observed()
        self.assertEqual((stage["state"], real, capturable), ("degraded", "ready", True))
        self.verify_pending()
        stage, real, capturable = self.observed()
        self.assertEqual((stage["state"], real, capturable), ("degraded", "ready", True))

    def test_unplaced_windows_degrade_the_stage_but_are_counted_as_restored(self):
        operations.bind(self.context)
        cli._publish_category_outcome("browsers", {"created_at": "saved"},
                                      count=ProviderCount(2, (GOOD, unplaced(2))))
        stage, real, capturable = self.observed()
        self.assertEqual((stage["state"], stage["current"], stage["total"]), ("degraded", 2, 2))
        self.assertIn("1 of 2 item(s) need attention", stage["message"])
        self.assertNotIn("wsctl save", stage["message"])
        self.assertEqual((real, capturable), ("ready", True))

    def test_a_real_error_is_never_hidden_by_advisory_items(self):
        for items in ((GOOD,), (GOOD, AGE), (GOOD, unplaced(2))):
            with self.subTest(items=[item.item_id for item in items]):
                operations.bind(self.context)
                returned = cli._publish_category_outcome(
                    "browsers", {"created_at": "saved"}, error=ProviderRestoreError(FINAL, items))
                stage, real, capturable = self.observed()
                self.assertEqual((returned, stage["state"], real, capturable), ("failed", "failed", "failed", False))
                self.assertIn("identities changed", stage["message"])
                self.assertIn("identities changed", stage["error"])

    def test_windows_that_all_failed_stay_failed_next_to_the_age_note(self):
        for items in ((FAILED,), (FAILED, AGE)):
            with self.subTest(items=[item.item_id for item in items]):
                operations.bind(self.context)
                returned = cli._publish_category_outcome(
                    "browsers", {"created_at": "saved"},
                    error=ProviderRestoreError("Restored 0/1 Chrome window(s); tabs did not load", items))
                stage, real, capturable = self.observed()
                self.assertEqual((returned, stage["state"], real, capturable), ("failed", "failed", "failed", False))
                self.assertIn("error", stage)
                self.assertEqual(stage["current"], 0)
                self.assertIn("0/", stage["message"])

    def test_a_failed_stage_does_not_count_an_unplaced_window_as_verified(self):
        document = {"mode": "startup", "stages": [{"id": "browsers", "state": "failed", "provider_error": "boom",
                    "provider_results": [item.to_dict() for item in (unplaced(1), GOOD)]}]}
        progress.refresh_stage_evidence(document)
        stage = document["stages"][0]
        self.assertEqual((stage["state"], stage["current"], stage["total"]), ("failed", 1, 2))
        self.assertIn("1/2 verified", stage["message"])

    def test_an_asynchronously_failed_window_stays_failed_next_to_the_age_note(self):
        self.seed([self.item(), AGE])
        with patch.object(progress, "_query_request", return_value={"token": "request-1", "status": "failed"}):
            progress.reconcile_pending()
        stage, real, capturable = self.observed()
        self.assertEqual((stage["state"], real, capturable), ("failed", "failed", False))
        self.assertEqual(self.document().get("operation_state"), "failed")

    def test_advisory_items_do_not_hold_back_waiting_or_recreation_checks(self):
        from workspace_state.provider_results import waiting_only
        waiting = ProviderItemResult("chrome", "w", E(S.VERIFIED), E(S.VERIFIED), E(S.WAITING, "x", True, "t"))
        self.assertTrue(waiting_only((waiting, unplaced(2), AGE)))
        self.assertFalse(waiting_only((waiting, FAILED)))

    def test_advisory_alone_never_fails_and_rolls_up_as_degraded(self):
        for label, items, shown in (("unplaced only", (unplaced(1), unplaced(2)), "2/2"),
                                    ("verified + age", (GOOD, AGE), "1/1"),
                                    ("unplaced + age", (unplaced(1), AGE), "1/1")):
            with self.subTest(label):
                stages = [{"id": "workspace", "state": "running", "provider_completion_pending": True}]
                for name in progress.CATEGORY_STAGES:
                    stage = {"id": name, "state": "ready"}
                    if name == "browsers":
                        stage.update(state="degraded", provider_results=[item.to_dict() for item in items])
                    stages.append(stage)
                document = {"mode": "startup", "stages": stages}
                progress.refresh_stage_evidence(document)
                by_id = {stage["id"]: stage for stage in document["stages"]}
                browsers = by_id["browsers"]
                self.assertEqual(browsers["state"], "degraded")
                self.assertEqual(f"{browsers['current']}/{browsers['total']}", shown)
                self.assertNotIn("wsctl save", browsers["message"])
                self.assertNotIn(ADVISORY_PREFIX, browsers["message"])
                self.assertNotIn("error", browsers)
                self.assertEqual(by_id["workspace"]["state"], "degraded")

    def test_a_partly_failed_stage_still_asks_for_a_save_and_names_the_real_failure(self):
        failed = ProviderItemResult("chrome", "Default/window-3", E(S.FAILED, "tabs did not load"),
                                    E(S.SKIPPED), E(S.SKIPPED))
        document = {"mode": "startup", "stages": [{"id": "browsers", "state": "degraded", "provider_results": [
            item.to_dict() for item in (GOOD, failed, AGE)]}]}
        progress.refresh_stage_evidence(document)
        stage = document["stages"][0]
        self.assertEqual((stage["state"], stage["current"], stage["total"]), ("degraded", 1, 2))
        self.assertIn("tabs did not load", stage["message"])
        self.assertIn("wsctl save", stage["message"])


if __name__ == "__main__":
    unittest.main()
