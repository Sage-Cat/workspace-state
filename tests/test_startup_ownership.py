from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import startup
from workspace_state.startup import startup_directory


class StartupOwnershipTests(unittest.TestCase):
    def test_each_login_key_has_its_own_directory_and_nothing_is_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = startup_directory(root, "boot", None)
            self.assertEqual(legacy.name, "startup-boot")
            (legacy / "terminals.done").write_text("an older release's attempt")
            (legacy / "generation-owner.json").write_text(json.dumps(
                {"schema_version": 1, "boot_id": "boot", "login_key": "a" * 16}))
            first = startup_directory(root, "boot", "a" * 16)
            self.assertEqual(first.name, "startup-boot-" + "a" * 16)
            self.assertNotEqual(first, legacy)
            self.assertEqual(list(first.iterdir()), [])
            second = startup_directory(root, "boot", "b" * 16)
            self.assertNotIn(second, (legacy, first))
            self.assertEqual(startup_directory(root, "boot", "b" * 16), second)
            # The old files stay where they are, ignored.
            self.assertTrue((legacy / "terminals.done").exists())

    def test_a_login_without_a_key_keeps_the_boot_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(startup_directory(root, "boot", None), root / "startup-boot")
            self.assertEqual(oct((root / "startup-boot").stat().st_mode & 0o777), "0o700")

    def test_login_key_files_are_read_with_the_session_id_as_the_fallback_key(self):
        from workspace_state.startup import login_identity
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(login_identity(root), (None, None))
            (root / "login-generation").write_text("a" * 16 + "\n")
            self.assertEqual(login_identity(root), ("a" * 16, "a" * 16))
            (root / "login-key").write_text("b" * 16 + "\n")
            self.assertEqual(login_identity(root), ("a" * 16, "b" * 16))
            (root / "login-key").write_text("not hex\n")
            self.assertEqual(login_identity(root), ("a" * 16, "a" * 16))


class UnitLivenessTests(unittest.TestCase):
    def answer(self, stdout="", returncode=0, error=None):
        outcome = {"side_effect": error} if error else {
            "return_value": subprocess.CompletedProcess([], returncode, stdout, "")}
        return patch.object(startup.subprocess, "run", **outcome)

    def test_only_a_clearly_live_state_counts(self):
        for state in ("active", "activating", "deactivating", "reloading", "refreshing"):
            with self.subTest(state=state), self.answer(state + "\n"):
                self.assertTrue(startup.unit_is_live("unit.service"))
        for state in ("inactive", "failed", "", "unknown-state"):
            with self.subTest(state=state), self.answer(state + "\n"):
                self.assertFalse(startup.unit_is_live("unit.service"))

    def test_an_unclear_answer_is_not_live(self):
        with self.answer("active\n", returncode=1):
            self.assertFalse(startup.unit_is_live("unit.service"))
        for error in (subprocess.TimeoutExpired("systemctl", 2), OSError("no systemctl")):
            with self.subTest(error=error), self.answer(error=error):
                self.assertFalse(startup.unit_is_live("unit.service"))


if __name__ == '__main__':
    unittest.main()
