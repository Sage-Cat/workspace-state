from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from workspace_state import operations, tmux_server
from workspace_state.util import CommandError

ARGUMENTS = ["-d", "-s", "main", "-c", "/tmp"]


def created(command):
    """The arguments `new_session` ran: the caller's, then its creation nonce."""
    return command[:len(ARGUMENTS) + 2], command[len(ARGUMENTS) + 2:]


def completed(code=0, out="", err=""):
    return subprocess.CompletedProcess([], code, out, err)


REAL_MANAGER_ENVIRONMENT = tmux_server._manager_environment
MANAGER = {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "DISPLAY": ":0",
           "WSCTL_X": "1", "INVOCATION_ID": "abc"}


class TmuxServerScopeTests(unittest.TestCase):
    """Every subprocess is replaced; no scope, unit or tmux server is ever created."""

    def setUp(self):
        for target, value in (("workspace_state.tmux_server.operations.current", None),
                              ("workspace_state.tmux_server._manager_environment", dict(MANAGER))):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_new_session(self, *, running, scoped=None, systemd_run="/usr/bin/systemd-run",
                        direct=None, session_exists=False):
        """`running` is the sequence of server_running() answers."""
        scoped = scoped if scoped is not None else [completed()]
        direct = direct if direct is not None else [completed()]
        with patch.object(tmux_server, "server_running", side_effect=list(running)), patch(
            "workspace_state.tmux_server.shutil.which", return_value=systemd_run,
        ), patch(
            "workspace_state.tmux_server.subprocess.run", side_effect=list(scoped) + list(direct),
        ) as sub, patch(
            "workspace_state.tmux_server._session_exists", return_value=session_exists,
        ), patch("workspace_state.tmux_server.run", return_value="joined") as run, patch(
            "sys.stderr",
        ) as stderr:
            try:
                result = tmux_server.new_session(ARGUMENTS)
            finally:
                self.stderr = "".join(call.args[0] for call in stderr.write.call_args_list)
        return result, sub, run

    def test_first_session_starts_the_server_in_its_own_scope(self):
        _result, sub, run = self.run_new_session(running=[False])
        run.assert_not_called()
        command = sub.call_args.args[0]
        self.assertEqual(command[:5], ["/usr/bin/systemd-run", "--user", "--scope", "--collect", "--quiet"])
        unit = next(value for value in command if value.startswith("--unit="))
        self.assertRegex(unit, r"^--unit=wsctl-tmux-server-[0-9a-f]{8}$")
        self.assertIn("--slice=app.slice", command)
        head, tail = created(command[command.index("--") + 1:])
        self.assertEqual(head, ["tmux", "new-session", *ARGUMENTS])
        self.assertEqual(tail[:4], [";", "set-option", "-t", "=main:"])
        self.assertEqual(tail[4], "@wsctl-created")
        self.assertRegex(tail[5], r"^[0-9a-f]{32}$")

    def test_every_server_gets_a_distinct_unit(self):
        units = set()
        for _ in range(3):
            _result, sub, _run = self.run_new_session(running=[False])
            units.add(next(v for v in sub.call_args.args[0] if v.startswith("--unit=")))
        self.assertEqual(len(units), 3)

    def test_server_gets_the_manager_environment_without_operation_or_unit_variables(self):
        _result, sub, _run = self.run_new_session(running=[False])
        environment = sub.call_args.kwargs["env"]
        self.assertEqual(environment, {"PATH": "/usr/bin:/bin", "HOME": "/home/u", "DISPLAY": ":0",
                                       "COLORTERM": "truecolor"})

    def test_an_existing_colorterm_is_kept(self):
        with patch("workspace_state.tmux_server._manager_environment", return_value={"COLORTERM": "24bit"}):
            self.assertEqual(tmux_server.server_environment()["COLORTERM"], "24bit")

    def test_worker_environment_fallback_drops_what_the_launcher_added(self):
        worker = {"PATH": "/rel/bin:/home/u/.local/bin:/usr/bin", "PYTHONPATH": "/rel/src:/extra",
                  "WSCTL_BIN_DIR": "/rel/bin", "WSCTL_OPERATION_CONTEXT": "{}", "HOME": "/home/u",
                  "JOURNAL_STREAM": "1:2"}
        with patch("workspace_state.tmux_server._manager_environment", return_value=None), patch.dict(
            "os.environ", worker, clear=True,
        ), patch("sys.stderr") as stderr:
            environment = tmux_server.server_environment()
        self.assertEqual(environment, {"PATH": "/usr/bin", "PYTHONPATH": "/extra", "HOME": "/home/u",
                                       "COLORTERM": "truecolor"})
        self.assertIn("unavailable", "".join(call.args[0] for call in stderr.write.call_args_list))

    def test_manager_environment_is_read_from_the_json_property(self):
        output = '{"type":"as","data":["A=1","B=x=y","bad"]}'
        with patch("workspace_state.tmux_server.subprocess.run", return_value=completed(0, output)):
            self.assertEqual(REAL_MANAGER_ENVIRONMENT(), {"A": "1", "B": "x=y"})
        for outcome in (completed(1), completed(0, "not json")):
            with patch("workspace_state.tmux_server.subprocess.run", return_value=outcome):
                self.assertIsNone(REAL_MANAGER_ENVIRONMENT())
        with patch("workspace_state.tmux_server.subprocess.run", side_effect=OSError):
            self.assertIsNone(REAL_MANAGER_ENVIRONMENT())

    def test_existing_server_is_used_directly(self):
        result, sub, run = self.run_new_session(running=[True])
        sub.assert_not_called()
        self.assertEqual(created(run.call_args.args[0])[0], ["tmux", "new-session", *ARGUMENTS])

    def test_missing_systemd_run_starts_directly_with_a_cleaned_environment(self):
        _result, sub, run = self.run_new_session(running=[False], systemd_run=None)
        run.assert_not_called()
        self.assertEqual(created(sub.call_args.args[0])[0], ["tmux", "new-session", *ARGUMENTS])
        self.assertNotIn("INVOCATION_ID", sub.call_args.kwargs["env"])
        self.assertIn("systemd-run is unavailable", self.stderr)

    def test_scope_failure_joins_a_server_started_meanwhile(self):
        # A terminal opened at the same moment may have started the server first.
        result, sub, run = self.run_new_session(
            running=[False, True], scoped=[completed(1, "", "server exists")])
        self.assertEqual(created(run.call_args.args[0])[0], ["tmux", "new-session", *ARGUMENTS])
        self.assertEqual(sub.call_count, 1)
        self.assertIn("server exists", self.stderr)

    def run_after_failed_scope(self, option_value):
        """The scope timed out; a session named `main` exists with `option_value` as its nonce."""
        with patch.object(tmux_server, "server_running", side_effect=[False, True]), patch(
            "workspace_state.tmux_server.shutil.which", return_value="/usr/bin/systemd-run",
        ), patch(
            "workspace_state.tmux_server.subprocess.run", side_effect=[subprocess.TimeoutExpired("systemd-run", 15)],
        ) as sub, patch(
            "workspace_state.tmux_server._session_exists", return_value=True,
        ), patch("workspace_state.tmux_server.run") as run, patch("sys.stderr"):
            with patch("workspace_state.tmux_server._created_by",
                       side_effect=lambda _name, nonce: option_value(nonce) == nonce):
                return tmux_server.new_session(ARGUMENTS), run, sub

    def test_scope_timeout_after_the_session_was_created_is_a_success(self):
        # The session carries this call's nonce: the same invocation created it.
        result, run, _sub = self.run_after_failed_scope(lambda nonce: nonce)
        run.assert_not_called()
        self.assertEqual(result, "")

    def test_scope_timeout_with_a_session_the_user_created_is_never_adopted(self):
        for value in ("", "other-nonce"):
            with self.subTest(value=value), self.assertRaises(tmux_server.SessionNotCreated):
                self.run_after_failed_scope(lambda _nonce, value=value: value)

    def test_created_by_reads_the_nonce_option(self):
        with patch("workspace_state.tmux_server.subprocess.run", return_value=completed(0, "abc\n")) as sub:
            self.assertTrue(tmux_server._created_by("main", "abc"))
            self.assertFalse(tmux_server._created_by("main", "abd"))
        self.assertEqual(sub.call_args.args[0],
                         ["tmux", "show-options", "-qv", "-t", "=main:", "@wsctl-created"])
        with patch("workspace_state.tmux_server.subprocess.run", return_value=completed(1)):
            self.assertFalse(tmux_server._created_by("main", ""))

    def test_scope_failure_without_a_server_never_starts_one_in_the_caller_unit(self):
        for failure in (completed(1, "", "boom"), OSError("no exec"),
                        subprocess.TimeoutExpired("systemd-run", 15)):
            with self.subTest(failure=failure):
                with self.assertRaises(CommandError):
                    self.run_new_session(running=[False, False], scoped=[failure])

    def test_a_real_tmux_failure_on_the_joined_server_still_fails_the_session(self):
        with patch.object(tmux_server, "server_running", side_effect=[False, True]), patch(
            "workspace_state.tmux_server.shutil.which", return_value="/usr/bin/systemd-run",
        ), patch("workspace_state.tmux_server.subprocess.run", return_value=completed(1, "", "x")), patch(
            "workspace_state.tmux_server._session_exists", return_value=False,
        ), patch("workspace_state.tmux_server.run", side_effect=CommandError("duplicate")), patch("sys.stderr"):
            with self.assertRaises(CommandError):
                tmux_server.new_session(ARGUMENTS)

    def test_server_probe_treats_a_missing_server_as_not_running(self):
        with patch("workspace_state.tmux_server.subprocess.run", return_value=completed(1)):
            self.assertFalse(tmux_server.server_running())
        with patch("workspace_state.tmux_server.subprocess.run", return_value=completed(0)):
            self.assertTrue(tmux_server.server_running())
        with patch("workspace_state.tmux_server.subprocess.run", side_effect=OSError):
            self.assertFalse(tmux_server.server_running())

    def test_cancelled_startup_creates_no_server(self):
        context = operations.OperationContext(
            boot_id="b", login_generation="g", operation_id="o", mode="startup",
            attempt=1, deadline=1.0)
        with patch.object(tmux_server, "server_running", return_value=False), patch(
            "workspace_state.tmux_server.operations.current", return_value=context,
        ), patch("workspace_state.startup_control.check_abort", side_effect=RuntimeError("stopped")), patch(
            "workspace_state.tmux_server.subprocess.run",
        ) as scope:
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                tmux_server.new_session(ARGUMENTS)
        scope.assert_not_called()


if __name__ == "__main__":
    unittest.main()
