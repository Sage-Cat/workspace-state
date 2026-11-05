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
from unittest.mock import MagicMock, call, patch

from gi.repository import GLib

from workspace_state.desktop import desktop_topology_signature
from workspace_state.gnome_session import (
    DESKTOP_SETTLE_SECONDS,
    DelayInhibitor,
    GnomeSessionClient,
    final_save_budget,
)
from workspace_state import operations


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

    def test_sigterm_during_the_save_defers_the_exit(self):
        self.client.handle_prepare_for_shutdown(True)
        self.client.stop()
        self.assertFalse(self.loop.stopped)
        self.finish(0)
        self.assertTrue(self.loop.stopped)


class BootstrapTerminalEvidenceTests(unittest.TestCase):
    def probe(self, shell, output="456\n", code=0, ancestor=123):
        with patch("workspace_state.gnome_session.capture_shell", return_value=shell), patch(
            "workspace_state.gnome_session.subprocess.run",
            return_value=subprocess.CompletedProcess([], code, output, ""),
        ), patch("workspace_state.capture._alacritty_ancestor", return_value=ancestor):
            return GnomeSessionClient._attached_terminal_available()

    def test_existing_native_terminal_must_have_an_attached_tmux_client(self):
        shell = {"available": True, "windows": [{"pid": 123}]}
        self.assertTrue(self.probe(shell))
        self.assertFalse(self.probe(shell, output=""))
        self.assertFalse(self.probe(shell, output="invalid\n"))
        self.assertFalse(self.probe(shell, code=1))
        self.assertFalse(self.probe(shell, ancestor=None))
        self.assertFalse(self.probe(shell, ancestor=999))

    def test_background_tmux_or_unavailable_window_evidence_does_not_suppress_bootstrap(self):
        for shell in ({"available": False, "windows": [{"pid": 123}]},
                      {"available": True, "windows": []},
                      {"available": True, "windows": [{"pid": "123"}, {"pid": -1}]}):
            with self.subTest(shell=shell):
                self.assertFalse(self.probe(shell))

    def test_disappearing_server_and_bounded_probe_timeout_allow_normal_bootstrap(self):
        shell = {"available": True, "windows": [{"pid": 123}]}
        for error in (OSError("server disappeared"), subprocess.TimeoutExpired("tmux", 1)):
            with self.subTest(error=error), patch(
                "workspace_state.gnome_session.capture_shell", return_value=shell,
            ), patch("workspace_state.gnome_session.subprocess.run", side_effect=error):
                self.assertFalse(GnomeSessionClient._attached_terminal_available())



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
        terminal = patch.object(GnomeSessionClient, "_attached_terminal_available", return_value=False)
        terminal.start()
        self.addCleanup(terminal.stop)

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

    def test_first_login_claim_launches_one_bootstrap_alacritty(self):
        client, _connection, callbacks = self._client()

        client.start_restore()
        self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-launch"])
        callbacks.pop(0)[1](0)

        self.assertEqual(callbacks[0][0][-1], "/usr/bin/alacritty")
        self.assertIn("--service-type=exec", callbacks[0][0])
        self.assertIn("--property=ExitType=main", callbacks[0][0])
        self.assertIn("--property=KillMode=process", callbacks[0][0])
        self.assertNotIn("--property=ExitType=cgroup", callbacks[0][0])
        self.assertNotIn("--property=KillMode=mixed", callbacks[0][0])

    def test_new_claim_with_attached_native_terminal_keeps_only_scheduled_worker(self):
        client, _connection, callbacks = self._client()
        client.start_restore()
        claimed = callbacks.pop(0)[1]
        with patch.object(client, "_attached_terminal_available", return_value=True), patch.object(
            client, "_run_direct_restore",
        ) as direct:
            claimed(0)
        self.assertEqual(callbacks, [])
        direct.assert_not_called()

    def test_login_generation_is_stable_for_one_session_manager_owner(self):
        client, _connection, _callbacks = self._client()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            client._record_login_generation(":1.42")
            marker = Path(directory) / "workspace-state/login-generation"

            self.assertEqual(
                marker.read_text().strip(),
                hashlib.sha256(b":1.42").hexdigest()[:16],
            )
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_service_restart_retries_without_another_alacritty(self):
        client, _connection, callbacks = self._client()

        client.start_restore()
        callbacks.pop(0)[1](1)

        self.assertEqual(callbacks[0][0][-3:], ["/tools/wsctl", "startup", "--await-tmux"])
        self.assertIn("--service-type=exec", callbacks[0][0])

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
        self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-launch"])

    def test_waits_for_display_handoff_settle_interval(self):
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

        self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-launch"])

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

        self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-launch"])

    def test_register_reattaches_completed_startup_without_replaying_or_changing_status(self):
        client, _connection, callbacks = self._client()
        connection = MagicMock()
        connection.call_sync.side_effect = [
            GLib.Variant("(o)", ("/org/gnome/SessionManager/Client99",)),
            GLib.Variant("(s)", (":1.42",)),
        ]
        client.connection = connection
        generation = hashlib.sha256(b":1.42").hexdigest()[:16]
        context = operations.OperationContext.create(generation, "startup", budget=1)
        document = {
            "schema_version": 1, "mode": "startup", "session_id": generation,
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
            client._run_direct_restore()
            client._poll_placement_progress()
        initialize.assert_not_called()
        claim.assert_not_called()
        update.assert_not_called()
        self.assertEqual(callbacks, [])
        self.assertEqual(status.read_bytes(), before)
        self.assertEqual(client._operation_context, context)
        self.assertTrue(client._startup_completed)
        connection.signal_subscribe.assert_called_once()

    def test_completed_startup_reattach_rejects_other_login_boot_and_untrusted_status(self):
        client, _connection, _callbacks = self._client()
        generation = "a" * 16
        context = operations.OperationContext.create(generation, "startup")
        document = {
            "schema_version": 1, "mode": "startup", "session_id": generation,
            "operation_context": context.to_dict(), "operation_id": context.operation_id,
            "operation_state": "completed",
        }
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        status = root / "login-hud-status.json"
        cases = [
            {"session_id": "b" * 16}, {"operation_state": "running"},
            {"operation_context": dict(context.to_dict(), boot_id="another-boot")},
            {"operation_id": "different-operation"}, {"operation_context": None},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                status.write_text(json.dumps(dict(document, **changes)))
                status.chmod(0o600)
                self.assertFalse(client._reattach_completed_startup(generation))
        status.write_text(json.dumps(document))
        status.chmod(0o644)
        self.assertFalse(client._reattach_completed_startup(generation))
        self.assertFalse(client._startup_completed)

    def test_idle_poll_is_passive(self):
        client, _connection, _callbacks = self._client()
        self.assertEqual(client.poll_progress(), GLib.SOURCE_CONTINUE)
