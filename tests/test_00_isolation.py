"""Runs first: stops the whole run before any test can touch the live runtime directory."""
import unittest
from unittest.mock import patch

import _isolation

_isolation.require_isolated_runtime()


class IsolationGuardTests(unittest.TestCase):
    def test_a_real_runtime_directory_is_refused_with_a_clear_message(self):
        for value in ("/run/user/1000", "/run/user/4242/", None):
            with self.subTest(value=value):
                environment = {} if value is None else {"XDG_RUNTIME_DIR": value}
                self.assertIn("scripts/run-tests.py", _isolation.isolation_message(environment))

    def test_a_private_directory_is_accepted(self):
        self.assertIsNone(_isolation.isolation_message({"XDG_RUNTIME_DIR": "/tmp/wsctl-unit-tests-x/runtime"}))

    def test_stand_in_objects_never_name_runtime_directories(self):
        from workspace_state import startup, util
        from unittest.mock import MagicMock
        for boot, generation in ((MagicMock(), "a"), ("a", MagicMock()), ("a/b", "c"), ("", "c")):
            with self.subTest(boot=boot, generation=generation):
                with self.assertRaises(TypeError):
                    startup.startup_directory(util.Path("/nonexistent-runtime"), boot, generation)
        with self.assertRaises(TypeError):
            startup.startup_directory(MagicMock(), "a", "b")
        with patch.dict("os.environ", {"XDG_RUNTIME_DIR": "/tmp/x"}):
            self.assertEqual(str(startup.runtime_identity()[0]), "/tmp/x/workspace-state")
