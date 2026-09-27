from __future__ import annotations

import os
import hashlib
import json
import signal
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

        self.assertEqual(callbacks, [])
        self.assertFalse(client._checkpoint_active)

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
        ) as cancel, patch(
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
        client, _connection, _callbacks = self._client()
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
            "workspace_state.gnome_session.time.monotonic",
            return_value=100.0,
        ) as clock:
            client._advance_shutdown_completion()
            self.assertFalse((root / "shutdown-prepared.json").exists())
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
            clock.return_value = 104.0
            client._advance_shutdown_completion()

        prepared = json.loads((root / "shutdown-prepared.json").read_text())
        self.assertEqual(prepared["operation_id"], operation_id)
        self.assertEqual(prepared["invocation_id"], invocation_id)
        self.assertEqual(prepared["schema_version"], 1)
        self.assertEqual(prepared["session_id"], "a" * 16)
        self.assertFalse((root / "shutdown-worker-complete.json").exists())
        inhibitor.release.assert_called_once_with()

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
