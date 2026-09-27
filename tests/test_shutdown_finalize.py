from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, call, patch

from workspace_state import shutdown_finalize


class ShutdownFinalizeTests(unittest.TestCase):
    def setUp(self):
        self.runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_directory.cleanup)
        environment = patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": self.runtime_directory.name,
                "WSCTL_SHUTDOWN_PROFILE_DIRS": "",
            },
            clear=False,
        )
        environment.start()
        self.addCleanup(environment.stop)
        stage_registration = patch(
            "workspace_state.shutdown_finalize.register_shutdown_stages",
            return_value=True,
        )
        stage_registration.start()
        self.addCleanup(stage_registration.stop)

    def test_worker_completion_is_private_and_bound_to_systemd_invocation(self):
        with patch.dict(
            os.environ, {"INVOCATION_ID": "c" * 32}, clear=False,
        ):
            root = Path(self.runtime_directory.name) / "workspace-state"
            root.mkdir(mode=0o700)
            (root / "login-generation").write_text("a" * 16 + "\n")
            self.assertTrue(
                shutdown_finalize.write_worker_complete_marker("b" * 32)
            )
            marker = root / "shutdown-worker-complete.json"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)
            payload = json.loads(marker.read_text())
            self.assertEqual(payload["operation_id"], "b" * 32)
            self.assertEqual(payload["login_generation"], "a" * 16)
            self.assertEqual(payload["invocation_id"], "c" * 32)
            self.assertEqual(payload["origin"], "preflight")

    @patch(
        "workspace_state.shutdown_finalize.consume_shutdown_cancel",
        return_value=False,
    )
    @patch("workspace_state.shutdown_finalize.set_overall")
    @patch("workspace_state.shutdown_finalize.update_stage")
    @patch(
        "workspace_state.shutdown_finalize.write_worker_complete_marker",
        return_value=True,
    )
    @patch(
        "workspace_state.shutdown_finalize._shutdown_context",
        return_value=("restart", "preflight", "login-1"),
    )
    @patch("workspace_state.shutdown_finalize._run_checkpoint")
    def test_transaction_only_saves_workspace_then_publishes_proof(
        self,
        checkpoint,
        context,
        marker,
        update,
        overall,
        _cancelled,
    ):
        checkpoint.side_effect = [False, False]
        with patch(
            "workspace_state.shutdown_finalize.signal.signal",
            side_effect=lambda *_args: None,
        ):
            self.assertEqual(shutdown_finalize.run_transaction("c" * 32), 0)

        self.assertCountEqual(checkpoint.call_args_list, [
            call(
                [
                    str(Path.home() / ".local/bin/wsctl-continuum-save"),
                    "--shutdown-operation",
                    "c" * 32,
                    "quiet",
                ],
                "tmux-resurrect save",
                "tmux-save",
                ANY,
            ),
            call(
                [
                    str(Path.home() / ".local/bin/wsctl"),
                    "save",
                    "--allow-partial",
                    "--shutdown-safe",
                ],
                "workspace save",
                "workspace-save",
                ANY,
                degraded_returncodes=frozenset({3}),
            ),
        ])
        context.assert_called_once_with("c" * 32)
        update.assert_called_once_with(
            "checkpoint-proof",
            "running",
            "Checkpoint saved; verifying the managed worker exit",
        )
        overall.assert_called_once()
        self.assertEqual(overall.call_args.args[0], "running")
        marker.assert_called_once_with(
            "c" * 32, action="restart", origin="preflight",
        )
        self.assertFalse(hasattr(shutdown_finalize, "_stop_drives"))
        self.assertFalse(hasattr(shutdown_finalize, "_stop_metadata_workers"))
        self.assertFalse(hasattr(shutdown_finalize, "_recover_cloud_systems"))

    @patch("workspace_state.shutdown_finalize.update_stage")
    @patch("workspace_state.shutdown_finalize.subprocess.Popen")
    def test_recoverable_checkpoint_exit_is_reported_as_degraded(self, popen, update):
        process = popen.return_value
        process.poll.return_value = 3
        process.returncode = 3

        degraded = shutdown_finalize._run_checkpoint(
            ["/test/checkpoint"],
            "workspace save",
            "workspace-save",
            shutdown_finalize.Cancellation("a" * 32),
            degraded_returncodes=frozenset({3}),
        )

        self.assertTrue(degraded)
        self.assertEqual(update.call_args.args[:3], (
            "workspace-save",
            "degraded",
            "Completed workspace save using safe fallback state",
        ))

    @patch("workspace_state.shutdown_finalize.finish")
    @patch("workspace_state.shutdown_finalize.cancel_shutdown")
    @patch("workspace_state.shutdown_finalize.write_worker_complete_marker")
    @patch(
        "workspace_state.shutdown_finalize._shutdown_context",
        return_value=("poweroff", "preflight", "login-1"),
    )
    @patch(
        "workspace_state.shutdown_finalize._run_checkpoint",
        side_effect=shutdown_finalize.ShutdownCancelled,
    )
    def test_cancelled_transaction_restores_profiles_and_publishes_no_proof(
        self, _checkpoint, _context, marker, cancel, finish,
    ):
        with patch(
            "workspace_state.shutdown_finalize.signal.signal",
            side_effect=lambda *_args: None,
        ):
            self.assertEqual(shutdown_finalize.run_transaction("d" * 32), 0)

        marker.assert_not_called()
        cancel.assert_called_once_with("Shutdown cancelled from the HUD")
        finish.assert_called_once_with(
            "Shutdown cancelled; prepared jobs were restored"
        )

    @patch("workspace_state.shutdown_finalize.append_diagnostic")
    @patch("workspace_state.shutdown_finalize.fail_active")
    @patch("workspace_state.shutdown_finalize.write_worker_complete_marker")
    @patch(
        "workspace_state.shutdown_finalize._shutdown_context",
        return_value=("poweroff", "preflight", "login-1"),
    )
    @patch(
        "workspace_state.shutdown_finalize._run_checkpoint",
        side_effect=RuntimeError("checkpoint failed"),
    )
    def test_checkpoint_failure_never_publishes_completion(
        self, _checkpoint, _context, marker, fail, diagnostic,
    ):
        with patch(
            "workspace_state.shutdown_finalize.signal.signal",
            side_effect=lambda *_args: None,
        ):
            self.assertEqual(shutdown_finalize.run_transaction("e" * 32), 1)

        marker.assert_not_called()
        fail.assert_called_once_with("checkpoint failed")
        diagnostic.assert_called_once_with("shutdown checkpoint", "checkpoint failed")

    def test_backend_only_gnome_origin_is_rejected(self):
        root = Path(self.runtime_directory.name) / "workspace-state"
        root.mkdir(mode=0o700)
        (root / "login-generation").write_text("a" * 16 + "\n")
        status = root / "login-hud-status.json"
        status.write_text(json.dumps({
            "schema_version": 1,
            "mode": "shutdown",
            "session_id": "a" * 16,
            "operation_id": "b" * 32,
            "shutdown_action": "poweroff",
            "shutdown_origin": "gnome",
        }))
        status.chmod(0o600)

        with self.assertRaisesRegex(RuntimeError, "not created after GNOME confirmation"):
            shutdown_finalize._shutdown_context("b" * 32)


if __name__ == "__main__":
    unittest.main()
