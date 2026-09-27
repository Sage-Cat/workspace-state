from __future__ import annotations

import argparse
import copy
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli


class SaveMonitorIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(cli, "state_lock"))
        self.stack.enter_context(patch.object(cli, "_shutdown_allows_unresolved_codex", return_value=True))
        self.stack.enter_context(patch.object(cli, "update_stage"))
        self.stack.enter_context(patch.object(cli, "_arm_autosave"))
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.save = self.stack.enter_context(patch.object(cli, "save", return_value=Path("/unused/current.json")))
        self.previous = self.snapshot([{
            "connector": "HDMI-2", "identity": {"vendor": "GSM", "product": "LG HDR 4K", "serial": "123"},
        }])

    def snapshot(self, monitors):
        return {
            "desktop": {"shell_companion": True, "monitors": monitors},
            "sessions": [], "terminals": [],
            "browsers": {"google_chrome": {"available": True, "profiles": []}},
        }

    def full_save(self, captured, *, shutdown=False):
        with patch.object(cli, "load", return_value=self.previous), patch.object(cli, "_capture_all", return_value=captured):
            return cli.cmd_save(argparse.Namespace(allow_partial=True, shutdown_safe=shutdown))

    def test_fallback_never_overwrites_physical_checkpoint_even_with_partial_permission(self):
        for shutdown in (False, True):
            with self.subTest(shutdown=shutdown):
                before = copy.deepcopy(self.previous)
                with self.assertRaisesRegex(RuntimeError, "existing checkpoint was preserved"):
                    self.full_save(self.snapshot([{"connector": "None-1"}]), shutdown=shutdown)
                self.assertEqual(self.previous, before)
        self.save.assert_not_called()

    def test_explicitly_unknown_identity_is_rejected(self):
        cases = [
            {"connector": "unknown"},
            {"identity": {"connector": "None-1"}},
            {"connector": "DP-1", "identity": {"vendor": "unknown", "product": "unknown", "serial": "unknown"}},
        ]
        for monitor in cases:
            with self.subTest(monitor=monitor), self.assertRaisesRegex(RuntimeError, "unknown display identity"):
                self.full_save(self.snapshot([monitor]))
        self.save.assert_not_called()

    def test_tmux_autosave_rejects_fallback_even_during_shutdown(self):
        captured = self.snapshot([{"connector": "None-1"}])
        for allow_unresolved in (False, True):
            with self.subTest(allow_unresolved=allow_unresolved):
                with patch.object(cli, "load", return_value=self.previous), patch.object(cli, "capture", return_value=captured):
                    path, problems = cli._autosave_from_tmux(allow_unresolved_codex=allow_unresolved)
                self.assertIsNone(path)
                self.assertEqual(len(problems), 1)
                self.assertIn("saved physical monitor layout", problems[0])
        self.save.assert_not_called()

    def test_real_monitor_changes_and_partial_identity_metadata_are_allowed(self):
        for monitor in (
            {"connector": "eDP-1"},
            {"connector": "HDMI-1", "identity": {"vendor": "GSM", "serial": "unknown"}},
        ):
            with self.subTest(monitor=monitor):
                captured = self.snapshot([monitor])
                self.assertEqual(self.full_save(captured), 0)
                self.assertEqual(self.save.call_args.args[0], captured)

    def test_absent_legacy_monitor_data_is_not_evidence_of_fallback(self):
        captured = self.snapshot([])
        del captured["desktop"]["monitors"]
        self.assertEqual(self.full_save(captured), 0)
        self.previous["desktop"].pop("monitors")
        self.assertEqual(self.full_save(self.snapshot([{"connector": "None-1"}])), 0)

    def test_no_physical_checkpoint_allows_first_save_of_fallback(self):
        self.previous = self.snapshot([{"connector": "None-1"}])
        self.assertEqual(self.full_save(self.snapshot([{"connector": "None-1"}])), 0)


if __name__ == "__main__":
    unittest.main()
