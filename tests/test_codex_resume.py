from __future__ import annotations

from contextlib import ExitStack, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from workspace_state import codex_resume


SESSION_ID = "12345678-abcd-4abc-8def-1234567890AB"
LOCK_FAILURE = "ERROR: failed to initialize sqlite local db: failed to open log DB: database is locked"


class CodexResumeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.lock_path = Path(directory) / "startup.lock"
        self.stack.enter_context(patch.object(codex_resume, "runtime_root", return_value=Path(directory)))
        self.cwd = self.stack.enter_context(patch.object(codex_resume, "saved_cwd", return_value=None))
        self.stack.enter_context(patch.object(codex_resume, "startup_lock_path", return_value=self.lock_path))
        self.stack.enter_context(patch.dict(os.environ, {"TMUX_PANE": "%987"}))
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        self.stack.enter_context(redirect_stderr(self.errors))
        self.signal = self.stack.enter_context(patch.object(codex_resume.signal, "signal"))
        self.sleep = self.stack.enter_context(patch.object(codex_resume.time, "sleep"))
        self.launch = self.stack.enter_context(patch.object(codex_resume.subprocess, "Popen"))
        self.capture = self.stack.enter_context(patch.object(codex_resume, "pane_text", return_value=""))
        self.ready = self.stack.enter_context(patch.object(codex_resume, "resumed_session", return_value=True))

    def child(self, returncode=None):
        child = Mock(pid=456, returncode=returncode)
        child.poll.return_value = returncode
        child.wait.return_value = 0
        return child

    def assert_gate(self, *, locked):
        descriptor = os.open(self.lock_path, os.O_RDWR)
        try:
            if locked:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def current_failure(self, _pane, *, history):
        self.assertTrue(history)
        return self.output.getvalue() + LOCK_FAILURE

    def test_gate_covers_initialization_but_releases_before_conversation_exit(self):
        child = self.child()
        self.launch.return_value = child
        probes = []

        def ready(pid, session_id, pane):
            self.assertEqual((pid, session_id, pane), (456, SESSION_ID, "%987"))
            self.assert_gate(locked=True)
            probes.append(pid)
            return len(probes) == 2

        def conversation():
            self.assert_gate(locked=False)
            receipt = json.loads((self.lock_path.parent / "codex-starts" / f"{SESSION_ID}.json").read_text())
            self.assertEqual(receipt["state"], "ready")
            self.assertEqual(self.errors.getvalue(), "")
            return 0

        self.ready.side_effect = ready
        child.wait.side_effect = conversation
        self.assertEqual(codex_resume.resume(SESSION_ID), 0)
        self.assertEqual(len(probes), 2)
        child.wait.assert_called_once_with()
        child.terminate.assert_not_called()

    def test_exact_uuid_terminal_streams_and_runtime_permissions_are_inherited(self):
        self.launch.return_value = self.child(0)
        self.assertEqual(codex_resume.resume(SESSION_ID), 0)
        self.launch.assert_called_once_with([
            "codex", "resume", "--no-alt-screen", SESSION_ID,
        ])
        # No Popen keyword redirects streams, changes the process group or env;
        # no argv flags override global config or force embedded server mode.
        self.assertEqual(self.launch.call_args.kwargs, {})
        self.assertEqual(self.lock_path.stat().st_mode & 0o777, 0o600)

    def test_invalid_session_identifiers_never_launch_or_create_gate(self):
        for invalid in ("", "latest", "--last", SESSION_ID + ";echo unsafe", "1234", SESSION_ID + "\n"):
            with self.subTest(session=invalid):
                self.assertEqual(codex_resume.resume(invalid), 2)
        self.launch.assert_not_called()
        self.assertFalse(self.lock_path.exists())

    def test_queue_timeout_does_not_launch_and_closes_descriptor(self):
        with patch.object(codex_resume, "acquire_startup_lock", return_value=False) as acquire:
            self.assertEqual(codex_resume.resume(SESSION_ID), 1)
        self.launch.assert_not_called()
        with self.assertRaises(OSError):
            os.fstat(acquire.call_args.args[0])

    def test_unavailable_saved_directory_waits_before_taking_startup_gate(self):
        self.cwd.return_value = Path("/fake-cloud/project")
        self.launch.return_value = self.child(0)

        def waiting(_seconds):
            self.assertFalse(self.lock_path.exists())
            self.launch.assert_not_called()
            self.assertEqual(codex_resume.waiting_directory_ids(), {SESSION_ID})

        self.sleep.side_effect = waiting
        with patch.object(codex_resume, "directory_ready", side_effect=[False, False, True]):
            self.assertEqual(codex_resume.resume(SESSION_ID), 0)
        self.sleep.assert_called_once_with(1)
        self.launch.assert_called_once()
        self.assertEqual(codex_resume.pending_start_ids(), set())

    def test_missing_directory_timeout_never_launches_or_takes_gate(self):
        self.cwd.return_value = Path("/fake-cloud/project")
        with patch.object(codex_resume, "directory_ready", return_value=False), patch.object(
            codex_resume.time, "monotonic", side_effect=[100, 100 + codex_resume.DIRECTORY_TIMEOUT],
        ):
            self.assertEqual(codex_resume.resume(SESSION_ID), 1)
        self.launch.assert_not_called()
        self.assertFalse(self.lock_path.exists())
        self.assertEqual(codex_resume.pending_start_ids(), set())

    def test_missing_executable_releases_gate(self):
        self.launch.side_effect = FileNotFoundError("codex")
        self.assertEqual(codex_resume.resume(SESSION_ID), 127)
        self.assert_gate(locked=False)

    def test_current_initialization_lock_retries_are_bounded(self):
        self.launch.side_effect = lambda *args, **kwargs: self.child(1)
        self.capture.side_effect = self.current_failure
        self.assertEqual(codex_resume.resume(SESSION_ID), 1)
        self.assertEqual(self.launch.call_count, codex_resume.MAX_ATTEMPTS)
        markers = [line.split("[")[-1].rstrip("]") for line in self.output.getvalue().splitlines()
                   if "attempt " in line]
        self.assertEqual(len(set(markers)), codex_resume.MAX_ATTEMPTS)
        self.assertEqual(self.sleep.call_count, codex_resume.MAX_ATTEMPTS - 1)
        self.assert_gate(locked=False)

    def test_transient_lock_can_recover_without_releasing_gate_between_attempts(self):
        children = iter((self.child(1), self.child(0)))

        def launch(*args, **kwargs):
            self.assert_gate(locked=True)
            return next(children)

        self.launch.side_effect = launch
        self.capture.side_effect = self.current_failure
        self.assertEqual(codex_resume.resume(SESSION_ID), 0)
        self.assertEqual(self.launch.call_count, 2)

    def test_old_scrollback_unrelated_error_and_success_do_not_retry(self):
        cases = (
            (1, lambda: LOCK_FAILURE + "\n" + self.output.getvalue()),
            (1, lambda: self.output.getvalue() + "API request failed: database is locked"),
            (1, lambda: self.output.getvalue() + "failed to initialize: permission denied"),
            (1, lambda: self.output.getvalue() + "ERROR: failed to initialize: permission denied\nAPI: database is locked"),
            (1, lambda: LOCK_FAILURE),
            (0, lambda: self.output.getvalue() + LOCK_FAILURE),
        )
        for code, output in cases:
            with self.subTest(code=code, output=output):
                self.launch.reset_mock()
                self.launch.return_value = self.child(code)
                self.capture.side_effect = lambda *args, **kwargs: output()
                self.assertEqual(codex_resume.resume(SESSION_ID), code)
                self.launch.assert_called_once()

    def test_user_exit_is_never_retried_even_after_initialization_lock_output(self):
        for code in (-signal.SIGINT, -signal.SIGTERM, 130, 143):
            with self.subTest(code=code):
                self.launch.reset_mock()
                self.launch.return_value = self.child(code)
                self.capture.side_effect = self.current_failure
                expected = 128 - code if code < 0 else code
                self.assertEqual(codex_resume.resume(SESSION_ID), expected)
                self.launch.assert_called_once()

    def test_failure_after_ready_is_not_retried(self):
        child = self.child()
        child.wait.return_value = 1
        self.launch.return_value = child
        self.capture.side_effect = self.current_failure
        self.assertEqual(codex_resume.resume(SESSION_ID), 1)
        self.launch.assert_called_once()
        self.capture.assert_not_called()

    def test_exit_immediately_after_ownership_is_observed_is_not_retried(self):
        child = self.child(1)
        child.poll.side_effect = [None, 1]
        self.launch.return_value = child
        self.capture.side_effect = self.current_failure
        self.assertEqual(codex_resume.resume(SESSION_ID), 1)
        self.ready.assert_called_once()
        self.launch.assert_called_once()
        self.capture.assert_not_called()

    def test_sigterm_during_retry_backoff_does_not_start_another_child(self):
        self.launch.return_value = self.child(1)
        self.capture.side_effect = self.current_failure

        def backoff(_seconds):
            handler = next(call.args[1] for call in reversed(self.signal.call_args_list)
                           if call.args[0] == signal.SIGTERM and callable(call.args[1]))
            handler(signal.SIGTERM, None)

        self.sleep.side_effect = backoff
        self.assertEqual(codex_resume.resume(SESSION_ID), 143)
        self.launch.assert_called_once()
        self.sleep.assert_called_once_with(1)

    def test_sigterm_inside_launch_is_forwarded_after_child_assignment(self):
        child = self.child()

        def launch(*args, **kwargs):
            handler = next(call.args[1] for call in reversed(self.signal.call_args_list)
                           if call.args[0] == signal.SIGTERM and callable(call.args[1]))
            handler(signal.SIGTERM, None)
            return child

        def terminate():
            child.poll.return_value = 0
            child.returncode = 0

        child.terminate.side_effect = terminate
        self.launch.side_effect = launch
        self.assertEqual(codex_resume.resume(SESSION_ID), 143)
        child.terminate.assert_called_once_with()
        self.ready.assert_not_called()
        self.launch.assert_called_once()

    def test_sigint_is_restored_before_retry_backoff(self):
        self.launch.return_value = self.child(1)
        self.capture.side_effect = self.current_failure
        original = signal.getsignal(signal.SIGINT)

        def backoff(_seconds):
            handler = next(call.args[1] for call in reversed(self.signal.call_args_list)
                           if call.args[0] == signal.SIGINT)
            self.assertIs(handler, original)
            raise KeyboardInterrupt()

        self.sleep.side_effect = backoff
        with self.assertRaises(KeyboardInterrupt):
            codex_resume.resume(SESSION_ID)
        self.launch.assert_called_once()
        self.assert_gate(locked=False)

    def test_exit_record_failure_still_closes_gate_and_restores_handlers(self):
        self.launch.return_value = self.child(0)

        def record(_session_id, state):
            if state == "exited":
                raise OSError("runtime state unavailable")

        with patch.object(codex_resume, "_record", side_effect=record), patch.object(
            codex_resume, "acquire_startup_lock", wraps=codex_resume.acquire_startup_lock,
        ) as acquire:
            with self.assertRaisesRegex(OSError, "runtime state unavailable"):
                codex_resume.resume(SESSION_ID)
        with self.assertRaises(OSError):
            os.fstat(acquire.call_args.args[0])
        self.assert_gate(locked=False)
        self.assertEqual(self.signal.call_args_list[-2].args, (signal.SIGINT, signal.getsignal(signal.SIGINT)))
        self.assertEqual(self.signal.call_args_list[-1].args, (signal.SIGTERM, signal.getsignal(signal.SIGTERM)))

    def test_unverified_live_process_is_preserved_and_gate_released_at_deadline(self):
        child = self.child()
        self.launch.return_value = child
        self.ready.return_value = False

        def conversation():
            self.assert_gate(locked=False)
            receipt = json.loads((self.lock_path.parent / "codex-starts" / f"{SESSION_ID}.json").read_text())
            self.assertEqual(receipt["state"], "unverified")
            self.assertEqual(self.errors.getvalue(), "")
            return 0

        child.wait.side_effect = conversation
        with patch.object(codex_resume, "START_TIMEOUT", 0):
            self.assertEqual(codex_resume.resume(SESSION_ID), 0)
        child.terminate.assert_not_called()
        child.kill.assert_not_called()
        self.launch.assert_called_once()
        self.assertEqual(self.errors.getvalue(), "")


class ResumeEvidenceTests(unittest.TestCase):
    def test_pending_records_require_live_matching_process_stamp_and_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            codex_resume, "runtime_root", return_value=Path(directory),
        ), patch.object(codex_resume, "_process_stamp", side_effect=lambda pid: {1: "live", 2: "reused"}.get(pid)):
            records = Path(directory) / "codex-starts"
            records.mkdir()
            entries = (
                ("directory", 1, "live", "waiting-directory"),
                ("queued", 1, "live", "queued"),
                ("starting", 1, "live", "starting"),
                ("ready", 1, "live", "ready"),
                ("exited", 1, "live", "exited"),
                ("reused", 2, "old", "waiting-directory"),
                ("dead", 3, "old", "waiting-directory"),
            )
            for name, pid, stamp, state in entries:
                (records / f"{name}.json").write_text(json.dumps({
                    "session_id": name, "pid": pid, "start_ticks": stamp, "state": state,
                }))
            (records / "broken.json").write_text("broken")
            self.assertEqual(codex_resume.waiting_directory_ids(), {"directory"})
            self.assertEqual(codex_resume.pending_start_ids(), {"directory", "queued", "starting"})

    def test_owned_rollout_must_match_exact_requested_session(self):
        with patch.object(codex_resume, "_open_rollout_sessions", return_value={SESSION_ID}) as owned, patch.object(
            codex_resume, "pane_text", return_value="",
        ), patch.object(codex_resume, "loaded_thread_ids") as loaded:
            self.assertTrue(codex_resume.resumed_session(456, SESSION_ID, "%987"))
            self.assertFalse(codex_resume.resumed_session(456, "other-session", "%987"))
        owned.assert_called_with(456)
        loaded.assert_not_called()

    def test_contradictory_owned_rollout_cannot_be_overridden_by_daemon_or_prompt(self):
        with patch.object(codex_resume, "_open_rollout_sessions", return_value={"other-session"}), patch.object(
            codex_resume, "pane_text", return_value="› Continue\n100% context left",
        ), patch.object(codex_resume, "loaded_thread_ids", return_value={SESSION_ID}) as loaded:
            self.assertFalse(codex_resume.resumed_session(456, SESSION_ID, "%987"))
        loaded.assert_not_called()

    def test_loaded_daemon_thread_requires_ready_client_evidence(self):
        ready = "› Continue working\n100% context left"
        cases = (
            (ready, {SESSION_ID}, True),
            (ready.replace("›", "»"), {SESSION_ID}, True),
            (ready.replace("›", "❯"), {SESSION_ID}, True),
            (ready, {"different-session"}, False),
            (ready, None, False),
            (f"codex resume {SESSION_ID}", {SESSION_ID}, False),
            ("Resuming session\n" + ready, {SESSION_ID}, False),
            ("Press enter to continue\n" + ready, {SESSION_ID}, False),
            ("model:       loading\n" + ready, {SESSION_ID}, False),
            (ready + "\n" + "loading\n" * 13, {SESSION_ID}, False),
        )
        for output, loaded, expected in cases:
            with self.subTest(output=output, loaded=loaded), patch.object(
                codex_resume, "_open_rollout_sessions", return_value=set(),
            ), patch.object(codex_resume, "pane_text", return_value=output), patch.object(
                codex_resume, "loaded_thread_ids", return_value=loaded,
            ), patch.object(codex_resume.subprocess, "run", return_value=subprocess.CompletedProcess(
                [], 0, "/dev/pts/7\t0\t2\t0\n", "",
            )), patch.object(codex_resume.os, "readlink", return_value="/dev/pts/7"
            ):
                self.assertEqual(codex_resume.resumed_session(456, SESSION_ID, "%987"), expected)

    def test_composer_requires_live_terminal_cursor_and_footer_geometry(self):
        cases = (
            ("» \n100% context left", "/dev/pts/7\t0\t2\t0", "/dev/pts/7", True),
            ("» \n100% context left", "/dev/pts/7\t0\t2\t0", "/dev/pts/8", False),
            ("» \n100% context left", "/dev/pts/7\t0\t2\t1", "/dev/pts/7", False),
            ("» \n100% context left", "/dev/pts/7\t1\t2\t0", "/dev/pts/7", False),
            ("» \n100% context left", "/dev/pts/7\t99\t2\t0", "/dev/pts/7", False),
            ("› \n" + "loading\n" * 6 + "100% context left", "/dev/pts/7\t0\t2\t0", "/dev/pts/7", False),
            ("Text quoting › and 100% context left", "/dev/pts/7\t0\t2\t0", "/dev/pts/7", False),
            ("100% context left\n› ", "/dev/pts/7\t1\t2\t0", "/dev/pts/7", False),
            ("› \n100% context left", "malformed", "/dev/pts/7", False),
        )
        for screen, metadata, terminal, expected in cases:
            with self.subTest(screen=screen, metadata=metadata, terminal=terminal), patch.object(
                codex_resume.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, metadata, ""),
            ), patch.object(codex_resume.os, "readlink", return_value=terminal):
                self.assertEqual(codex_resume._composer_ready(456, "%987", screen), expected)

    def test_missing_terminal_and_tmux_timeout_fail_closed(self):
        with patch.object(codex_resume.subprocess, "run", side_effect=subprocess.TimeoutExpired("tmux", 1)):
            self.assertFalse(codex_resume._composer_ready(456, "%987", "› \n100% context left"))
        with patch.object(codex_resume.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, "/dev/pts/7\t0\t2\t0", "",
        )), patch.object(codex_resume.os, "readlink", side_effect=FileNotFoundError):
            self.assertFalse(codex_resume._composer_ready(456, "%987", "› \n100% context left"))


class StartupGateTests(unittest.TestCase):
    def test_competing_launcher_waits_until_startup_gate_is_released(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.lock"
            first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(path, os.O_RDWR)
            started = threading.Event()
            acquired = threading.Event()
            failures = []

            def contender():
                started.set()
                try:
                    if codex_resume.acquire_startup_lock(second, timeout=2):
                        acquired.set()
                        fcntl.flock(second, fcntl.LOCK_UN)
                except Exception as error:
                    failures.append(error)

            worker = threading.Thread(target=contender, daemon=True)
            try:
                self.assertTrue(codex_resume.acquire_startup_lock(first, timeout=0))
                worker.start()
                self.assertTrue(started.wait(1))
                self.assertFalse(acquired.wait(0.05))
                fcntl.flock(first, fcntl.LOCK_UN)
                self.assertTrue(acquired.wait(3))
            finally:
                fcntl.flock(first, fcntl.LOCK_UN)
                worker.join(3)
                os.close(first)
                os.close(second)
            self.assertFalse(worker.is_alive())
            self.assertEqual(failures, [])

    def test_queue_wait_is_bounded_when_another_launcher_owns_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "startup.lock"
            first = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(path, os.O_RDWR)
            try:
                fcntl.flock(first, fcntl.LOCK_EX)
                self.assertFalse(codex_resume.acquire_startup_lock(second, timeout=0))
                fcntl.flock(first, fcntl.LOCK_UN)
                self.assertTrue(codex_resume.acquire_startup_lock(second, timeout=0))
            finally:
                os.close(first)
                os.close(second)

    def test_gate_is_shared_by_same_codex_home_but_isolates_different_homes(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            codex_resume, "runtime_root", return_value=Path(directory) / "runtime",
        ):
            with patch.dict(os.environ, {"CODEX_HOME": directory + "/codex"}):
                first = codex_resume.startup_lock_path()
            with patch.dict(os.environ, {"CODEX_HOME": directory + "/unused/../codex"}):
                self.assertEqual(codex_resume.startup_lock_path(), first)
            with patch.dict(os.environ, {"CODEX_HOME": directory + "/other"}):
                self.assertNotEqual(codex_resume.startup_lock_path(), first)
            self.assertEqual(first.parent.stat().st_mode & 0o777, 0o700)


if __name__ == "__main__":
    unittest.main()
