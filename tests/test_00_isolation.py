"""Runs first: stops the whole run before any test can touch the live runtime directory."""
import unittest
from unittest.mock import patch

import _isolation

_isolation.require_isolated_runtime()


class IsolationGuardTests(unittest.TestCase):
    def test_a_real_runtime_directory_is_refused_with_a_clear_message(self):
        for value in ("/run/user/1000", "/run/user/4242/", None):
            with self.subTest(value=value):
                environment = {} if value is None else {"XDG_RUNTIME_DIR": value, "XDG_STATE_HOME": "/tmp/x",
                                                        "XDG_DATA_HOME": "/tmp/x"}
                self.assertIn("scripts/run-tests.py", _isolation.isolation_message(environment))

    def test_a_private_directory_is_accepted(self):
        private = {"XDG_RUNTIME_DIR": "/tmp/wsctl-unit-tests-x/runtime", "XDG_STATE_HOME": "/tmp/wsctl-unit-tests-x/state",
                   "XDG_DATA_HOME": "/tmp/wsctl-unit-tests-x/data"}
        self.assertIsNone(_isolation.isolation_message(private))

    def test_the_live_state_and_data_directories_are_refused_too(self):
        private = {"XDG_RUNTIME_DIR": "/tmp/wsctl-unit-tests-x/runtime", "XDG_STATE_HOME": "/tmp/wsctl-unit-tests-x/state",
                   "XDG_DATA_HOME": "/tmp/wsctl-unit-tests-x/data"}
        home = str(_isolation.Path.home())
        for name, value in (("XDG_STATE_HOME", home + "/.local/state"), ("XDG_DATA_HOME", home + "/.local/share/x"),
                            ("XDG_DATA_HOME", ""), ("XDG_STATE_HOME", None)):
            environment = {**private, name: value}
            if value is None:
                del environment[name]
            with self.subTest(name=name, value=value):
                self.assertIn("scripts/run-tests.py", _isolation.isolation_message(environment))

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

    def test_an_empty_xdg_variable_means_unset(self):
        from workspace_state import browser, login_status, util
        with patch.dict("os.environ", {"XDG_RUNTIME_DIR": "", "XDG_STATE_HOME": "", "XDG_DATA_HOME": ""}):
            self.assertEqual(str(browser.runtime_dir()), f"/run/user/{util.os.getuid()}/workspace-state")
            self.assertEqual(login_status.state_root(), util.Path.home() / ".local/state/workspace-state")
            self.assertEqual(util.data_home(), util.Path.home() / ".local/share/workspace-state")
