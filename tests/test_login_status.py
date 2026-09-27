from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import login_status


class LoginStatusTests(unittest.TestCase):
    def test_startup_finalization_cannot_mutate_shutdown_even_with_current_authority(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-finalization-test", "b" * 32)
            before = login_status.status_path().read_bytes()
            with patch.object(login_status, "_refresh_provider_placements") as refresh:
                self.assertFalse(login_status.finish())
            refresh.assert_not_called()
            self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_shutdown_finalization_preserves_message_and_stage_truth(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-finalization-test", "b" * 32)
            login_status.register_shutdown_stages([("profile-test", "Profile test")])
            login_status.update_stage("profile-test", "running", "Preparing")
            with patch.object(login_status, "_refresh_provider_placements") as refresh:
                self.assertTrue(login_status.finish_shutdown("Power-off preparation complete"))
            refresh.assert_not_called()
            status = json.loads(login_status.status_path().read_text())
            self.assertEqual(status["overall_state"], "running")
            self.assertEqual(status["overall_message"], "Power-off preparation complete")
            for stage in status["stages"]:
                login_status.update_stage(stage["id"], "ready", "Verified")
            self.assertTrue(login_status.finish_shutdown("Power-off preparation complete"))
            status = json.loads(login_status.status_path().read_text())
            self.assertEqual(status["overall_state"], "ready")
            self.assertEqual(status["overall_message"], "Power-off preparation complete")

    def test_shutdown_finalization_cannot_mutate_startup(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("login-finalization-test")
            before = login_status.status_path().read_bytes()
            self.assertFalse(login_status.finish_shutdown("Shutdown handoff authorized"))
            self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_startup_hud_claim_survives_only_a_mid_startup_service_restart(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "XDG_RUNTIME_DIR": f"{directory}/runtime",
                "XDG_STATE_HOME": f"{directory}/state",
            },
            clear=False,
        ), patch("workspace_state.login_status._boot_id", return_value="boot-a"):
            self.assertTrue(login_status.claim_startup_hud("session-a"))
            self.assertTrue(login_status.claim_startup_hud("session-a"))
            login_status.initialize("session-a", show_startup_hud=True)
            self.assertTrue(login_status.claim_startup_hud("session-a"))
            for identifier, _label in login_status.DEFAULT_STAGES:
                login_status.update_stage(
                    identifier, "ready", "Ready", current=1, total=1,
                )
            login_status.finish()
            self.assertFalse(login_status.claim_startup_hud("session-a"))
            self.assertFalse(login_status.claim_startup_hud("session-b"))

            claim = json.loads(login_status.boot_claim_path().read_text())
            self.assertEqual(claim["boot_id"], "boot-a")
            self.assertEqual(claim["session_id"], "session-a")
            self.assertEqual(login_status.boot_claim_path().stat().st_mode & 0o777, 0o600)

            with patch("workspace_state.login_status._boot_id", return_value="boot-b"):
                self.assertTrue(login_status.claim_startup_hud("session-c"))

    def test_hidden_same_boot_status_stays_hidden_after_recovery_update(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"XDG_RUNTIME_DIR": directory},
            clear=False,
        ):
            runtime_root = Path(directory) / "workspace-state"
            runtime_root.mkdir(parents=True)
            (runtime_root / "login-generation").write_text("later-login\n")
            self.assertTrue(
                login_status.initialize(
                    "later-login",
                    show_startup_hud=False,
                )
            )
            status = json.loads(login_status.status_path().read_text())
            self.assertFalse(status["show_startup_hud"])

            # A damaged status is rebuilt from the private per-login policy;
            # a later stage update must not accidentally make the HUD visible.
            login_status.status_path().write_text("not-json")
            login_status.update_stage("gnome", "ready", "Wayland ready")
            recovered = json.loads(login_status.status_path().read_text())
            self.assertEqual(recovered["session_id"], "later-login")
            self.assertFalse(recovered["show_startup_hud"])

    def test_boot_claim_failure_never_breaks_login_restore(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"XDG_STATE_HOME": f"{directory}/not-a-directory"},
            clear=False,
        ), patch("workspace_state.login_status._boot_id", return_value="boot-a"):
            Path(os.environ["XDG_STATE_HOME"]).write_text("occupied")
            self.assertFalse(login_status.claim_startup_hud("session-a"))

    def test_status_is_atomic_private_and_derived_from_stage_truth(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            self.assertTrue(login_status.initialize("login-42"))
            for identifier, _label in login_status.DEFAULT_STAGES:
                self.assertTrue(
                    login_status.update_stage(
                        identifier, "ready", "Ready", current=1, total=1,
                    )
                )
            self.assertTrue(login_status.finish())

            status_file = Path(directory) / "workspace-state/login-hud-status.json"
            status = json.loads(status_file.read_text())
            self.assertEqual(status["session_id"], "login-42")
            self.assertTrue(status["show_startup_hud"])
            self.assertEqual(status["overall_state"], "ready")
            self.assertEqual(status_file.stat().st_mode & 0o777, 0o600)
            self.assertFalse(list(status_file.parent.glob(".*.tmp")))

            login_status.update_stage(
                "pdrive", "failed", "Mount failed", error="connection refused",
            )
            status = json.loads(status_file.read_text())
            self.assertEqual(status["overall_state"], "failed")
            self.assertIn("connection refused", login_status.log_path().read_text())

    def test_startup_stages_publish_combined_jobs_and_bounded_events(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("grouped-login")
            for index in range(login_status.MAX_STAGE_EVENTS + 5):
                login_status.update_stage("gdrive", "running", f"Mount poll {index}")
            # An identical poll must not create a duplicate event.
            login_status.update_stage(
                "gdrive", "running",
                f"Mount poll {login_status.MAX_STAGE_EVENTS + 4}",
            )

            status = json.loads(login_status.status_path().read_text())
            grouped = {
                stage["id"]: (stage.get("group_id"), stage.get("group_label"))
                for stage in status["stages"]
            }
            self.assertEqual(grouped["gnome"], (
                "desktop-readiness", "GNOME Wayland and workspace readiness",
            ))
            self.assertEqual(grouped["displays"], grouped["gnome"])
            self.assertEqual(grouped["tmux"], (
                "terminal-restore", "Alacritty and tmux restoration",
            ))
            self.assertEqual(grouped["terminals"], grouped["tmux"])
            cloud = [
                stage for stage in status["stages"]
                if stage.get("group_id") == "cloud-drives"
            ]
            self.assertEqual(
                [stage["id"] for stage in cloud],
                ["gdrive", "nextcloud", "pdrive", "warmup"],
            )
            self.assertTrue(all(
                stage["group_label"] == "Cloud drives and metadata"
                for stage in cloud
            ))
            gdrive = next(stage for stage in cloud if stage["id"] == "gdrive")
            self.assertEqual(len(gdrive["events"]), login_status.MAX_STAGE_EVENTS)
            self.assertEqual(gdrive["events"][-1]["message"], "Mount poll 36")

    def test_same_session_reattaches_without_erasing_progress(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("same-login")
            login_status.update_stage("gnome", "ready", "Wayland ready")
            login_status.initialize("same-login")

            status = json.loads(login_status.status_path().read_text())
            gnome = next(stage for stage in status["stages"] if stage["id"] == "gnome")
            self.assertEqual(gnome["state"], "ready")
            self.assertIn("publisher reattached", login_status.log_path().read_text())

    def test_same_session_reattach_adds_new_default_stage_without_losing_progress(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("same-login")
            login_status.update_stage("gnome", "ready", "Wayland ready")
            status = json.loads(login_status.status_path().read_text())
            status["stages"] = [
                stage for stage in status["stages"]
                if stage["id"] != "virtual-machines"
            ]
            login_status.status_path().write_text(json.dumps(status))
            login_status.status_path().chmod(0o600)

            login_status.initialize("same-login")

            restored = json.loads(login_status.status_path().read_text())
            identifiers = [stage["id"] for stage in restored["stages"]]
            self.assertEqual(
                identifiers,
                [identifier for identifier, _label in login_status.DEFAULT_STAGES],
            )
            gnome = next(stage for stage in restored["stages"] if stage["id"] == "gnome")
            vm = next(
                stage for stage in restored["stages"]
                if stage["id"] == "virtual-machines"
            )
            self.assertEqual(gnome["state"], "ready")
            self.assertEqual(vm["label"], "Windows VM restoration")
            self.assertEqual(restored["overall_state"], "running")

    def test_shutdown_replaces_startup_status_and_cancel_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("login-7")
            login_status.initialize_shutdown(
                "login-7", "shutdown-op-9",
                action="restart", origin="preflight",
            )
            status = json.loads(login_status.status_path().read_text())
            self.assertEqual(status["mode"], "shutdown")
            self.assertEqual(status["operation_id"], "shutdown-op-9")
            self.assertEqual(status["shutdown_action"], "restart")
            self.assertEqual(status["shutdown_origin"], "preflight")
            self.assertFalse(status["cancelled"])
            self.assertEqual(
                [stage["id"] for stage in status["stages"]],
                [identifier for identifier, _label in login_status.SHUTDOWN_STAGES],
            )
            self.assertFalse(any(
                stage.get("group_id") == "cloud-drives"
                for stage in status["stages"]
            ))

            login_status.update_stage("tmux-save", "ready", "Saved")
            login_status.cancel_shutdown()
            status = json.loads(login_status.status_path().read_text())
            self.assertEqual(status["overall_state"], "ready")
            self.assertTrue(status["cancelled"])
            self.assertTrue(all(
                stage["state"] in login_status.TERMINAL_STATES
                for stage in status["stages"]
            ))

    def test_dynamic_shutdown_profiles_are_inserted_before_integrity_proof(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-7", "shutdown-op-9")
            self.assertTrue(login_status.register_shutdown_stages([
                ("profile-windows-vm", "Windows VM hibernation"),
                ("profile-backup", "Backup checkpoint"),
            ]))
            status = json.loads(login_status.status_path().read_text())

        self.assertEqual(
            [stage["id"] for stage in status["stages"]],
            [
                "tmux-save", "workspace-save", "social-apps-save", "file-manager-save", "vscode-save", "profile-windows-vm",
                "profile-backup", "checkpoint-proof",
            ],
        )

    def test_cancelled_shutdown_remains_running_until_recovery_finishes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-7", "shutdown-op-9")
            login_status.cancel_shutdown("Cancelling", recovery_pending=True)
            status = json.loads(login_status.status_path().read_text())
            recovery = next(
                stage for stage in status["stages"]
                if stage["id"] == "profile-recovery"
            )

        self.assertTrue(status["cancelled"])
        self.assertEqual(status["overall_state"], "running")
        self.assertEqual(recovery["state"], "running")

    def test_new_publisher_replaces_terminal_shutdown_status_for_same_login(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize("login-7")
            login_status.initialize_shutdown(
                "login-7", "shutdown-op-9",
                action="poweroff", origin="preflight",
            )
            login_status.update_stage(
                "tmux-save", "failed", "checkpoint failed", error="checkpoint failed",
            )

            login_status.initialize("login-7", show_startup_hud=False)
            status = json.loads(login_status.status_path().read_text())

            self.assertEqual(status["mode"], "startup")
            self.assertFalse(status["show_startup_hud"])

    def test_cancel_request_is_private_bound_and_consumed_once(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-8", "expected-operation")
            request = {
                "schema_version": login_status.SCHEMA_VERSION,
                "operation_id": "expected-operation",
                "session_id": "login-8",
                "operation_context": json.loads(login_status.status_path().read_text())["operation_context"],
            }
            login_status.cancel_path().write_text(json.dumps(request))
            login_status.cancel_path().chmod(0o600)

            self.assertTrue(login_status.consume_shutdown_cancel("expected-operation"))
            self.assertFalse(login_status.cancel_path().exists())
            self.assertFalse(login_status.consume_shutdown_cancel("expected-operation"))

    def test_stale_cancel_request_cannot_cancel_a_new_operation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            login_status.initialize_shutdown("login-9", "new-operation")
            login_status.cancel_path().write_text(json.dumps({
                "schema_version": login_status.SCHEMA_VERSION,
                "operation_id": "old-operation",
            }))

            self.assertFalse(login_status.consume_shutdown_cancel("new-operation"))
            self.assertFalse(login_status.cancel_path().exists())


if __name__ == "__main__":
    unittest.main()
