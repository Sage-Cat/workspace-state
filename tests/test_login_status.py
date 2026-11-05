from __future__ import annotations

import json
import fcntl
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import login_status


class LoginStatusTests(unittest.TestCase):
    def test_nonblocking_diagnostic_does_not_wait_for_busy_hud_writer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {'XDG_RUNTIME_DIR': directory}, clear=False,
        ):
            root = Path(directory) / 'workspace-state'
            root.mkdir()
            with (root / 'login-hud-status.lock').open('a+') as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                self.assertFalse(login_status.append_diagnostic('barrier', 'temporary marker remained', blocking=False))
            self.assertTrue(login_status.append_diagnostic('barrier', 'temporary marker remained', blocking=False))
            self.assertIn('temporary marker remained', (root / 'login-hud.log').read_text())

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
                if stage["id"] != "warmup"
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
                if stage["id"] == "warmup"
            )
            self.assertEqual(gnome["state"], "ready")
            self.assertEqual(vm["label"], "Cloud metadata warm-up")
            self.assertEqual(restored["overall_state"], "running")


if __name__ == "__main__":
    unittest.main()
