"""Runs first: stops the whole run before any test can touch the live runtime directory."""
import os
import re
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
        from workspace_state import login_status, util
        with patch.dict("os.environ", {"XDG_STATE_HOME": "", "XDG_DATA_HOME": ""}):
            self.assertEqual(login_status.state_root(), util.Path.home() / ".local/state/workspace-state")
            self.assertEqual(util.data_home(), util.Path.home() / ".local/share/workspace-state")

    def test_a_missing_runtime_directory_never_falls_back_to_the_live_one(self):
        from workspace_state import alerts, browser, file_manager, login_status, startup, util, vscode
        for value in ({}, {"XDG_RUNTIME_DIR": ""}):
            with patch.dict("os.environ", value, clear=True):
                for function in (util.runtime_home, browser.runtime_dir, login_status.runtime_root,
                                 alerts.snapshot_path, file_manager._marker_directory,
                                 startup.runtime_identity, vscode.runtime_root):
                    with self.subTest(value=value, function=function.__name__):
                        with self.assertRaises(RuntimeError):
                            function()

    def test_the_home_directory_of_a_run_is_private(self):
        home = _isolation.Path.home().resolve()
        self.assertEqual(str(home), str(_isolation.Path(os.environ["HOME"]).resolve()))
        self.assertTrue(str(home).startswith(str(_isolation.Path(os.environ["XDG_RUNTIME_DIR"]).resolve().parent)))

    def test_every_module_that_starts_or_binds_an_operation_context_unbinds_it(self):
        # A context left bound makes later tests pass or fail by order. Startup code
        # binds it too, so tests that run it need a module or per-test cleanup.
        directory = _isolation.Path(__file__).parent
        binds = re.compile(r"operations\.bind\((?!None)|login_status\.initialize\(|\bcmd_startup\(")
        for path in sorted(directory.glob("test_*.py")):
            text = path.read_text()
            if path.name != _isolation.Path(__file__).name and binds.search(text):
                with self.subTest(path.name):
                    self.assertRegex(text, r"addCleanup\(\s*(lambda: )?operations\.bind|"
                                           r"def tearDownModule\(\):(?:\n(?:    .*)?)*?\n    (?:operations|_operations)\.bind\(None\)",
                                     "binds a context without a cleanup that unbinds it")
