from __future__ import annotations

import argparse
import copy
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class ShutdownRecipeIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.object(cli, "_startup_marker", side_effect=lambda category: self.root / f"{category}.done"))
        self.stack.enter_context(patch.object(cli, "state_lock"))
        self.stack.enter_context(patch.object(cli, "_shutdown_allows_unresolved_codex", return_value=True))
        self.stack.enter_context(patch.object(cli, "_arm_autosave"))
        from workspace_state import operations
        self.startup_context = operations.OperationContext.create("recipe-test", "startup")
        shutdown_context = operations.OperationContext.create("recipe-test", "shutdown")
        self.stack.enter_context(patch.object(cli, "_marker_context", return_value=shutdown_context))
        self.stages = self.stack.enter_context(patch.object(cli, "update_stage"))
        self.save = self.stack.enter_context(patch.object(cli, "save", return_value=self.root / "current.json"))
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

    def invoke(self, previous, captured, *, shutdown=True):
        with patch.object(cli, "load", return_value=previous), patch.object(cli, "_capture_all", return_value=captured):
            result = cli.cmd_save(argparse.Namespace(allow_partial=True, shutdown_safe=shutdown))
        return result, self.save.call_args.args[0]

    def test_unlaunched_apps_are_retained_when_shutdown_replaces_startup_hud(self):
        previous = self.recipe()
        before = copy.deepcopy(previous)
        result, saved = self.invoke(previous, self.empty_capture())
        self.assertEqual(result, 3)
        for key in ("browsers", "social_apps", "file_manager", "vscode"):
            self.assertEqual(saved[key], previous[key])
        self.assertEqual(previous, before)
        self.assertEqual(len(saved["capture_errors"]["preserved_categories"]), 4)
        for stage in ("social-apps-save", "file-manager-save", "vscode-save"):
            last = [call for call in self.stages.call_args_list if call.args[0] == stage][-1]
            self.assertEqual(last.args[1], "degraded")
            self.assertIn("restoration did not complete", last.args[2])

    def test_failed_or_empty_completion_marker_does_not_authorize_recipe_loss(self):
        for marker in ("failed: companion did not respond\n", "\n"):
            with self.subTest(marker=marker):
                (self.root / "vscode.done").write_text(marker)
                result, saved = self.invoke(self.recipe(), self.empty_capture())
                self.assertEqual(result, 3)
                self.assertEqual(saved["vscode"], self.recipe()["vscode"])

    def test_successful_restore_allows_intentional_closes(self):
        for category in ("browsers", "social-apps", "file-manager", "vscode"):
            write_stage_marker(self.root / f"{category}.done", StageMarker(category, "ready", operation_context=self.startup_context.to_dict()))
        captured = self.empty_capture()
        result, saved = self.invoke(self.recipe(), captured)
        self.assertEqual(result, 0)
        self.assertEqual(saved, captured)
        self.assertFalse(saved.get("capture_errors", {}).get("preserved_categories"))

    def test_failure_preserves_whole_recipe_instead_of_partial_live_windows(self):
        previous = self.recipe()
        previous["vscode"]["windows"].append({"folders": [{"uri": "file:///second-project"}]})
        current = self.recipe()
        (self.root / "vscode.done").write_text("failed: second project unavailable\n")
        _, saved = self.invoke(previous, current)
        self.assertEqual(saved["vscode"], previous["vscode"])

    def test_newly_opened_apps_are_saved_when_prior_recipe_was_empty(self):
        result, saved = self.invoke(self.empty_capture(), self.recipe())
        self.assertEqual(result, 0)
        self.assertEqual(saved["vscode"], self.recipe()["vscode"])
        self.assertEqual(saved["browsers"], self.recipe()["browsers"])

    def test_explicit_ordinary_save_can_replace_prior_recipe(self):
        captured = self.empty_capture()
        result, saved = self.invoke(self.recipe(), captured, shutdown=False)
        self.assertEqual(result, 0)
        self.assertEqual(saved, captured)

    def test_preservation_does_not_override_unsafe_unsaved_editor_failure(self):
        captured = self.empty_capture()
        captured["capture_errors"] = {"vscode": ["Hot Exit disabled"], "vscode_unsafe": True}
        with self.assertRaisesRegex(RuntimeError, "unsaved edits"):
            self.invoke(self.recipe(), captured)
        self.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
