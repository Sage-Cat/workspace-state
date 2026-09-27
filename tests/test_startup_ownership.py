from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workspace_state.startup import startup_directory, startup_suspended


class StartupOwnershipTests(unittest.TestCase):
    def test_legacy_claim_is_adopted_once_and_never_shared_with_the_next_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = startup_directory(root, "boot", None)
            (legacy / "terminals.done").write_text("first login")
            self.assertEqual(startup_directory(root, "boot", "a" * 16), legacy)
            self.assertEqual(startup_directory(root, "boot", "a" * 16), legacy)
            second = startup_directory(root, "boot", "b" * 16)
            self.assertNotEqual(second, legacy)
            self.assertFalse((second / "terminals.done").exists())
            self.assertEqual(startup_directory(root, "boot", "b" * 16), second)

    def test_unknown_or_corrupt_legacy_owner_does_not_authorize_adoption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = startup_directory(root, "boot", None)
            (legacy / "generation-owner.json").write_text("bad JSON")
            self.assertNotEqual(startup_directory(root, "boot", "a" * 16), legacy)
            with self.assertRaisesRegex(RuntimeError, "refusing to reuse"):
                startup_directory(root, "boot", None)

    def test_suspension_is_exactly_scoped_to_boot_and_login_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "startup-suspended.json").write_text(json.dumps({
                "schema_version": 1, "boot_id": "boot", "login_generation": "a" * 16,
                "operation_id": "b" * 32,
            }))
            self.assertTrue(startup_suspended(root, "boot", "a" * 16))
            self.assertFalse(startup_suspended(root, "next-boot", "a" * 16))
            self.assertFalse(startup_suspended(root, "boot", "c" * 16))
            self.assertFalse(startup_suspended(root, "boot", None))


if __name__ == '__main__':
    unittest.main()
