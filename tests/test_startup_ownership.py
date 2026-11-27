from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from workspace_state.startup import startup_directory


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

    def test_adoption_follows_the_login_key(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = startup_directory(root, "boot", None)
            self.assertEqual(startup_directory(root, "boot", "b" * 16), legacy)
            owner = json.loads((legacy / "generation-owner.json").read_text())
            self.assertEqual(owner, {"schema_version": 1, "boot_id": "boot", "login_key": "b" * 16})
            # An owner written before the key existed is never adopted by a key.
            (legacy / "generation-owner.json").write_text(json.dumps(
                {"schema_version": 1, "boot_id": "boot", "login_generation": "b" * 16}))
            self.assertNotEqual(startup_directory(root, "boot", "b" * 16), legacy)


if __name__ == '__main__':
    unittest.main()
