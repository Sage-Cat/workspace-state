from __future__ import annotations

import argparse
import copy
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class SaveRecipeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.root / "runtime")}))
        self.stack.enter_context(patch.object(cli, "_startup_marker", side_effect=lambda category: self.root / f"{category}.done"))
        self.stack.enter_context(patch.object(cli, "state_lock"))
        from workspace_state import operations
        self.startup_context = operations.OperationContext.create("recipe-test", "startup")
        login_context = operations.OperationContext.create("recipe-test", "startup")
        self.stack.enter_context(patch.object(cli, "_marker_context", return_value=login_context))
        self.stages = self.stack.enter_context(patch.object(cli, "update_stage"))
        self.save = self.stack.enter_context(patch.object(cli, "save", return_value=self.root / "current.json"))
        self.seen_directory = self.root / "seen"
        self.seen_directory.mkdir()
        self.stack.enter_context(patch.object(cli, "_seen_directory", return_value=self.seen_directory))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))

    def recipe(self):
        return {
            "desktop": {"shell_companion": True}, "sessions": [], "terminals": [],
            "browsers": {"google_chrome": {"available": True, "profiles": [{
                "profile": "Default", "windows": [{"id": "saved", "workspace_index": 0, "monitor": 1,
                                                     "tabs": [{"url": "https://example.com/saved"}]}],
            }]}},
            "social_apps": {"slack": {"mode": "visible", "windows": [{"placement": "saved"}]}},
            "file_manager": {"windows": [{"path": "saved-folder"}]},
            "vscode": {"windows": [{"folders": [{"uri": "file:///saved-project"}]}]},
        }

    def empty_capture(self):
        result = self.recipe()
        result["browsers"]["google_chrome"]["profiles"][0]["windows"] = []
        result["social_apps"]["slack"] = {"mode": "stopped", "windows": []}
        result["file_manager"]["windows"] = []
        result["vscode"]["windows"] = []
        return result

    def mark_seen(self, snapshot):
        """The items were live earlier in this login, e.g. restored at startup."""
        from workspace_state import pending
        pending.record_seen(self.seen_directory, pending.live_keys(snapshot, pending.CATEGORIES))

    def invoke(self, previous, captured):
        with patch.object(cli, "load", return_value=previous), patch.object(cli, "_capture_all", return_value=captured):
            result = cli.cmd_save(argparse.Namespace(allow_partial=True))
        return result, self.save.call_args.args[0]

    def test_empty_capture_drops_items_seen_this_login(self):
        previous = self.recipe()
        before = copy.deepcopy(previous)
        self.mark_seen(previous)
        result, saved = self.invoke(previous, self.empty_capture())
        self.assertEqual(result, 0)
        for key in ("browsers", "social_apps", "file_manager", "vscode"):
            self.assertEqual(saved[key], self.empty_capture()[key])
        self.assertEqual(previous, before)
        self.assertNotIn("pending", saved)

    def test_unseen_items_stay_pending_after_empty_capture(self):
        previous = self.recipe()
        result, saved = self.invoke(previous, self.empty_capture())
        self.assertEqual(result, 0)
        self.assertEqual(saved["file_manager"], previous["file_manager"])
        self.assertEqual(saved["vscode"], previous["vscode"])
        self.assertEqual(sorted(entry["category"] for entry in saved["pending"].values()),
                         ["file-manager", "vscode"])
        self.assertEqual(saved["browsers"], self.empty_capture()["browsers"])

    def test_completion_marker_does_not_change_successful_empty_capture(self):
        self.mark_seen(self.recipe())
        for marker in ("failed: companion did not respond\n", "\n"):
            with self.subTest(marker=marker):
                (self.root / "vscode.done").write_text(marker)
                result, saved = self.invoke(self.recipe(), self.empty_capture())
                self.assertEqual(result, 0)
                self.assertEqual(saved["vscode"], self.empty_capture()["vscode"])

    def test_successful_restore_allows_intentional_closes(self):
        for category in ("browsers", "social-apps", "file-manager", "vscode"):
            write_stage_marker(self.root / f"{category}.done", StageMarker(category, "ready", operation_context=self.startup_context.to_dict()))
        captured = self.empty_capture()
        expected = copy.deepcopy(captured)
        # A successful restore records its items as seen in this login.
        self.mark_seen(self.recipe())
        result, saved = self.invoke(self.recipe(), captured)
        self.assertEqual(result, 0)
        for key in ("browsers", "social_apps", "file_manager", "vscode"):
            self.assertEqual(saved[key], expected[key])
        self.assertNotIn("pending", saved)
        self.assertFalse(saved.get("capture_errors", {}).get("preserved_categories"))

    def test_successful_capture_replaces_whole_recipe_after_failed_marker(self):
        previous = self.recipe()
        previous["vscode"]["windows"].append({"folders": [{"uri": "file:///second-project"}]})
        current = self.recipe()
        expected = copy.deepcopy(current["vscode"])
        self.mark_seen(previous)
        (self.root / "vscode.done").write_text("failed: second project unavailable\n")
        _, saved = self.invoke(previous, current)
        self.assertEqual(saved["vscode"], expected)

    def test_successful_browser_capture_keeps_complete_live_recipe(self):
        previous = self.recipe()
        current = self.recipe()
        current["created_at"] = "2026-01-02T10:00:00Z"
        live = current["browsers"]["google_chrome"]
        live["profiles"][0]["windows"][0]["tabs"].append({"url": "https://example.com/new"})
        expected_live = copy.deepcopy(live)
        _, saved = self.invoke(previous, current)
        self.assertEqual(saved["browsers"]["google_chrome"], expected_live)
        self.assertNotIn("latest_observation", previous["browsers"]["google_chrome"])

    def test_failed_or_empty_browser_capture_cannot_replace_latest_observation(self):
        previous = self.recipe()
        observation = {"schema_version": 1, "captured_at": "earlier", "browser_state": {"profiles": []}}
        previous["browsers"]["google_chrome"]["latest_observation"] = observation
        for failure in ("empty", "error", "unplaced"):
            current = self.empty_capture() if failure == "empty" else self.recipe()
            chrome = current["browsers"]["google_chrome"]
            if failure == "error":
                chrome["errors"] = ["profile failed"]
            elif failure == "unplaced":
                del chrome["profiles"][0]["windows"][0]["monitor"]
            with self.subTest(failure=failure):
                _, saved = self.invoke(previous, current)
                if failure == "empty":
                    self.assertNotIn("latest_observation", saved["browsers"]["google_chrome"])
                else:
                    self.assertEqual(saved["browsers"]["google_chrome"]["latest_observation"], observation)

    def test_newly_opened_apps_are_saved_when_prior_recipe_was_empty(self):
        result, saved = self.invoke(self.empty_capture(), self.recipe())
        self.assertEqual(result, 0)
        self.assertEqual(saved["vscode"], self.recipe()["vscode"])
        self.assertEqual(saved["browsers"], self.recipe()["browsers"])

    def test_explicit_ordinary_save_can_replace_prior_recipe(self):
        captured = self.empty_capture()
        expected = copy.deepcopy(captured)
        self.mark_seen(self.recipe())
        result, saved = self.invoke(self.recipe(), captured)
        self.assertEqual(result, 0)
        for key in ("browsers", "social_apps", "file_manager", "vscode"):
            self.assertEqual(saved[key], expected[key])
        self.assertNotIn("pending", saved)

    def test_partial_manual_adoption_leaves_failed_file_manager_protected(self):
        previous = self.recipe()
        partial = self.recipe()
        partial.pop("file_manager")
        partial["capture_errors"] = {"file_manager": ["bridge unavailable"]}
        owner = {"boot_id": "test-boot", "login_generation": "test-login"}
        with patch.object(cli, "_checkpoint_login", return_value=owner):
            _, adopted = self.invoke(previous, partial)
            self.assertIn("adoption", adopted["category_provenance"]["browsers"])
            self.assertNotIn("adoption", adopted["category_provenance"]["file-manager"])
            self.assertEqual(adopted["file_manager"], previous["file_manager"])
            _, saved = self.invoke(adopted, self.empty_capture())
        # The failed capture never saw the folder window live, so it stays
        # pending; the editor window seen by the first save closes.
        self.assertEqual(saved["file_manager"], previous["file_manager"])
        self.assertEqual([entry["category"] for entry in saved["pending"].values()], ["file-manager"])
        self.assertEqual(saved["browsers"], self.empty_capture()["browsers"])
        self.assertEqual(saved["social_apps"], self.empty_capture()["social_apps"])
        self.assertEqual(saved["vscode"], self.empty_capture()["vscode"])
        self.assertNotIn("retained_reason", saved["category_provenance"]["file-manager"])


if __name__ == "__main__":
    unittest.main()
