from __future__ import annotations

import io
import os
import hashlib
import json
import signal
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, Mock, call, patch

from gi.repository import GLib

from workspace_state.desktop import desktop_topology_signature
from workspace_state.gnome_session import (
    DESKTOP_SETTLE_SECONDS,
    DelayInhibitor,
    GnomeSessionClient,
    final_save_budget,
    process_start_epoch,
    process_start_time,
)
from workspace_state import login_status, operations, session_ending


class FakeLoop:
    def __init__(self) -> None:
        self.stopped = False

    def quit(self) -> None:
        self.stopped = True


class FakeConnection:
    def __init__(self) -> None:
        self.calls = []

    def call_sync(self, *args):
        self.calls.append(args)



class FakeInhibitor:
    def __init__(self) -> None:
        self.active = False
        self.acquired = 0
        self.released = 0

    def acquire(self) -> None:
        if not self.active:
            self.acquired += 1
        self.active = True

    def release(self) -> None:
        if self.active:
            self.released += 1
        self.active = False


class FakeLogind:
    def __init__(self, delay_usec: int | None = 30_000_000) -> None:
        self.delay_usec = delay_usec

    def call_sync(self, *args):
        if self.delay_usec is None:
            raise GLib.Error("no such property")
        return GLib.Variant("(v)", (GLib.Variant("t", self.delay_usec),))


class FakeProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode = None


class DelayInhibitorTests(unittest.TestCase):
    def test_delay_lock_is_taken_and_released_idempotently(self):
        original, peer = os.pipe()
        self.addCleanup(os.close, original)
        self.addCleanup(os.close, peer)
        held = os.dup(original)
        reply = MagicMock()
        reply.unpack.return_value = (0,)
        descriptors = MagicMock()
        descriptors.get.return_value = held
        connection = MagicMock()
        connection.call_with_unix_fd_list_sync.return_value = (reply, descriptors)
        inhibitor = DelayInhibitor(connection)

        inhibitor.acquire()
        inhibitor.acquire()

        self.assertTrue(inhibitor.active)
        connection.call_with_unix_fd_list_sync.assert_called_once()
        self.assertEqual(connection.call_with_unix_fd_list_sync.call_args.args[4].unpack(), (
            "shutdown", "workspace-state", "Saving the workspace before power-off", "delay"))
        os.fstat(held)
        inhibitor.release()
        inhibitor.release()
        self.assertFalse(inhibitor.active)
        with self.assertRaises(OSError):
            os.fstat(held)


class FinalSaveBudgetTests(unittest.TestCase):
    def test_budget_stays_inside_the_logind_delay_window(self):
        self.assertEqual(final_save_budget(30.0), 20.0)
        self.assertEqual(final_save_budget(5.0), 2.0)
        self.assertEqual(final_save_budget(3.0), 1.0)
        self.assertEqual(final_save_budget(0.5), 1.0)

    def test_unreadable_delay_property_uses_the_systemd_default(self):
        client = GnomeSessionClient(FakeConnection(), FakeLoop(), Path("/tools"),
                                    system_connection=FakeLogind(None))
        self.assertEqual(final_save_budget(client._inhibit_delay_seconds()), 2.0)
        client = GnomeSessionClient(FakeConnection(), FakeLoop(), Path("/tools"),
                                    system_connection=FakeLogind(30_000_000))
        self.assertEqual(final_save_budget(client._inhibit_delay_seconds()), 20.0)


class FinalSaveTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.stack.enter_context(redirect_stdout(self.output))
        runtime = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": runtime}))
        self.marker = Path(runtime) / "workspace-state" / "session-ending.json"
        self.inhibitor = FakeInhibitor()
        self.inhibitor.acquire()
        self.connection = FakeConnection()
        self.loop = FakeLoop()
        self.client = GnomeSessionClient(
            self.connection, self.loop, Path("/tools"),
            delay_inhibitor=self.inhibitor, system_connection=FakeLogind(30_000_000))
        self.client.client_path = "/org/gnome/SessionManager/Client99"
        self.spawned = []
        self.watches = []
        self.timeouts = {}
        self.removed = []
        self.signals = []

        def popen(command, **kwargs):
            self.spawned.append((command, kwargs))
            return FakeProcess(4000 + len(self.spawned))

        def timeout_add(milliseconds, callback, *args):
            identifier = 100 + len(self.timeouts)
            self.timeouts[identifier] = (milliseconds, callback)
            return identifier

        self.stack.enter_context(patch("workspace_state.gnome_session.subprocess.Popen", side_effect=popen))
        self.stack.enter_context(patch.object(GLib, "child_watch_add",
                                              side_effect=lambda *args: self.watches.append(args)))
        self.stack.enter_context(patch.object(GLib, "timeout_add", side_effect=timeout_add))
        self.stack.enter_context(patch.object(GLib, "source_remove", side_effect=self.removed.append))
        self.stack.enter_context(patch("workspace_state.gnome_session.os.killpg",
                                       side_effect=lambda pid, number: self.signals.append((pid, number))))

    def responses(self):
        return [item[4].unpack() for item in self.connection.calls]

    def lines(self):
        return self.output.getvalue().splitlines()

    def finish(self, code=0, index=-1):
        pid = self.watches[index][1]
        self.client._final_child_exited(pid, code << 8)

    def test_prepare_for_shutdown_starts_exactly_one_bounded_save(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_signal("EndSession")
        self.assertEqual(len(self.spawned), 1)
        command, kwargs = self.spawned[0]
        self.assertEqual(command, ["/tools/wsctl", "save", "--auto", "--final"])
        self.assertIs(kwargs["start_new_session"], True)
        self.assertIn("wsctl: final save started; trigger=prepare-for-shutdown; budget=20.0s", self.lines())
        self.assertEqual(next(iter(self.timeouts.values()))[0], 20000)
        self.assertTrue(self.inhibitor.active)

    def test_end_session_waits_for_the_save_then_answers_true(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_signal("EndSession")
        self.assertEqual(self.responses(), [])
        self.finish(0)
        self.assertEqual(self.responses(), [(True, "")])
        self.assertFalse(self.inhibitor.active)
        self.assertTrue(any(line.startswith("wsctl: final save finished; rc=0; elapsed=") for line in self.lines()))
        self.assertIn("wsctl: logind delay lock released", self.lines())
        self.assertIn(next(iter(self.timeouts)), self.removed)

    def test_end_session_after_a_finished_save_answers_at_once(self):
        self.client.handle_prepare_for_shutdown(True)
        self.finish(3)
        self.client.handle_signal("EndSession")
        self.assertEqual(self.responses(), [(True, "")])
        self.assertEqual(len(self.spawned), 1)
        self.assertTrue(any("rc=3" in line for line in self.lines()))

    def test_end_session_without_prepare_starts_the_save(self):
        self.client.handle_signal("QueryEndSession")
        self.assertEqual(self.spawned, [])
        self.client.handle_signal("EndSession")
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("wsctl: final save started; trigger=end-session; budget=20.0s", self.lines())
        self.assertEqual(self.responses(), [(True, "")])
        self.finish(0)
        self.assertEqual(self.responses(), [(True, ""), (True, "")])
        self.client.handle_prepare_for_shutdown(True)
        self.assertEqual(len(self.spawned), 1)

    def test_deadline_terminates_then_kills_the_process_group_and_releases_the_lock(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_signal("EndSession")
        _milliseconds, deadline = self.timeouts[min(self.timeouts)]
        pid = self.watches[0][1]
        self.assertEqual(deadline(), GLib.SOURCE_REMOVE)
        self.assertEqual(self.signals, [(pid, signal.SIGTERM)])
        self.assertTrue(self.inhibitor.active)
        self.assertEqual(self.responses(), [])
        self.assertTrue(any(line.startswith("wsctl: final save stopped at deadline; elapsed=")
                            for line in self.lines()))
        milliseconds, kill = self.timeouts[max(self.timeouts)]
        self.assertEqual(milliseconds, 1000)
        kill()
        self.assertEqual(self.signals, [(pid, signal.SIGTERM), (pid, signal.SIGKILL)])
        self.assertFalse(self.inhibitor.active)
        self.assertIn("wsctl: logind delay lock released", self.lines())
        self.assertEqual(self.responses(), [(True, "")])
        # The reaped child is not reported as a second finished save.
        self.client._final_child_exited(pid, 9)
        self.assertFalse(any("final save finished" in line for line in self.lines()))

    def test_cancelled_shutdown_takes_the_lock_again_and_allows_a_new_save(self):
        self.client.handle_prepare_for_shutdown(True)
        self.finish(0)
        self.assertFalse(self.inhibitor.active)
        self.client.handle_prepare_for_shutdown(False)
        self.assertTrue(self.inhibitor.active)
        self.assertEqual(self.inhibitor.acquired, 2)
        self.client.handle_prepare_for_shutdown(True)
        self.assertEqual(len(self.spawned), 2)

    def test_gnome_cancel_during_the_save_rearms_the_agent_when_it_ends(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_signal("CancelEndSession")
        self.assertIn("wsctl: GNOME CancelEndSession", self.lines())
        self.client.handle_prepare_for_shutdown(True)
        self.assertEqual(len(self.spawned), 1)
        self.assertEqual(self.signals, [])
        self.finish(0)
        self.assertTrue(self.inhibitor.active)
        self.client.handle_signal("EndSession")
        self.assertEqual(len(self.spawned), 2)

    def test_gnome_cancel_after_the_save_takes_the_lock_again(self):
        self.client.handle_signal("EndSession")
        self.finish(0)
        self.assertFalse(self.inhibitor.active)
        self.client.handle_signal("CancelEndSession")
        self.assertTrue(self.inhibitor.active)

    def test_the_agent_never_refuses_or_cancels_the_session_end(self):
        for name in ("QueryEndSession", "EndSession", "CancelEndSession", "EndSession", "Stop"):
            self.client.handle_signal(name)
            self.assertIn(f"wsctl: GNOME {name}", self.lines())
        self.finish(1)
        self.assertTrue(all(value == (True, "") for value in self.responses()))
        self.assertTrue(self.loop.stopped)

    def test_a_save_that_cannot_start_does_not_hold_the_shutdown(self):
        with patch("workspace_state.gnome_session.subprocess.Popen", side_effect=OSError("missing")):
            self.client.handle_signal("EndSession")
        self.assertEqual(self.responses(), [(True, "")])
        self.assertFalse(self.inhibitor.active)

    def test_stop_without_a_save_exits_at_once(self):
        self.client.handle_signal("Stop")
        self.assertTrue(self.loop.stopped)

    def test_stop_during_the_save_exits_after_it(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_signal("Stop")
        self.assertFalse(self.loop.stopped)
        self.finish(0)
        self.assertTrue(self.loop.stopped)

    def marker_document(self):
        return json.loads(self.marker.read_text())

    def test_the_end_signals_write_a_private_marker(self):
        for start in (lambda: self.client.handle_prepare_for_shutdown(True),
                      lambda: self.client.handle_signal("EndSession")):
            with self.subTest(start=start):
                self.marker.unlink(missing_ok=True)
                self.client._final_started = False
                start()
                document = self.marker_document()
                self.assertEqual(document["pid"], os.getpid())
                self.assertEqual(document["boot_id"], operations.boot_id())
                self.assertEqual(document["process_start"], session_ending.process_start_time(os.getpid()))
                self.assertEqual(self.marker.stat().st_mode & 0o777, 0o600)
                self.assertEqual(self.marker.parent.stat().st_mode & 0o777, 0o700)
        self.assertIn(document["trigger"], {"prepare-for-shutdown", "end-session"})
        self.assertEqual(document["trigger"], "end-session")

    def test_the_marker_is_removed_when_the_agent_rearms(self):
        self.client.handle_prepare_for_shutdown(True)
        self.finish(0)
        self.assertTrue(self.marker.exists())
        self.client.handle_signal("CancelEndSession")
        self.assertFalse(self.marker.exists())

    def test_the_marker_is_removed_by_a_rearm_deferred_until_the_save_ends(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.handle_prepare_for_shutdown(False)
        self.assertTrue(self.marker.exists())
        self.finish(0)
        self.assertFalse(self.marker.exists())

    def test_register_removes_a_stale_marker_first(self):
        self.client.handle_prepare_for_shutdown(True)
        self.assertTrue(self.marker.exists())
        self.client.connection = MagicMock()
        self.client.connection.call_sync.side_effect = RuntimeError("stop here")
        with self.assertRaises(RuntimeError):
            self.client.register()
        self.assertFalse(self.marker.exists())

    def test_sigterm_during_the_save_defers_the_exit(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.stop()
        self.assertFalse(self.loop.stopped)
        self.finish(0)
        self.assertTrue(self.loop.stopped)


class ServiceUnitTests(unittest.TestCase):
    def test_stop_signals_only_the_agent_and_allows_the_final_save_to_end(self):
        unit = (Path(__file__).resolve().parents[1] / "systemd/wsctl-gnome-session.service").read_text()
        self.assertIn("\nKillMode=mixed\n", unit)
        self.assertIn("\nTimeoutStopSec=30\n", unit)


class GnomeSessionClientTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_directory.cleanup)
        environment = patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": self.runtime_directory.name},
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        for name in ("claim_startup_hud", "fail_active", "initialize_login_status", "update_stage"):
            patcher = patch(f"workspace_state.gnome_session.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        # Another test may leave an operation context bound; none belongs here.
        previous_context = operations.current()
        operations.bind(None)
        self.addCleanup(operations.bind, previous_context)
        # Some tests clear os.environ: the runtime directory must stay this temporary one.
        runtime = Path(self.runtime_directory.name) / "workspace-state"
        for target in ("workspace_state.gnome_session.runtime_root", "workspace_state.login_status.runtime_root"):
            patch(target, return_value=runtime).start()
        # The launch record is the real status document in the dedicated tests.
        self.claim = patch("workspace_state.gnome_session.claim_restore_launch",
                           return_value="claimed").start()
        # Never ask the live user manager about a unit.
        self.unit_live = patch("workspace_state.gnome_session.unit_is_live", return_value=False).start()
        self.addCleanup(patch.stopall)

    def _client(self):
        connection = FakeConnection()
        client = GnomeSessionClient(connection, FakeLoop(), Path("/tools"))
        client.client_path = "/org/gnome/SessionManager/Client99"
        callbacks = []

        def spawn(command, finished):
            callbacks.append((command, finished))

        patcher = patch.object(client, "_spawn", side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, connection, callbacks

    def _restore_context(self):
        context = operations.OperationContext.create("a" * 16, "startup", budget=100)
        previous = operations.current()
        operations.bind(context)
        self.addCleanup(operations.bind, previous)
        return context

    def test_start_restore_launches_exactly_one_fixed_named_service(self):
        client, _connection, callbacks = self._client()
        context = self._restore_context()
        client._login_key = "b" * 16

        self.assertEqual(client.start_restore(), GLib.SOURCE_REMOVE)

        self.assertEqual(len(callbacks), 1)
        command = callbacks[0][0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertEqual(command.count("--unit=wsctl-startup-restore-" + "b" * 16 + ".service"), 1)
        self.claim.assert_called_once_with(f"wsctl-startup-restore-{'b' * 16}.service", "b" * 16)
        for expected in ("--user", "--collect", "--service-type=exec",
                         "--property=KillMode=control-group",
                         "--property=PartOf=graphical-session.target",
                         "--property=After=graphical-session.target"):
            self.assertIn(expected, command)
        runtime = next(item for item in command if item.startswith("--property=RuntimeMaxSec="))
        self.assertTrue(0 < float(runtime.split("=", 2)[2].rstrip("s")) <= 100)
        environment = next(item for item in command if item.startswith("--setenv=WSCTL_OPERATION_CONTEXT="))
        self.assertEqual(json.loads(environment.split("=", 2)[2]), context.to_dict())
        report = next(item for item in command if item.startswith("--property=ExecStopPost="))
        self.assertEqual(report, '--property=ExecStopPost="/tools/wsctl-python" workspace_state.startup worker-exit')
        self.assertEqual(command[command.index("--") + 1:], ["/tools/wsctl", "startup", "--wait", "120"])
        for retired in ("_run_direct_restore", "_transient_service", "_attached_terminal_available",
                        "_bootstrap_terminal_service"):
            self.assertFalse(hasattr(client, retired))

    def test_a_systemd_run_failure_fails_the_active_stage(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        client.start_restore()
        callbacks.pop()[1](1)
        from workspace_state import gnome_session
        self.assertIn("systemd-run exit status 1", gnome_session.fail_active.call_args.args[0])
        gnome_session.fail_active.reset_mock()
        client.start_restore()
        callbacks.pop()[1](127)  # systemd-run itself could not be executed
        gnome_session.fail_active.assert_called_once()
        gnome_session.fail_active.reset_mock()
        client.start_restore()
        callbacks.pop()[1](0)
        gnome_session.fail_active.assert_not_called()

    def test_a_recorded_launch_of_this_login_spawns_nothing(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        self.claim.return_value = "present"
        self.assertEqual(client.start_restore(), GLib.SOURCE_REMOVE)
        self.assertEqual(callbacks, [])
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_not_called()

    def test_an_unwritable_launch_record_fails_and_spawns_nothing(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        self.claim.return_value = "failed"
        client.start_restore()
        self.assertEqual(callbacks, [])
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_called_once()

    def test_an_expired_operation_fails_instead_of_launching(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        with patch.object(operations.OperationContext, "check",
                          side_effect=TimeoutError("operation expired")):
            client.start_restore()
        self.assertEqual(callbacks, [])
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_called_once()
        self.assertIn("did not start", gnome_session.fail_active.call_args.args[0])
        self.claim.assert_not_called()  # the status is left alone

    def _real_status(self, login_key="b" * 16):
        self.addCleanup(operations.bind, operations.current())
        self.assertTrue(login_status.initialize("a" * 16, login_key=login_key, show_startup_hud=False))
        runtime = Path(self.runtime_directory.name) / "workspace-state"
        (runtime / "login-generation").write_text("a" * 16 + "\n")
        (runtime / "login-key").write_text(login_key + "\n")
        return runtime

    def test_a_launcher_claim_of_an_older_release_is_no_longer_read(self):
        runtime = self._real_status()
        client, _connection, callbacks = self._client()
        client._login_key = "b" * 16
        from workspace_state.startup import startup_directory
        (startup_directory(runtime, operations.boot_id(), "b" * 16) / "launcher.claimed").touch()
        client.start_restore()
        self.assertEqual(len(callbacks), 1)

    def test_a_live_service_of_the_fixed_name_means_already_started(self):
        self._real_status()
        client, _connection, callbacks = self._client()
        client._login_key = "b" * 16
        self.unit_live.return_value = True
        before = login_status.status_path().read_bytes()
        client.start_restore()
        self.assertEqual(callbacks, [])
        self.claim.assert_not_called()
        self.unit_live.assert_called_with("wsctl-startup-restore-" + "b" * 16 + ".service")
        self.assertEqual(login_status.status_path().read_bytes(), before)
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_not_called()

    def test_an_unclear_unit_answer_does_not_block_the_launch(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        self.unit_live.return_value = False  # a timeout or an unknown state reads as not live
        client.start_restore()
        self.assertEqual(len(callbacks), 1)

    def test_a_systemd_run_failure_next_to_a_live_service_is_only_logged(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        client.start_restore()
        self.unit_live.return_value = True
        with patch("builtins.print") as printed:
            callbacks.pop()[1](1)
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_not_called()
        self.assertIn("already running", printed.call_args.args[0])

    def test_a_systemd_run_failure_with_no_live_service_still_fails_the_stage(self):
        client, _connection, callbacks = self._client()
        self._restore_context()
        client.start_restore()
        callbacks.pop()[1](1)
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_called_once()

    def test_another_logins_service_does_not_block(self):
        self._real_status()
        client, _connection, callbacks = self._client()
        client._login_key = "b" * 16
        client.start_restore()
        self.unit_live.assert_called_with("wsctl-startup-restore-" + "b" * 16 + ".service")
        self.assertEqual(len(callbacks), 1)

    def test_a_recorded_launch_survives_an_expired_deadline_without_touching_status(self):
        self._real_status()
        unit = "wsctl-startup-restore-" + "b" * 16 + ".service"
        self.assertEqual(login_status.claim_restore_launch(unit, "b" * 16), "claimed")
        client, _connection, callbacks = self._client()
        client._login_key = "b" * 16
        before = login_status.status_path().read_bytes()
        with patch.object(operations.OperationContext, "check", side_effect=TimeoutError("expired")):
            client.start_restore()
        self.assertEqual(callbacks, [])
        self.assertEqual(login_status.status_path().read_bytes(), before)
        from workspace_state import gnome_session
        gnome_session.fail_active.assert_not_called()

    def _login(self, client, *, pid=100, start=5000, boot="boot-a", owner=":1.42"):
        reply = Mock()
        reply.unpack.return_value = (pid,)
        client.connection.call_sync = Mock(return_value=reply)
        with patch("workspace_state.gnome_session.process_start_time", return_value=start), \
                patch.object(operations, "boot_id", return_value=boot):
            return client._record_login(owner)[:2]

    def test_session_id_follows_the_bus_name_and_login_key_the_process(self):
        client, _connection, _callbacks = self._client()
        session_id, login_key = self._login(client)
        root = Path(self.runtime_directory.name) / "workspace-state"

        self.assertEqual(session_id, hashlib.sha256(b":1.42").hexdigest()[:16])
        self.assertEqual(login_key, hashlib.sha256(b"boot-a:100:5000").hexdigest()[:16])
        for name, value in (("login-generation", session_id), ("login-key", login_key)):
            self.assertEqual((root / name).read_text(), f"{value}\n")
            self.assertEqual((root / name).stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._login(client), (session_id, login_key))

    def test_same_bus_name_with_another_process_is_a_new_login(self):
        client, _connection, _callbacks = self._client()
        session_id, login_key = self._login(client)

        for changes in ({"pid": 101}, {"start": 5001}, {"boot": "boot-b"}):
            with self.subTest(changes=changes):
                other_session, other_key = self._login(client, **changes)
                self.assertEqual(other_session, session_id)
                self.assertNotEqual(other_key, login_key)

    def test_session_id_is_published_before_the_login_key(self):
        # A key older than the session id is stale, so the key goes second.
        client, _connection, _callbacks = self._client()
        published = []
        real = Path.replace

        def replace(path, target):
            published.append(Path(target).name)
            return real(path, target)

        with patch.object(Path, "replace", replace):
            self._login(client)
        self.assertEqual(published, ["login-generation", "login-key"])

    def test_process_start_epoch_is_boot_time_plus_start_ticks_in_seconds(self):
        with patch.object(Path, "read_text", return_value="cpu 1 2 3\nbtime 1000\nprocesses 9\n"), \
                patch("workspace_state.gnome_session.os.sysconf", return_value=100):
            self.assertEqual(process_start_epoch(500), 1005.0)

    def test_process_start_time_survives_awkward_comm(self):
        fields = " ".join(str(number) for number in range(3, 53))
        with patch.object(Path, "read_text", return_value=f"100 (a b) c) {fields}\n"):
            self.assertEqual(process_start_time(100), 22)

    def test_failed_process_lookup_uses_the_session_id_and_reports_it(self):
        client, _connection, _callbacks = self._client()
        client.connection.call_sync = Mock(side_effect=GLib.Error("gone"))
        with patch("workspace_state.gnome_session.append_diagnostic") as diagnostic:
            session_id, login_key, _process = client._record_login(":1.42")

        self.assertEqual(session_id, hashlib.sha256(b":1.42").hexdigest()[:16])
        self.assertEqual(login_key, session_id)
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args[0], "login identity")
        root = Path(self.runtime_directory.name) / "workspace-state"
        self.assertEqual((root / "login-key").read_text(), f"{session_id}\n")

    def test_registered_status_carries_both_identifiers(self):
        client, _connection, _callbacks = self._client()
        connection = MagicMock()
        connection.call_sync.side_effect = [
            GLib.Variant("(o)", ("/org/gnome/SessionManager/Client99",)),
            GLib.Variant("(s)", (":1.42",)),
            GLib.Variant("(u)", (4242,)),
        ]
        client.connection = connection
        session_id = hashlib.sha256(b":1.42").hexdigest()[:16]
        login_key = hashlib.sha256(f"{operations.boot_id()}:4242:777".encode()).hexdigest()[:16]
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        with patch.dict(os.environ, {"XDG_STATE_HOME": state.name}), \
                patch("workspace_state.gnome_session.process_start_time", return_value=777), \
                patch("workspace_state.gnome_session.claim_startup_hud", login_status.claim_startup_hud), \
                patch("workspace_state.gnome_session.initialize_login_status", login_status.initialize):
            client.register()
            claim = json.loads(login_status.boot_claim_path().read_text())
        document = json.loads(login_status.status_path().read_text())
        self.assertEqual(document["session_id"], session_id)
        self.assertEqual(document["operation_context"]["login_generation"], session_id)
        self.assertEqual(document["login_key"], login_key)
        self.assertNotEqual(login_key, session_id)
        self.assertEqual(claim["login_key"], login_key)

    def test_a_restarted_agent_in_the_same_login_does_not_restore_again(self):
        self.addCleanup(operations.bind, operations.current())
        real = patch("workspace_state.gnome_session.claim_restore_launch",
                     login_status.claim_restore_launch)
        real.start()
        self.assertTrue(login_status.initialize("a" * 16, login_key="b" * 16, show_startup_hud=False))
        first, _connection, first_calls = self._client()
        first._login_key = "b" * 16
        first.start_restore()
        self.assertEqual(len(first_calls), 1)

        # The agent restarts: it reattaches to the running operation of this login.
        self.assertTrue(login_status.initialize("a" * 16, login_key="b" * 16, show_startup_hud=False))
        restarted, _connection, restarted_calls = self._client()
        restarted._login_key = "b" * 16
        restarted.start_restore()
        self.assertEqual(restarted_calls, [])

        # A later login has its own status document and starts its own restore.
        self.assertTrue(login_status.initialize("a" * 16, login_key="d" * 16, show_startup_hud=False))
        later, _connection, later_calls = self._client()
        later._login_key = "d" * 16
        later.start_restore()
        self.assertEqual(len(later_calls), 1)
        self.assertIn("--unit=wsctl-startup-restore-" + "d" * 16 + ".service", later_calls[0][0])

    def test_waits_without_claiming_before_wayland_is_ready(self):
        client, _connection, callbacks = self._client()
        with patch.object(client, "_systemd_environment", return_value={}), patch.object(
            client, "_wayland_socket_ready", return_value=False,
        ):
            result = client.wait_for_graphical_environment()

        self.assertEqual(result, GLib.SOURCE_CONTINUE)
        self.assertEqual(callbacks, [])

    def test_waits_without_claiming_before_graphical_session_target(self):
        client, _connection, callbacks = self._client()
        environment = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-test",
        }
        with patch.object(
            client, "_systemd_environment", return_value=environment,
        ), patch.object(
            client, "_wayland_socket_ready", return_value=True,
        ), patch.object(
            client, "_graphical_session_ready", return_value=False,
        ):
            result = client.wait_for_graphical_environment()

        self.assertEqual(result, GLib.SOURCE_CONTINUE)
        self.assertEqual(callbacks, [])

    def test_imports_graphical_environment_before_claiming_restore(self):
        client, _connection, callbacks = self._client()
        # Only the settle interval is under test, not the restore launch.
        start = patch.object(client, "start_restore", return_value=GLib.SOURCE_REMOVE).start()
        environment = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-test",
            "DISPLAY": ":9",
        }
        shell = {
            "available": True,
            "capabilities": [
                "list_windows", "list_monitors", "list_workspaces",
                "place_window", "expect_window", "expectation_status",
                "monitor_recovery", "placement_lifecycle_v2", "staged_placement_handoff",
            ],
            "monitors": [{"index": 0}],
            "workspaces": [{"index": 0, "name": "Life"}],
            "monitor_policy": {
                "display_identity_ready": True,
                "display_identity_cache_valid": True,
                "recovery_active": False,
                "screen_unavailable": False,
            },
        }
        with patch.object(
            client, "_systemd_environment", return_value=environment,
        ), patch.object(
            client, "_wayland_socket_ready", return_value=True,
        ), patch.object(
            client, "_graphical_session_ready", return_value=True,
        ), patch("workspace_state.gnome_session.capture_shell", return_value=shell), patch.dict(
            "os.environ", {}, clear=True,
        ):
            client._desktop_ready_since = time.monotonic() - DESKTOP_SETTLE_SECONDS
            client._desktop_signature = desktop_topology_signature(shell)
            result = client.wait_for_graphical_environment()
            self.assertEqual(os.environ["WAYLAND_DISPLAY"], "wayland-test")
            self.assertEqual(os.environ["DISPLAY"], ":9")

        self.assertEqual(result, GLib.SOURCE_REMOVE)
        start.assert_called_once_with()
        self.assertEqual(callbacks, [])

    def test_waits_for_display_handoff_settle_interval(self):
        client, _connection, callbacks = self._client()
        # Only the settle interval is under test, not the restore launch.
        start = patch.object(client, "start_restore", return_value=GLib.SOURCE_REMOVE).start()
        environment = {
            "XDG_RUNTIME_DIR": "/run/user/1000",
            "WAYLAND_DISPLAY": "wayland-test",
        }
        with patch.object(
            client, "_systemd_environment", return_value=environment,
        ), patch.object(
            client, "_wayland_socket_ready", return_value=True,
        ), patch.object(
            client, "_graphical_session_ready", return_value=True,
        ), patch("workspace_state.gnome_session.capture_shell", return_value={
            "available": True,
            "capabilities": [
                "list_windows", "list_monitors", "list_workspaces",
                "place_window", "expect_window", "expectation_status",
                "monitor_recovery", "placement_lifecycle_v2", "staged_placement_handoff",
            ],
            "monitors": [{"index": 0}],
            "workspaces": [{"index": 0, "name": "Life"}],
            "monitor_policy": {
                "display_identity_ready": True,
                "display_identity_cache_valid": True,
                "recovery_active": False,
                "screen_unavailable": False,
            },
        }), patch("workspace_state.gnome_session.time.monotonic", side_effect=[100.0, 102.0]):
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_CONTINUE)
            self.assertEqual(callbacks, [])
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_REMOVE)

        start.assert_called_once_with()
        self.assertEqual(callbacks, [])

    def test_waits_while_display_recovery_is_active(self):
        client, _connection, callbacks = self._client()
        shell = {
            "available": True,
            "capabilities": [
                "list_windows", "list_monitors", "list_workspaces",
                "place_window", "expect_window", "expectation_status",
                "monitor_recovery", "placement_lifecycle_v2", "staged_placement_handoff",
            ],
            "monitors": [{"index": 0}],
            "workspaces": [{"index": 0, "name": "Life"}],
            "monitor_policy": {
                "display_identity_ready": True,
                "display_identity_cache_valid": True,
                "recovery_active": True,
                "screen_unavailable": False,
            },
        }
        with patch.object(
            client,
            "_systemd_environment",
            return_value={"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-test"},
        ), patch.object(
            client, "_wayland_socket_ready", return_value=True,
        ), patch.object(
            client, "_graphical_session_ready", return_value=True,
        ), patch("workspace_state.gnome_session.capture_shell", return_value=shell):
            result = client.wait_for_graphical_environment()

        self.assertEqual(result, GLib.SOURCE_CONTINUE)
        self.assertEqual(callbacks, [])
        self.assertIsNone(client._desktop_ready_since)

    def test_topology_change_restarts_the_login_settle_interval(self):
        client, _connection, callbacks = self._client()
        # Only the settle interval is under test, not the restore launch.
        start = patch.object(client, "start_restore", return_value=GLib.SOURCE_REMOVE).start()

        def shell(connector):
            return {
                "available": True,
                "capabilities": [
                    "list_windows", "list_monitors", "list_workspaces",
                    "place_window", "expect_window", "expectation_status",
                    "monitor_recovery", "placement_lifecycle_v2", "staged_placement_handoff",
                ],
                "monitors": [{"index": 0, "connector": connector}],
                "workspaces": [{"index": 0, "name": "Life"}],
                "monitor_policy": {
                    "display_identity_ready": True,
                    "display_identity_cache_valid": True,
                    "recovery_active": False,
                    "screen_unavailable": False,
                },
            }

        with patch.object(
            client,
            "_systemd_environment",
            return_value={"XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-test"},
        ), patch.object(
            client, "_wayland_socket_ready", return_value=True,
        ), patch.object(
            client, "_graphical_session_ready", return_value=True,
        ), patch(
            "workspace_state.gnome_session.capture_shell",
            side_effect=[shell("DP-1"), shell("HDMI-1"), shell("HDMI-1")],
        ), patch(
            "workspace_state.gnome_session.time.monotonic",
            side_effect=[100.0, 103.0, 106.0],
        ):
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_CONTINUE)
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_CONTINUE)
            self.assertEqual(callbacks, [])
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_REMOVE)

        start.assert_called_once_with()
        self.assertEqual(callbacks, [])

    def test_register_reattaches_completed_startup_without_replaying_or_changing_status(self):
        client, _connection, callbacks = self._client()
        connection = MagicMock()
        connection.call_sync.side_effect = [
            GLib.Variant("(o)", ("/org/gnome/SessionManager/Client99",)),
            GLib.Variant("(s)", (":1.42",)),
            GLib.Variant("(u)", (4242,)),
        ]
        client.connection = connection
        generation = hashlib.sha256(b":1.42").hexdigest()[:16]
        login_key = hashlib.sha256(f"{operations.boot_id()}:4242:777".encode()).hexdigest()[:16]
        start_time = patch("workspace_state.gnome_session.process_start_time", return_value=777)
        start_time.start()
        self.addCleanup(start_time.stop)
        context = operations.OperationContext.create(generation, "startup", budget=1)
        document = {
            "schema_version": 1, "mode": "startup", "session_id": generation, "login_key": login_key,
            "operation_context": context.to_dict(), "operation_id": context.operation_id,
            "operation_state": "completed", "show_startup_hud": False,
            "stages": [{"id": "gnome", "state": "ready"}, {"id": "displays", "state": "ready"}],
        }
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        status = root / "login-hud-status.json"
        status.write_text(json.dumps(document))
        status.chmod(0o600)
        before = status.read_bytes()
        previous_context = operations.current()
        self.addCleanup(operations.bind, previous_context)
        with patch("workspace_state.gnome_session.time.monotonic", return_value=context.deadline + 1), \
             patch("workspace_state.gnome_session.initialize_login_status") as initialize, \
             patch("workspace_state.gnome_session.claim_startup_hud") as claim, \
             patch("workspace_state.gnome_session.update_stage") as update:
            client.register()
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_REMOVE)
            self.assertEqual(client.start_restore(), GLib.SOURCE_REMOVE)
            client._poll_placement_progress()
        initialize.assert_not_called()
        claim.assert_not_called()
        update.assert_not_called()
        self.assertEqual(callbacks, [])
        self.assertEqual(status.read_bytes(), before)
        self.assertEqual(client._operation_context, context)
        self.assertTrue(client._startup_completed)
        connection.signal_subscribe.assert_called_once()

    def _register_login(self, *, pid, start, completed=None, drop_key=False, began=0.0):
        """Register one login on a shared runtime, as the coordinator does."""
        client, _connection, _callbacks = self._client()
        connection = MagicMock()
        connection.call_sync.side_effect = [
            GLib.Variant("(o)", ("/org/gnome/SessionManager/Client99",)),
            GLib.Variant("(s)", (":1.42",)),
            GLib.Variant("(u)", (pid,)),
        ]
        client.connection = connection
        if completed is not None:
            status = json.loads(login_status.status_path().read_text())
            status["operation_state"] = completed
            if drop_key:
                # What a release without login keys wrote.
                del status["login_key"], status["login_process"]
            login_status.status_path().write_text(json.dumps(status))
            login_status.status_path().chmod(0o600)
        # `began` is when the session manager process started, relative to now.
        with patch("workspace_state.gnome_session.process_start_time", return_value=start), \
                patch("workspace_state.gnome_session.process_start_epoch", return_value=time.time() + began), \
                patch("workspace_state.gnome_session.claim_startup_hud", return_value=True), \
                patch("workspace_state.gnome_session.initialize_login_status", login_status.initialize):
            client.register()
        return client, json.loads(login_status.status_path().read_text())

    def test_same_bus_name_and_process_reattaches_without_a_new_operation(self):
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        client, first = self._register_login(pid=4242, start=777)
        self.assertFalse(client._startup_completed)
        client, again = self._register_login(pid=4242, start=777, completed="completed")
        self.assertTrue(client._startup_completed)
        self.assertEqual(again["operation_id"], first["operation_id"])
        self.assertEqual(again["operation_state"], "completed")

    def test_same_bus_name_with_another_process_starts_a_fresh_operation(self):
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        _client, first = self._register_login(pid=4242, start=777)
        client, second = self._register_login(pid=4242, start=778, completed="completed")
        self.assertEqual(second["session_id"], first["session_id"])
        self.assertNotEqual(second["login_key"], first["login_key"])
        self.assertNotEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(second["operation_state"], "running")
        self.assertFalse(client._startup_completed)
        self.assertEqual(client._operation_context.operation_id, second["operation_id"])

    def test_completed_status_without_a_login_key_is_a_previous_login(self):
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        _client, first = self._register_login(pid=4242, start=777)
        # The session manager started after that status: an earlier login.
        client, second = self._register_login(
            pid=4242, start=777, completed="completed", drop_key=True, began=60)
        self.assertNotEqual(second["operation_id"], first["operation_id"])
        self.assertEqual(second["operation_state"], "running")
        self.assertNotEqual(second["login_key"], first["session_id"])
        self.assertFalse(client._startup_completed)

    def test_upgrade_in_a_finished_login_reattaches_to_the_old_release_status(self):
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        _client, first = self._register_login(pid=4242, start=777)
        # The old release began this login; the same session manager still runs.
        client, again = self._register_login(
            pid=4242, start=777, completed="completed", drop_key=True, began=-60)
        self.assertTrue(client._startup_completed)
        self.assertEqual(again["operation_id"], first["operation_id"])
        # Its directory, claim and status are named by the session id.
        root = Path(self.runtime_directory.name) / "workspace-state"
        self.assertEqual((root / "login-key").read_text().strip(), first["session_id"])
        # A later restart of the new release keeps the same key.
        client, third = self._register_login(pid=4242, start=777, began=-60)
        self.assertEqual((root / "login-key").read_text().strip(), first["session_id"])
        self.assertEqual(third["operation_id"], first["operation_id"])

    def test_upgrade_in_the_middle_of_a_restore_continues_the_same_operation(self):
        previous = operations.current()
        self.addCleanup(operations.bind, previous)
        _client, first = self._register_login(pid=4242, start=777)
        client, again = self._register_login(
            pid=4242, start=777, completed="running", drop_key=True, began=-60)
        self.assertFalse(client._startup_completed)
        self.assertEqual(again["operation_id"], first["operation_id"])
        self.assertEqual(again["operation_state"], "running")
        # Stamped, so a further restart of the new release agrees.
        self.assertEqual(again["login_key"], first["session_id"])
        self.assertNotEqual(again["login_process"], again["login_key"])
        _client, last = self._register_login(pid=4242, start=777, began=-60)
        self.assertEqual(last["operation_id"], first["operation_id"])
        self.assertEqual(last["login_key"], first["session_id"])

    def test_completed_startup_reattach_rejects_other_login_boot_and_untrusted_status(self):
        client, _connection, _callbacks = self._client()
        generation = "a" * 16
        login_key = "c" * 16
        context = operations.OperationContext.create(generation, "startup")
        document = {
            "schema_version": 1, "mode": "startup", "session_id": generation, "login_key": login_key,
            "operation_context": context.to_dict(), "operation_id": context.operation_id,
            "operation_state": "completed",
        }
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        status = root / "login-hud-status.json"
        cases = [
            {"session_id": "b" * 16}, {"operation_state": "running"},
            # The same bus name with another session manager process is a new login.
            {"login_key": "d" * 16}, {"login_key": None},
            {"operation_context": dict(context.to_dict(), boot_id="another-boot")},
            {"operation_id": "different-operation"}, {"operation_context": None},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                status.write_text(json.dumps(dict(document, **changes)))
                status.chmod(0o600)
                self.assertFalse(client._reattach_completed_startup(generation, login_key))
        status.write_text(json.dumps(document))
        status.chmod(0o644)
        self.assertFalse(client._reattach_completed_startup(generation, login_key))
        self.assertFalse(client._startup_completed)

    def test_idle_poll_is_passive(self):
        client, _connection, _callbacks = self._client()
        self.assertEqual(client.poll_progress(), GLib.SOURCE_CONTINUE)


class RestoreLaunchRecordTests(unittest.TestCase):
    """The launch record lives in the status document, once per login."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(operations.bind, operations.current())
        self.assertTrue(login_status.initialize("a" * 16, login_key="b" * 16, show_startup_hud=False))
        self.unit = "wsctl-startup-restore-" + "b" * 16 + ".service"

    def document(self):
        return json.loads(login_status.status_path().read_text())

    def test_first_claim_wins_and_a_restarted_agent_finds_it(self):
        self.assertEqual(login_status.claim_restore_launch(self.unit, "b" * 16), "claimed")
        self.assertEqual(login_status.claim_restore_launch(self.unit, "b" * 16), "present")
        document = self.document()
        self.assertEqual((document["restore_unit"], document["restore_login_key"]), (self.unit, "b" * 16))
        # The agent's own restart reattaches to the same document and keeps the record.
        self.assertTrue(login_status.initialize("a" * 16, login_key="b" * 16, show_startup_hud=False))
        self.assertEqual(login_status.claim_restore_launch(self.unit, "b" * 16), "present")

    def test_the_record_adds_no_context_keys_and_no_stage_states(self):
        before = self.document()
        login_status.claim_restore_launch(self.unit, "b" * 16)
        after = self.document()
        self.assertEqual(set(after["operation_context"]), set(before["operation_context"]))
        self.assertEqual(len(after["operation_context"]), 6)
        self.assertEqual([stage["state"] for stage in after["stages"]],
                         [stage["state"] for stage in before["stages"]])
        self.assertEqual(set(after) - set(before), {"restore_unit", "restore_login_key"})

    def test_a_record_of_another_login_does_not_block(self):
        other = "wsctl-startup-restore-" + "c" * 16 + ".service"
        self.assertEqual(login_status.claim_restore_launch(other, "c" * 16), "claimed")
        self.assertEqual(login_status.claim_restore_launch(self.unit, "b" * 16), "claimed")
        self.assertEqual(self.document()["restore_unit"], self.unit)

    def test_a_new_login_starts_without_a_record(self):
        login_status.claim_restore_launch(self.unit, "b" * 16)
        self.assertTrue(login_status.initialize("a" * 16, login_key="d" * 16, show_startup_hud=False))
        self.assertNotIn("restore_unit", self.document())

    def test_an_unwritable_status_reports_failure(self):
        with patch.object(login_status, "_locked_update", return_value=False):
            self.assertEqual(login_status.claim_restore_launch(self.unit, "b" * 16), "failed")
