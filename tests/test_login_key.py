"""Two logins that share a session id (a reused bus name) stay separate by login key."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, native_status_evidence, operations, provider_progress
from login_stage import write_status

BOOT = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
SESSION_ID = hashlib.sha256(b":1.42").hexdigest()[:16]
FIRST, SECOND = "1" * 16, "2" * 16


class LoginKeyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory}))
        self.root = Path(directory) / "workspace-state"
        self.root.mkdir(mode=0o700)
        self.stack.enter_context(patch.object(cli, "_boot_id", return_value=BOOT))
        self.stack.enter_context(patch.object(operations, "boot_id", return_value=BOOT))
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        operations.bind(None)

    def login(self, key, session_id=SESSION_ID):
        (self.root / "login-generation").write_text(session_id + "\n")
        (self.root / "login-key").write_text(key + "\n")

    def context(self, session_id=SESSION_ID):
        return operations.OperationContext(BOOT, session_id, "o" * 32, "startup", 1, 10**9)

    def test_checkpoint_login_names_both_identifiers(self):
        self.login(FIRST)
        self.assertEqual(cli._checkpoint_login(), {
            "boot_id": BOOT, "login_generation": SESSION_ID, "login_key": FIRST})

    def test_directories_follow_the_login_key_not_the_session_id(self):
        self.login(FIRST)
        first = cli._startup_directory()
        self.assertEqual(first.name, f"startup-{BOOT}-{FIRST}")
        self.assertEqual(cli._seen_directory(cli._checkpoint_login()), first)
        self.login(SECOND)
        second = cli._startup_directory()
        self.assertNotEqual(second, first)
        self.assertEqual(cli._seen_directory(cli._checkpoint_login()), second)

    def test_a_key_older_than_the_session_id_is_not_this_logins(self):
        # A rolled-back release rewrites the session id and leaves the old key.
        self.login(FIRST)
        os.utime(self.root / "login-key", ns=(1, 1))
        self.assertEqual(cli._checkpoint_login()["login_key"], SESSION_ID)
        self.assertEqual(cli._startup_directory().name, f"startup-{BOOT}-{SESSION_ID}")
        os.utime(self.root / "login-key", ns=(4 * 10**18, 4 * 10**18))
        self.assertEqual(cli._checkpoint_login()["login_key"], FIRST)

    def test_an_old_release_login_keeps_its_directory_and_lock(self):
        # The coordinator keeps the session id as the key of such a login.
        directory = self.root / f"startup-{BOOT}-{SESSION_ID}"
        directory.mkdir()
        self.login(SESSION_ID)
        self.assertEqual(cli._startup_directory(), directory)
        self.assertEqual(cli._seen_directory(cli._checkpoint_login()), directory)

    def test_inherited_context_follows_the_session_id(self):
        self.login(FIRST)
        operations.bind(self.context())
        self.assertIsNotNone(cli._checkpoint_login())
        operations.bind(self.context("f" * 16))
        self.assertIsNone(cli._checkpoint_login())

    def test_final_save_keeps_chrome_capturable_under_the_inherited_context(self):
        self.login(FIRST)
        context = self.context()
        operations.bind(context)
        login = cli._checkpoint_login()
        self.assertFalse(cli._browsers_capturable(login))
        write_status(login, {"browsers": {"state": "ready"}}, context=context, login_key=FIRST)
        self.assertTrue(cli._browsers_capturable(login))
        # Another login with the same session id has not restored this Chrome.
        self.login(SECOND)
        self.assertFalse(cli._browsers_capturable(cli._checkpoint_login()))

    def test_manual_acceptance_is_scoped_to_the_login_key(self):
        self.login(FIRST)
        login = cli._checkpoint_login()
        cli._accept_browsers(login)
        self.assertTrue(cli._browsers_capturable(login))
        self.login(SECOND)
        self.assertFalse(cli._browsers_capturable(cli._checkpoint_login()))

    def test_native_status_scope_follows_the_login_key(self):
        self.stack.enter_context(patch.object(native_status_evidence, "runtime_root", return_value=self.root))
        self.login(FIRST)
        scope = native_status_evidence._scope()
        self.assertEqual(scope, {"boot_id": BOOT, "login_key": FIRST})
        self.login(SECOND)
        self.assertNotEqual(native_status_evidence._scope(), scope)
        self.login(FIRST, session_id="e" * 16)
        self.assertEqual(native_status_evidence._scope(), scope)

    def test_receipt_updates_use_the_login_key_directory_and_the_session_id_context(self):
        self.login(FIRST)
        context = self.context()
        directory = cli._startup_directory()
        receipt = directory / "browser-reconciliation.json"
        receipt.write_text(json.dumps({"operation_context": context.to_dict(), "state": "waiting-reuse-only"}))
        document = {"stages": [{"id": "browsers", "state": "ready", "message": "Restored", "current": 2}]}
        provider_progress._update_browser_receipt(document, context)
        self.assertEqual(json.loads(receipt.read_text())["state"], "verified-reuse-only")
        # A context of another session id never reaches this login's receipt.
        receipt.write_text(json.dumps({"operation_context": context.to_dict(), "state": "waiting-reuse-only"}))
        provider_progress._update_browser_receipt(
            {"stages": [{"id": "browsers", "state": "failed", "message": "x"}]}, self.context("f" * 16))
        self.assertEqual(json.loads(receipt.read_text())["state"], "waiting-reuse-only")


if __name__ == "__main__":
    unittest.main()
