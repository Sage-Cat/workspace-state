from __future__ import annotations

import os
import hashlib
import json
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from gi.repository import GLib

from workspace_state.desktop import desktop_topology_signature
from workspace_state.gnome_session import (
    DESKTOP_SETTLE_SECONDS,
    GnomeSessionClient,
    ShutdownInhibitor,
)
from workspace_state.shutdown_profiles import ShutdownProfileError
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


class ShutdownInhibitorTests(unittest.TestCase):
    def test_logind_descriptor_is_held_until_explicit_release(self):
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
        inhibitor = ShutdownInhibitor(connection)

        inhibitor.acquire()

        self.assertTrue(inhibitor.active)
        call_args = connection.call_with_unix_fd_list_sync.call_args.args
        self.assertEqual(call_args[4].unpack(), (
            "shutdown",
            "workspace-state",
            "Waiting for the verified workspace shutdown HUD checkpoint",
            "block",
        ))
        os.fstat(held)

        inhibitor.release()

        self.assertFalse(inhibitor.active)
        with self.assertRaises(OSError):
            os.fstat(held)


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
        for name in (
            "capture_shutdown_profile_preflight",
            "claim_startup_hud", "fail_active", "initialize_login_status",
            "initialize_shutdown", "load_profiles", "update_stage",
        ):
            patcher = patch(f"workspace_state.gnome_session.{name}")
            patcher.start()
            self.addCleanup(patcher.stop)
        terminal = patch.object(GnomeSessionClient, "_attached_terminal_available", return_value=False)
        terminal.start()
        self.addCleanup(terminal.stop)
        portal = patch("workspace_state.portal_drain.verify_stopped")
        self.portal_verify = portal.start()
        self.addCleanup(portal.stop)

    @staticmethod
    def _portal_receipt(context):
        return {"schema_version": 1, "operation_context": context,
                "status": "succeeded", "settled": True, "units": [], "requests": {}, "errors": [],
                "not_running": True, "settlement_only": False}

    def _client(self):
        connection = FakeConnection()
        client = GnomeSessionClient(
            connection, FakeLoop(), Path("/tools"), shutdown_close_delay_ms=0,
        )
        client.client_path = "/org/gnome/SessionManager/Client99"
        callbacks = []

        def spawn(command, finished):
            callbacks.append((command, finished))

        patcher = patch.object(client, "_spawn", side_effect=spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        journal_patcher = patch.object(client, "_append_shutdown_journal")
        journal_patcher.start()
        self.addCleanup(journal_patcher.stop)
        return client, connection, callbacks

    def test_initial_query_before_native_confirmation_is_passive(self):
        client, connection, callbacks = self._client()

        client.handle_signal("QueryEndSession")
        self.assertEqual(callbacks, [])
        self.assertIsNone(client._shutdown_operation_id)
        self.assertEqual(
            connection.calls[-1][4].unpack(),
            (True, ""),
        )

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
                "monitor_recovery", "placement_lifecycle_v2",
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
                "monitor_recovery", "placement_lifecycle_v2",
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
                "monitor_recovery", "placement_lifecycle_v2",
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
                    "monitor_recovery", "placement_lifecycle_v2",
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

    def test_failed_preflight_service_start_never_authorizes_handoff(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16

        client._begin_checkpoint(
            operation_id="b" * 32,
            origin="preflight",
            action="poweroff",
        )
        callbacks.pop(0)[1](0)  # Startup barrier completed.
        callbacks.pop(0)[1](0)  # Graphical ownership preflight completed.
        callbacks.pop(0)[1](2)

        self.assertFalse(client._shutdown_handoff_accepted)
        self.assertFalse(client._checkpoint_active)

    def test_pre_hud_profile_capture_precedes_status_and_worker_start(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16
        events = []
        with patch(
            "workspace_state.gnome_session.load_profiles", return_value=[],
        ), patch(
            "workspace_state.gnome_session.capture_shutdown_profile_preflight",
            side_effect=lambda *_args, **_kwargs: events.append("capture"),
        ), patch(
            "workspace_state.gnome_session.initialize_shutdown",
            side_effect=lambda *_args, **_kwargs: events.append("status") or True,
        ):
            client._begin_checkpoint(
                operation_id="b" * 32,
                origin="preflight",
                action="poweroff",
            )
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-barrier"])
            callbacks.pop(0)[1](0)  # No capture until startup workers are joined.
            self.assertEqual(events, [])
            self.assertIn("--check", callbacks[0][0])
            callbacks.pop(0)[1](0)  # Read-only ownership check before profile mutations.

        self.assertEqual(events, ["capture", "status"])
        self.assertEqual(len(callbacks), 1)

    def test_pre_hud_profile_capture_failure_never_starts_worker(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16
        with patch(
            "workspace_state.gnome_session.load_profiles", return_value=[],
        ), patch(
            "workspace_state.gnome_session.capture_shutdown_profile_preflight",
            side_effect=ShutdownProfileError("placement unavailable"),
        ), patch(
            "workspace_state.gnome_session.initialize_shutdown", return_value=True,
        ):
            client._begin_checkpoint(
                operation_id="b" * 32,
                origin="preflight",
                action="poweroff",
            )
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-barrier"])
            callbacks.pop(0)[1](0)  # No capture until startup workers are joined.
            callbacks.pop(0)[1](0)

        self.assertEqual(callbacks, [])
        self.assertFalse(client._checkpoint_active)

    def test_barrier_companion_failure_is_not_reported_as_worker_stop_failure(self):
        client, _connection, callbacks = self._client()
        client._login_generation = 'a' * 16
        with patch('workspace_state.gnome_session.update_stage') as update:
            client._begin_checkpoint(operation_id='b' * 32, origin='preflight', action='poweroff')
            callbacks.pop(0)[1](3)
        detail = update.call_args.args[2]
        self.assertIn('Chrome', detail)
        self.assertNotIn('workers could not be stopped', detail)
        self.assertFalse(client._checkpoint_active)
        self.assertEqual(callbacks, [])

    def test_prepared_shutdown_is_released_without_starting_another_save(self):
        client, connection, callbacks = self._client()
        with patch.object(client, "_prepared_operation_is_current", return_value=True):
            client.handle_signal("QueryEndSession")

        self.assertEqual(callbacks, [])
        self.assertEqual(connection.calls[-1][4].unpack(), (True, ""))

    def test_completion_poll_releases_original_gnome_request(self):
        client, connection, _callbacks = self._client()
        client._shutdown_handoff_accepted = True
        client._end_session_pending = True
        with patch.object(client, "_prepared_operation_is_current", return_value=True):
            self.assertEqual(client.poll_cancel_request(), GLib.SOURCE_CONTINUE)

        self.assertTrue(client._shutdown_released)
        self.assertEqual(connection.calls[-1][4].unpack(), (True, ""))

    def test_cancel_after_prepared_handoff_is_consumed_without_recovery(self):
        client, _connection, _callbacks = self._client()
        client._shutdown_operation_id = "b" * 32
        with patch.object(
            client, "_advance_shutdown_completion",
        ), patch.object(
            client, "_prepared_operation_is_current", return_value=True,
        ), patch(
            "workspace_state.gnome_session.consume_shutdown_cancel",
            return_value=True,
        ), patch.object(client, "_cancel_verified_preflight") as cancel:
            self.assertEqual(client.poll_cancel_request(), GLib.SOURCE_CONTINUE)

        cancel.assert_called_once_with(
            "Shutdown cancelled after GNOME rejected or cancelled the final handoff"
        )

    def test_private_preflight_request_starts_worker_without_querying_gnome(self):
        client, connection, callbacks = self._client()
        client._login_generation = "a" * 16
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        request = root / "shutdown-request.json"
        request.write_text(
            '{"schema_version":1,"operation_id":"' + "b" * 32
            + '","session_id":"' + "a" * 16
            + '","action":"restart","requested_at":"now"}'
        )
        request.chmod(0o600)

        client.poll_cancel_request()
        self.assertEqual(callbacks[0][0], ["/tools/wsctl-startup-barrier"])
        callbacks.pop(0)[1](0)
        callbacks.pop(0)[1](0)

        self.assertTrue(request.exists())
        self.assertEqual(client._shutdown_origin, "preflight")
        self.assertEqual(client._shutdown_action, "restart")
        self.assertFalse(client._end_session_pending)
        self.assertEqual(connection.calls, [])
        self.assertEqual(
            callbacks[0][0],
            [
                "/usr/bin/systemctl", "--user", "start", "--no-block",
                f"wsctl-shutdown-finalize@{'b' * 32}.service",
            ],
        )

    def test_terminal_failure_request_is_retained_but_never_replayed(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16
        operation_id = "b" * 32
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        request = root / "shutdown-request.json"
        request.write_text(json.dumps({
            "schema_version": 1,
            "operation_id": operation_id,
            "session_id": "a" * 16,
            "action": "poweroff",
            "requested_at": "now",
        }))
        request.chmod(0o600)
        status = root / "login-hud-status.json"
        status.write_text(json.dumps({
            "schema_version": 1,
            "mode": "shutdown",
            "session_id": "a" * 16,
            "operation_id": operation_id,
            "shutdown_action": "poweroff",
            "shutdown_origin": "preflight",
            "cancelled": True,
            "overall_state": "failed",
        }))
        status.chmod(0o600)

        client.poll_cancel_request()

        self.assertTrue(request.exists())
        self.assertFalse(client._checkpoint_active)
        self.assertEqual(callbacks, [])

    def test_insecure_preflight_request_is_consumed_without_starting_worker(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        request = root / "shutdown-request.json"
        request.write_text(
            '{"schema_version":1,"operation_id":"' + "b" * 32
            + '","session_id":"' + "a" * 16
            + '","action":"poweroff"}'
        )
        request.chmod(0o644)

        client.poll_cancel_request()

        self.assertFalse(request.exists())
        self.assertEqual(callbacks, [])

    def test_preflight_handoff_watchdog_completes_bounded_recovery(self):
        client, _connection, callbacks = self._client()
        client._checkpoint_active = True
        client._shutdown_handoff_accepted = True
        client._shutdown_origin = "preflight"
        client._shutdown_operation_id = "c" * 32
        with patch.object(
            client, "_prepared_operation_is_current", return_value=True,
        ), patch.object(
            client, "_clear_shutdown_coordination",
        ), patch(
            "workspace_state.gnome_session.cancel_shutdown",
        ) as cancel, patch("workspace_state.gnome_session.finish_shutdown"), patch(
            "workspace_state.gnome_session.time.monotonic",
            side_effect=[10.0, 21.0],
        ):
            client._guard_preflight_handoff()
            client._guard_preflight_handoff()

        self.assertFalse(client._checkpoint_active)
        self.assertEqual(callbacks, [])
        self.assertEqual(cancel.call_args_list, [
            call(
                "GNOME did not accept the prepared shutdown handoff",
                recovery_pending=True,
            ),
            call(
                "GNOME did not accept the prepared shutdown handoff"
            ),
        ])

    def test_final_end_session_without_hud_transaction_follows_ubuntu(self):
        client, connection, callbacks = self._client()

        client.handle_signal("EndSession")

        self.assertEqual(callbacks, [])
        self.assertEqual(
            connection.calls[-1][4].unpack(),
            (True, ""),
        )

    def test_final_end_session_blocks_only_an_active_unprepared_preflight(self):
        client, connection, callbacks = self._client()
        client._checkpoint_active = True
        client._shutdown_operation_id = "b" * 32

        client.handle_signal("EndSession")

        self.assertEqual(callbacks, [])
        self.assertEqual(
            connection.calls[-1][4].unpack(),
            (False, "Workspace checkpoint has not been committed"),
        )

    def test_final_end_session_is_authorized_only_with_current_preparation(self):
        client, connection, callbacks = self._client()
        with patch.object(client, "_prepared_operation_is_current", return_value=True):
            client.handle_signal("EndSession")

        self.assertEqual(callbacks, [])
        self.assertEqual(connection.calls[-1][4].unpack(), (True, ""))

    def test_final_end_session_keeps_query_authorization_across_long_inhibitor_wait(self):
        client, connection, callbacks = self._client()
        client._shutdown_operation_id = "b" * 32
        client._prepared_operation_id = "b" * 32
        client._shutdown_released = True
        with patch.object(
            client, "_prepared_operation_is_current", return_value=False,
        ) as revalidate:
            client.handle_signal("EndSession")

        self.assertEqual(callbacks, [])
        revalidate.assert_not_called()
        self.assertEqual(connection.calls[-1][4].unpack(), (True, ""))

    def test_final_end_session_disarms_profile_rollback_at_point_of_no_return(self):
        client, connection, _callbacks = self._client()
        client._shutdown_operation_id = "b" * 32
        client._prepared_operation_id = "b" * 32
        client._shutdown_released = True
        with patch(
            "workspace_state.gnome_session.disarm_transaction", return_value=True,
        ) as disarm:
            client.handle_signal("EndSession")

        disarm.assert_called_once_with(
            "b" * 32,
            action=None,
            session_id=None,
        )
        self.assertEqual(connection.calls[-1][4].unpack(), (True, ""))

    def test_end_session_is_rejected_if_profile_rollback_cannot_be_disarmed(self):
        client, connection, _callbacks = self._client()
        client._shutdown_operation_id = "b" * 32
        client._prepared_operation_id = "b" * 32
        client._shutdown_released = True
        with patch(
            "workspace_state.gnome_session.disarm_transaction", return_value=False,
        ), patch.object(client, "_fail_shutdown_coordination") as fail:
            client.handle_signal("EndSession")

        self.assertEqual(
            connection.calls[-1][4].unpack(),
            (False, "Could not disarm shutdown rollback before GNOME EndSession"),
        )
        fail.assert_called_once()

    def test_gnome_cancel_stops_exact_managed_worker(self):
        client, _connection, callbacks = self._client()
        inhibitor = MagicMock()
        client._shutdown_inhibitor = inhibitor
        client._shutdown_operation_id = "d" * 32
        client._shutdown_unit = "wsctl-shutdown-finalize@test.service"
        client._checkpoint_active = True

        with patch(
            "workspace_state.gnome_session.cancel_shutdown"
        ) as cancel, patch(
            "workspace_state.gnome_session.transaction_exists", return_value=False,
        ):
            client.handle_signal("CancelEndSession")

        cancel.assert_called_once_with(
            "GNOME shutdown was cancelled; restoring prepared jobs",
            recovery_pending=True,
        )
        inhibitor.acquire.assert_called()
        self.assertEqual(callbacks[-1][0], [
            "/usr/bin/systemctl", "--user", "stop",
            "wsctl-shutdown-finalize@test.service",
        ])
        self.assertTrue(client._shutdown_recovery_pending)
        callbacks[-1][1](0)
        self.assertFalse(client._checkpoint_active)
        self.assertFalse(client._shutdown_recovery_pending)

    def test_worker_exit_render_and_three_seconds_are_required_before_prepared(self):
        client, _connection, callbacks = self._client()
        inhibitor = MagicMock()
        client._shutdown_inhibitor = inhibitor
        client._login_generation = "a" * 16
        operation_id = "b" * 32
        invocation_id = "c" * 32
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        (root / "login-hud-status.json").write_text(
            '{"schema_version":1,"mode":"shutdown","session_id":"' + "a" * 16
            + '","operation_id":"' + operation_id
            + '","shutdown_action":"poweroff","shutdown_origin":"preflight",'
            '"cancelled":false,"overall_state":"running"}'
        )
        (root / "login-hud-status.json").chmod(0o600)
        (root / "shutdown-worker-complete.json").write_text(
            '{"schema_version":1,"operation_id":"' + operation_id
            + '","login_generation":"' + "a" * 16
            + '","action":"poweroff","origin":"preflight","invocation_id":"'
            + invocation_id + f'","created_at":{time.time()}' + '}'
        )
        (root / "shutdown-worker-complete.json").chmod(0o600)
        from workspace_state import login_status
        completion = json.loads((root / "shutdown-worker-complete.json").read_text())
        login_status.initialize_shutdown("a" * 16, operation_id)
        context = json.loads(login_status.status_path().read_text())["operation_context"]
        client._operation_context = operations.OperationContext.from_dict(context)
        completion["operation_context"] = context
        (root / "shutdown-worker-complete.json").write_text(json.dumps(completion))
        (root / "shutdown-worker-complete.json").chmod(0o600)
        properties = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "Job": "", "ExecMainCode": "1",
            "ExecMainStatus": "0", "InvocationID": invocation_id,
            "ExecMainStartTimestampMonotonic": "10",
            "ExecMainExitTimestampMonotonic": "20",
        }
        with patch.object(
            client, "_shutdown_unit_properties", return_value=properties,
        ), patch(
            "workspace_state.shutdown_checkpoint_guard.arm_retry_protection",
        ), patch(
            "workspace_state.gnome_session.time.monotonic",
            return_value=100.0,
        ) as clock:
            client._advance_shutdown_completion()
            self.assertFalse((root / "shutdown-prepared.json").exists())
            self.assertEqual(callbacks, [])
            for filename in ("shutdown-hud-rendered.json", "shutdown-commit.json"):
                (root / filename).write_text(
                    '{"schema_version":1,"operation_id":"' + operation_id
                    + '","session_id":"' + "a" * 16 + '"}'
                )
                payload = json.loads((root / filename).read_text())
                payload["operation_context"] = context
                (root / filename).write_text(json.dumps(payload))
                (root / filename).chmod(0o600)
            client._advance_shutdown_completion()
            self.assertFalse((root / "shutdown-prepared.json").exists())
            self.assertEqual(callbacks, [])
            clock.return_value = 104.0
            (root / "shutdown-commit.json").unlink()
            client._advance_shutdown_completion()
            self.assertEqual(callbacks, [])
            from workspace_state.util import atomic_json
            atomic_json(root / "shutdown-commit.json", payload)
            client._advance_shutdown_completion()
            self.assertFalse((root / "shutdown-prepared.json").exists())
            inhibitor.release.assert_not_called()
            self.assertEqual(len(callbacks), 1)
            self.assertEqual(callbacks[0][0][:2], ["/usr/bin/python3", "-I"])
            atomic_json(client._graphical_drain_receipt_path(), {
                "schema_version": 1, "operation_context": context,
                "status": "succeeded", "settled": True, "units": [], "errors": [], "requests": {},
                "portal_required": True, "portal": self._portal_receipt(context),
                "deadline": 129.0, "finished_monotonic": 104.0, "finished_at": time.time(),
            })
            callbacks[0][1](0)

        prepared = json.loads((root / "shutdown-prepared.json").read_text())
        self.assertEqual(prepared["operation_id"], operation_id)
        self.assertEqual(prepared["invocation_id"], invocation_id)
        self.assertEqual(prepared["schema_version"], 1)
        self.assertEqual(prepared["session_id"], "a" * 16)
        self.assertFalse((root / "shutdown-worker-complete.json").exists())
        inhibitor.release.assert_called_once_with()

    def _drain_fixture(self):
        from workspace_state import login_status
        from workspace_state.util import atomic_json
        guard = patch("workspace_state.shutdown_checkpoint_guard.arm_retry_protection")
        guard.start()
        self.addCleanup(guard.stop)
        client, connection, callbacks = self._client()
        client._shutdown_inhibitor = MagicMock()
        client._login_generation = "a" * 16
        client._shutdown_operation_id = "b" * 32
        client._shutdown_unit = f"wsctl-shutdown-finalize@{client._shutdown_operation_id}.service"
        client._shutdown_origin = "preflight"
        client._shutdown_action = "poweroff"
        client._checkpoint_active = client._shutdown_handoff_accepted = True
        login_status.initialize_shutdown(client._login_generation, client._shutdown_operation_id)
        client._operation_context = operations.current()
        self.addCleanup(operations.bind, None)
        status = json.loads(login_status.status_path().read_text())
        status.update(operation_state="authorized", commit_authorized=True,
                      overall_state="ready", cancelled=False)
        atomic_json(login_status.status_path(), status)
        completion = {
            "schema_version": 1, "operation_id": client._shutdown_operation_id,
            "login_generation": client._login_generation, "action": "poweroff",
            "origin": "preflight", "invocation_id": "c" * 32,
            "created_at": time.time(), "operation_context": client._operation_context.to_dict(),
        }
        client._verified_worker_completion = completion
        atomic_json(login_status.shutdown_worker_complete_path(), completion)
        for path in (login_status.shutdown_rendered_path(), login_status.shutdown_commit_path()):
            atomic_json(path, {"schema_version": 1, "operation_id": client._shutdown_operation_id,
                               "session_id": client._login_generation,
                               "operation_context": client._operation_context.to_dict()})
        properties = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "Job": "", "ExecMainCode": "1", "ExecMainStatus": "0",
            "InvocationID": completion["invocation_id"], "ExecMainStartTimestampMonotonic": "10",
            "ExecMainExitTimestampMonotonic": "20",
        }
        patcher = patch.object(client, "_shutdown_unit_properties", return_value=properties)
        patcher.start()
        self.addCleanup(patcher.stop)
        return client, connection, callbacks, completion

    @staticmethod
    def _drain_receipt(client, *, settled=True, status="succeeded", **extra):
        from workspace_state.util import atomic_json
        atomic_json(client._graphical_drain_receipt_path(), {
            "schema_version": 1, "operation_context": client._operation_context.to_dict(),
            "settled": settled, "status": status, "units": [], "errors": [], "requests": {},
            "portal_required": True, "portal": GnomeSessionClientTests._portal_receipt(client._operation_context.to_dict()),
            "deadline": client._graphical_drain_intent["deadline"],
            "finished_at": time.time(), "finished_monotonic": time.monotonic(), **extra,
        })

    def test_invalid_checkpoint_binding_cannot_start_application_drain(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        with patch('workspace_state.shutdown_checkpoint_guard.arm_retry_protection',
                   side_effect=RuntimeError('checkpoint changed before drain')):
            with self.assertRaisesRegex(RuntimeError, 'checkpoint changed'):
                client._begin_graphical_drain(completion)
        self.assertEqual(callbacks, [])
        self.assertFalse(client._graphical_drain_path().exists())
        self.assertFalse(client._graphical_drain_started)

    def test_cancel_during_checkpoint_guard_read_starts_only_settlement(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        with patch('workspace_state.gnome_session.consume_shutdown_cancel', return_value=True):
            client._begin_graphical_drain(completion)
        self.assertEqual(client._graphical_drain_abort[0], 'cancel')
        self.assertEqual(len(callbacks), 1)
        self.assertIn('--settle-only', callbacks[0][0])

    def test_drain_is_async_exactly_once_and_blocks_native_session_end_until_completion(self):
        client, connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self.assertEqual(len(callbacks), 1)
        self.assertIn("graphical_drain.py", callbacks[0][0][2])
        for _ in range(3):
            client.poll_cancel_request()
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(client._prepared_shutdown_path().exists())
        client.handle_signal("EndSession")
        self.assertFalse(connection.calls[-1][4].unpack()[0])
        client._shutdown_inhibitor.release.assert_not_called()
        self._drain_receipt(client)
        callbacks[0][1](0)
        callbacks[0][1](0)
        self.assertTrue(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_called_once_with()
        from workspace_state.util import data_home
        prepared = json.loads(client._prepared_shutdown_path().read_text())
        self.assertTrue(prepared["graphical_drain_completed"])
        self.assertEqual(json.loads((data_home() / prepared["graphical_drain_receipt"]).read_text()),
                         json.loads(client._graphical_drain_receipt_path().read_text()))
        self.assertEqual(json.loads((data_home() / f"shutdown-prepared-{client._shutdown_operation_id}.json").read_text()),
                         prepared)

    def test_cancel_during_drain_retains_ownership_until_app_stops_then_recovers_only_jobs(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        client._cancel_verified_preflight("User cancelled")
        client._reset_shutdown_attempt()
        self.assertTrue(client._graphical_drain_pending)
        self.assertTrue(client._shutdown_recovery_pending)
        with self.assertRaisesRegex(RuntimeError, "still owns"):
            client._begin_checkpoint(operation_id="d" * 32)
        self.assertEqual(len(callbacks), 1, "profile jobs cannot restart while app stops are active")
        self._drain_receipt(client)
        callbacks[0][1](0)
        self.assertEqual(callbacks[1][0][-2:], ["stop", client._shutdown_unit])
        self.assertTrue(client._shutdown_recovery_pending)
        with patch("workspace_state.gnome_session.transaction_exists", return_value=False):
            callbacks[1][1](0)
        self.assertFalse(client._checkpoint_active)
        self.assertFalse(client._prepared_shutdown_path().exists())
        status = json.loads((Path(self.runtime_directory.name) / "workspace-state/login-hud-status.json").read_text())
        self.assertIn("Closed applications have not been restored", status["overall_message"])
        client._shutdown_inhibitor.release.assert_not_called()

    def test_unsettled_helper_timeout_is_rejoined_read_only_before_recovery(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client, settled=False, status="failed")
        callbacks[0][1](75)
        self.assertTrue(client._graphical_drain_pending)
        self.assertTrue(client._shutdown_recovery_pending)
        client._graphical_drain_retry_at = 0
        client._poll_graphical_drain()
        self.assertIn("--settle-only", callbacks[1][0])
        self.assertEqual(callbacks[0][0][4], callbacks[1][0][4])
        self._drain_receipt(client, settled=True, status="failed")
        callbacks[1][1](1)
        self.assertFalse(client._graphical_drain_pending)
        self.assertTrue(client._shutdown_recovery_pending)
        self.assertEqual(callbacks[2][0][-2:], ["stop", client._shutdown_unit])
        client._shutdown_inhibitor.release.assert_not_called()

    def test_stale_epoch_or_context_callback_never_publishes_or_releases(self):
        for mutation in ("epoch", "context"):
            with self.subTest(mutation=mutation):
                client, _connection, callbacks, completion = self._drain_fixture()
                client._begin_graphical_drain(completion)
                self._drain_receipt(client)
                if mutation == "epoch":
                    client._shutdown_epoch += 1
                else:
                    client._operation_context = operations.OperationContext.create("new-login", "shutdown")
                callbacks[0][1](0)
                self.assertFalse(client._prepared_shutdown_path().exists())
                client._shutdown_inhibitor.release.assert_not_called()

    def test_delayed_success_after_drain_deadline_fails_without_publishing(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        deadline = client._graphical_drain_intent["deadline"]
        with patch("workspace_state.gnome_session.time.monotonic", return_value=deadline + 1), patch.object(
            client, "_fail_shutdown_coordination",
        ) as fail:
            callbacks[0][1](0)
        self.assertIn("after its authorization deadline", fail.call_args.args[0])
        self.assertFalse(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_not_called()

    def test_drain_success_rechecks_worker_and_current_authorization(self):
        from workspace_state import login_status
        from workspace_state.util import atomic_json
        for change in ("worker", "status", "commit", "receipt"):
            with self.subTest(change=change):
                client, _connection, callbacks, completion = self._drain_fixture()
                client._begin_graphical_drain(completion)
                self._drain_receipt(client)
                if change == "worker":
                    atomic_json(login_status.shutdown_worker_complete_path(), {**completion, "invocation_id": "d" * 32})
                elif change == "status":
                    status = json.loads(login_status.status_path().read_text())
                    status["commit_authorized"] = False
                    atomic_json(login_status.status_path(), status)
                elif change == "commit":
                    login_status.shutdown_commit_path().unlink()
                else:
                    client._graphical_drain_receipt_path().unlink()
                callbacks[0][1](0)
                self.assertFalse(client._prepared_shutdown_path().exists())
                client._shutdown_inhibitor.release.assert_not_called()

    def test_drain_rejoin_preserves_deadline_across_coordinator_restart(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        deadline = client._graphical_drain_intent["deadline"]
        restarted, _connection, joined = self._client()
        restarted._login_generation = client._login_generation
        self.assertTrue(restarted._reattach_shutdown_transaction(client._login_generation))
        self.assertTrue(restarted._graphical_drain_pending)
        self.assertEqual(restarted._graphical_drain_intent["deadline"], deadline)
        self.assertEqual(joined[0][0], callbacks[0][0])
        self.assertFalse(restarted._prepared_shutdown_path().exists())

    def test_cancelled_drain_restart_settles_without_replaying_application_stops(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        client._cancel_verified_preflight("User cancelled")
        restarted, _connection, joined = self._client()
        restarted._login_generation = client._login_generation
        with patch("workspace_state.gnome_session.transaction_exists", return_value=False):
            self.assertTrue(restarted._reattach_shutdown_transaction(client._login_generation))
        self.assertTrue(restarted._shutdown_recovery_pending)
        self.assertTrue(restarted._graphical_drain_pending)
        self.assertIn("--settle-only", joined[0][0])
        self.assertEqual(len(joined), 1)

    def test_restart_after_published_handoff_does_not_redrain_consumed_worker(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        callbacks[0][1](0)
        restarted, _connection, joined = self._client()
        restarted._login_generation = client._login_generation
        restarted._shutdown_inhibitor = MagicMock()
        with patch.object(restarted, "_prepared_operation_is_current", return_value=True):
            self.assertTrue(restarted._reattach_shutdown_transaction(client._login_generation))
        self.assertEqual(joined, [])
        restarted._shutdown_inhibitor.release.assert_called_once_with()

    def test_busy_helper_rejoins_without_duplicate_stop_or_renewed_deadline(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        deadline = client._graphical_drain_intent["deadline"]
        callbacks[0][1](75)  # Lock holder has not published its ledger yet.
        self.assertTrue(client._graphical_drain_pending)
        self.assertIsNone(client._graphical_drain_abort)
        client._graphical_drain_retry_at = 0
        client._poll_graphical_drain()
        self.assertEqual(callbacks[0][0], callbacks[1][0])
        self.assertEqual(client._graphical_drain_intent["deadline"], deadline)
        self._drain_receipt(client)
        callbacks[1][1](0)
        client._shutdown_inhibitor.release.assert_called_once_with()

    def test_missing_helper_failure_keeps_ownership_until_no_work_settlement(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        callbacks[0][1](127)
        self.assertTrue(client._shutdown_recovery_pending)
        self.assertTrue(client._graphical_drain_pending)
        client._graphical_drain_retry_at = 0
        client._poll_graphical_drain()
        self.assertIn("--settle-only", callbacks[1][0])
        callbacks[1][1](0)
        self.assertFalse(client._graphical_drain_pending)
        self.assertEqual(callbacks[2][0][-2:], ["stop", client._shutdown_unit])
        client._shutdown_inhibitor.release.assert_not_called()

    def test_failed_settlement_retries_are_bounded_and_block_survives_restart(self):
        from workspace_state import login_status
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        callbacks[0][1](127)
        for index in range(1, 4):
            client._graphical_drain_retry_at = 0
            client._poll_graphical_drain()
            self.assertIn("--settle-only", callbacks[index][0])
            callbacks[index][1](127)
        client._graphical_drain_retry_at = 0
        for _ in range(10):
            client._poll_graphical_drain()
            client._reset_shutdown_attempt()
        self.assertEqual(len(callbacks), 4)
        self.assertTrue(client._graphical_drain_blocked)
        self.assertTrue(client._graphical_drain_pending)
        self.assertTrue(client._shutdown_recovery_pending)
        status = json.loads(login_status.status_path().read_text())
        self.assertEqual(status["operation_state"], "recovery-failed")
        self.assertIn("manual recovery", status["overall_message"])
        with self.assertRaisesRegex(RuntimeError, "still owns"):
            client._begin_checkpoint(operation_id="d" * 32)
        client._shutdown_inhibitor.release.assert_not_called()
        self.assertFalse(client._prepared_shutdown_path().exists())
        restarted, _connection, joined = self._client()
        restarted._login_generation = client._login_generation
        self.assertTrue(restarted._reattach_shutdown_transaction(client._login_generation))
        self.assertTrue(restarted._graphical_drain_blocked)
        self.assertTrue(restarted._shutdown_recovery_pending)
        self.assertEqual(joined, [])

    def test_unsupported_graphical_units_fail_before_profile_mutation(self):
        client, _connection, callbacks = self._client()
        client._login_generation = "a" * 16
        with patch("workspace_state.gnome_session.capture_shutdown_profile_preflight") as capture, patch(
            "workspace_state.gnome_session.update_stage",
        ) as stage:
            client._begin_checkpoint(operation_id="b" * 32)
            callbacks.pop(0)[1](0)
            command, checked = callbacks.pop(0)
            self.assertIn("--check", command)
            self.assertNotIn("--receipt", command)
            self.assertEqual(command[:2], ["/usr/bin/python3", "-I"])
            self.assertEqual(command[-2:], ["--timeout", "5"])
            capture.assert_not_called()
            checked(1)
        capture.assert_not_called()
        self.assertEqual(callbacks, [])
        self.assertIn("immutable application helpers", stage.call_args.args[2])
        self.assertIn("exceeded 5 seconds", stage.call_args.args[2])
        self.assertFalse(client._checkpoint_active)

    def test_stale_graphical_preflight_callback_cannot_start_checkpoint(self):
        client, _connection, callbacks = self._client()
        with patch("workspace_state.gnome_session.capture_shutdown_profile_preflight") as capture:
            client._begin_checkpoint(operation_id="b" * 32)
            callbacks.pop(0)[1](0)
            checked = callbacks.pop(0)[1]
            client._reset_shutdown_attempt()
            checked(0)
        capture.assert_not_called()
        self.assertEqual(callbacks, [])

    def test_incomplete_or_failed_drain_receipt_never_publishes_final_marker(self):
        for extra in ({"errors": ["native main did not exit"]}, {"finished_at": None},
                      {"finished_monotonic": float("inf")}, {"requests": None},
                      {"portal": None}, {"portal_required": False}):
            with self.subTest(extra=extra):
                client, _connection, callbacks, completion = self._drain_fixture()
                client._begin_graphical_drain(completion)
                self._drain_receipt(client, **extra)
                callbacks[0][1](0)
                self.assertFalse(client._prepared_shutdown_path().exists())
                client._shutdown_inhibitor.release.assert_not_called()

    def test_reactivated_portal_after_archive_prevents_native_handoff(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        self.portal_verify.side_effect = [None, ValueError("document portal reactivated")]
        callbacks[0][1](0)
        self.assertFalse(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_not_called()

    def test_stalled_fresh_portal_verification_cannot_exceed_local_drain_deadline(self):
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        deadline = client._graphical_drain_intent["deadline"]
        clock = [deadline - 1]
        def verification(*_args):
            if self.portal_verify.call_count == 2:
                clock[0] = deadline + 1
        self.portal_verify.side_effect = verification
        with patch("workspace_state.gnome_session.time.monotonic", side_effect=lambda: clock[0]), patch.object(
            client, "_fail_shutdown_coordination",
        ) as fail:
            callbacks[0][1](0)
        self.assertEqual(self.portal_verify.call_count, 2)
        self.assertIn("verification exceeded the application drain deadline", fail.call_args.args[0])
        self.assertFalse(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_not_called()

    def test_durable_drain_archive_failure_withholds_handoff(self):
        from workspace_state.util import atomic_json, data_home
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        durable = data_home() / client._graphical_drain_receipt_path().name
        def write(path, value):
            if path == durable:
                raise OSError("archive disk unavailable")
            atomic_json(path, value)
        with patch("workspace_state.gnome_session.atomic_json", side_effect=write):
            callbacks[0][1](0)
        self.assertFalse(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_not_called()

    def test_durable_prepared_archive_failure_withholds_runtime_handoff(self):
        from workspace_state.util import atomic_json, data_home
        client, _connection, callbacks, completion = self._drain_fixture()
        client._begin_graphical_drain(completion)
        self._drain_receipt(client)
        durable = data_home() / f"shutdown-prepared-{client._shutdown_operation_id}.json"
        def write(path, value):
            if path == durable:
                raise OSError("handoff archive disk unavailable")
            atomic_json(path, value)
        with patch("workspace_state.gnome_session.atomic_json", side_effect=write):
            callbacks[0][1](0)
        self.assertFalse(client._prepared_shutdown_path().exists())
        client._shutdown_inhibitor.release.assert_not_called()

    def test_durable_drain_archive_rejects_symlink_and_late_cancel(self):
        from workspace_state import login_status
        from workspace_state.util import atomic_json, data_home
        for variant in ("symlink", "cancel_during_archive"):
            with self.subTest(variant=variant):
                client, _connection, callbacks, completion = self._drain_fixture()
                client._begin_graphical_drain(completion)
                self._drain_receipt(client)
                durable = data_home() / client._graphical_drain_receipt_path().name
                durable.unlink(missing_ok=True)
                protected = Path(self.runtime_directory.name) / "protected"
                protected.write_text("preserved")
                if variant == "symlink":
                    durable.parent.mkdir(parents=True, exist_ok=True)
                    durable.symlink_to(protected)
                def write(path, value):
                    atomic_json(path, value)
                    if path == durable:
                        status = json.loads(login_status.status_path().read_text())
                        status.update(cancelled=True, commit_authorized=False)
                        atomic_json(login_status.status_path(), status)
                with patch("workspace_state.gnome_session.atomic_json", side_effect=write):
                    callbacks[0][1](0)
                self.assertFalse(client._prepared_shutdown_path().exists())
                self.assertEqual(protected.read_text(), "preserved")
                client._shutdown_inhibitor.release.assert_not_called()
                durable.unlink(missing_ok=True)

    def test_unit_which_never_started_cannot_authorize_shutdown(self):
        properties = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "Job": "", "ExecMainCode": "0",
            "ExecMainStatus": "0", "InvocationID": "",
            "ExecMainStartTimestampMonotonic": "0",
            "ExecMainExitTimestampMonotonic": "0",
        }

        self.assertFalse(GnomeSessionClient._unit_finished_successfully(
            properties, "c" * 32,
        ))

    def test_successful_remain_after_exit_unit_authorizes_shutdown(self):
        properties = {
            "LoadState": "loaded", "ActiveState": "active", "SubState": "exited",
            "Result": "success", "Job": "", "ExecMainCode": "1",
            "ExecMainStatus": "0", "InvocationID": "c" * 32,
            "ExecMainStartTimestampMonotonic": "10",
            "ExecMainExitTimestampMonotonic": "20",
        }

        self.assertTrue(GnomeSessionClient._unit_finished_successfully(
            properties, "c" * 32,
        ))

    def test_running_remain_after_exit_unit_waits_for_worker(self):
        properties = {
            "LoadState": "loaded", "ActiveState": "active", "SubState": "running",
            "Result": "success", "Job": "", "ExecMainCode": "0",
            "ExecMainStatus": "0", "InvocationID": "c" * 32,
            "ExecMainStartTimestampMonotonic": "10",
            "ExecMainExitTimestampMonotonic": "0",
        }

        self.assertIsNone(GnomeSessionClient._unit_finished_successfully(
            properties, "c" * 32,
        ))

    def test_wrong_systemd_invocation_cannot_authorize_shutdown(self):
        properties = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "Job": "", "ExecMainCode": "1",
            "ExecMainStatus": "0", "InvocationID": "d" * 32,
            "ExecMainStartTimestampMonotonic": "10",
            "ExecMainExitTimestampMonotonic": "20",
        }

        self.assertFalse(GnomeSessionClient._unit_finished_successfully(
            properties, "c" * 32,
        ))

    def test_coordinator_restart_reattaches_shutdown_without_startup_restore(self):
        client, _connection, callbacks = self._client()
        session_id = "a" * 16
        operation_id = "b" * 32
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        status = root / "login-hud-status.json"
        status.write_text(json.dumps({
            "schema_version": 1,
            "mode": "shutdown",
            "session_id": session_id,
            "operation_id": operation_id,
            "shutdown_action": "restart",
            "shutdown_origin": "preflight",
            "overall_state": "running",
            "cancelled": False,
        }))
        status.chmod(0o600)

        self.assertTrue(client._reattach_shutdown_transaction(session_id))

        self.assertTrue(client._startup_blocked_by_shutdown)
        self.assertTrue(client._checkpoint_active)
        self.assertEqual(client._shutdown_operation_id, operation_id)
        self.assertEqual(client._shutdown_action, "restart")
        self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_REMOVE)
        self.assertEqual(callbacks, [])

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
        self.assertFalse(client._startup_blocked_by_shutdown)
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

    def test_coordinator_restart_resumes_an_interrupted_profile_rollback(self):
        client, _connection, callbacks = self._client()
        status = {
            "operation_id": "b" * 32,
            "cancelled": True,
            "overall_state": "running",
        }
        with patch.object(
            client, "_read_current_shutdown_status", return_value=status,
        ), patch(
            "workspace_state.gnome_session.transaction_exists", return_value=True,
        ), patch(
            "workspace_state.gnome_session.cancel_shutdown",
        ) as cancel:
            self.assertTrue(client._reattach_shutdown_transaction("a" * 16))

        cancel.assert_called_once_with(
            "Resuming interrupted shutdown cancellation recovery",
            recovery_pending=True,
        )
        self.assertEqual(callbacks[-1][0], [
            "/usr/bin/systemctl", "--user", "stop",
            f"wsctl-shutdown-finalize@{'b' * 32}.service",
        ])

    def test_register_after_legacy_shutdown_failure_does_not_replay_login(self):
        client, _connection, callbacks = self._client()
        connection = MagicMock()
        connection.call_sync.side_effect = [
            GLib.Variant("(o)", ("/org/gnome/SessionManager/Client99",)),
            GLib.Variant("(s)", (":1.42",)),
        ]
        client.connection = connection
        generation = hashlib.sha256(b":1.42").hexdigest()[:16]
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        status = root / "login-hud-status.json"
        status.write_text(json.dumps({
            "schema_version": 1, "mode": "shutdown", "session_id": generation,
            "operation_id": "b" * 32, "shutdown_action": "poweroff",
            "shutdown_origin": "preflight", "overall_state": "failed",
            "cancelled": True,
        }))
        status.chmod(0o600)
        before = status.read_bytes()
        with patch("workspace_state.gnome_session.transaction_exists", return_value=False), \
             patch("workspace_state.gnome_session.initialize_login_status") as initialize, \
             patch("workspace_state.gnome_session.claim_startup_hud") as claim:
            client.register()
            self.assertEqual(client.wait_for_graphical_environment(), GLib.SOURCE_REMOVE)
            client.start_restore()
            client._run_direct_restore()
        initialize.assert_not_called()
        claim.assert_not_called()
        self.assertEqual(callbacks, [])
        self.assertEqual(status.read_bytes(), before)
        self.assertTrue(client._startup_blocked_by_shutdown)
        self.assertFalse(client._checkpoint_active)
        self.assertIsNone(client._shutdown_operation_id)
        self.assertIsNone(client._operation_context)
        self.assertEqual(client._login_generation, generation)
        self.assertTrue((root / "coordinator-build.json").exists())

    def test_worker_which_never_starts_fails_without_unneeded_recovery(self):
        client, _connection, _callbacks = self._client()
        client._checkpoint_active = True
        client._shutdown_handoff_accepted = True
        client._shutdown_unit = "wsctl-shutdown-finalize@test.service"
        client._shutdown_start_deadline = 1.0
        properties = {
            "LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead",
            "Result": "success", "ExecMainStartTimestampMonotonic": "0",
        }
        with patch.object(
            client, "_shutdown_unit_properties", return_value=properties,
        ), patch.object(
            client, "_fail_shutdown_coordination",
        ) as fail, patch(
            "workspace_state.gnome_session.time.monotonic", return_value=2.0,
        ):
            client._check_shutdown_worker_without_completion()

        fail.assert_called_once_with(
            "wsctl-shutdown-finalize@test.service did not start",
            recovery_required=False,
        )

    def test_consumed_worker_marker_is_safe_after_prepared_promotion(self):
        client, _connection, _callbacks = self._client()
        client._checkpoint_active = True
        client._shutdown_handoff_accepted = True
        client._shutdown_unit = "wsctl-shutdown-finalize@test.service"
        with patch.object(
            client, "_prepared_operation_is_current", return_value=True,
        ), patch.object(
            client, "_shutdown_unit_properties",
        ) as properties:
            client._check_shutdown_worker_without_completion()

        properties.assert_not_called()

    def test_idle_completion_poll_is_passive(self):
        client, _connection, _callbacks = self._client()
        self.assertEqual(client.poll_cancel_request(), GLib.SOURCE_CONTINUE)

    def test_stale_or_wrong_login_preparation_is_not_approved(self):
        client, _connection, _callbacks = self._client()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            root = Path(directory) / "workspace-state"
            root.mkdir(mode=0o700)
            marker = root / "shutdown-prepared.json"
            marker.write_text('{"operation_id":"' + "a" * 32 + '","login_generation":"other","created_at":1}')
            marker.chmod(0o600)
            client._login_generation = "b" * 16
            self.assertFalse(client._prepared_operation_is_current())


if __name__ == "__main__":
    unittest.main()
