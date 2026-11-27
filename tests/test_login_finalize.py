from __future__ import annotations

import subprocess
import os
import tempfile
import unittest
from unittest.mock import call, patch

import synthetic_drives
from workspace_state import host_config, login_finalize, login_status, operations


class LoginFinalizeTests(unittest.TestCase):
    def setUp(self):
        previous = operations.current()
        self.addCleanup(lambda: operations.bind(previous))
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {
            'XDG_RUNTIME_DIR': directory.name + '/runtime',
            'XDG_STATE_HOME': directory.name + '/state',
        })
        environment.start()
        self.addCleanup(environment.stop)
        synthetic_drives.install(self)
        operations.bind(None)
        login_status.initialize('a' * 16)
        ownership = patch.object(login_finalize, "_check_operation")
        ownership.start()
        self.addCleanup(ownership.stop)
        failures = patch.object(login_finalize, "_attention_startup_stages", return_value=[])
        failures.start()
        self.addCleanup(failures.stop)
        # Drive assertions are independent of the user's current app markers.
        for name in ("finish_deferred_file_manager", "finish_deferred_vscode", "finish_deferred_codex"):
            deferred = patch.object(login_finalize, name, return_value=True)
            deferred.start()
            self.addCleanup(deferred.stop)

    @patch("workspace_state.login_finalize.time.sleep")
    @patch("workspace_state.login_finalize._unit_properties", return_value={"ActiveState": "active", "Result": "success"})
    @patch("workspace_state.login_finalize._run", return_value=subprocess.CompletedProcess([], 0, "", ""))
    @patch("workspace_state.login_finalize._mounted", return_value=True)
    def test_the_real_finalizer_closes_an_operation_whose_worker_died(self, _mounted, _run, _properties, _sleep):
        import json
        from workspace_state import cli, startup
        for identifier in ("browsers", "social-apps", "workspace"):
            login_status.update_stage(identifier, "running", "working")
        for identifier in ("tmux", "terminals", "codex", "file-manager", "vscode", "gnome", "displays"):
            login_status.update_stage(identifier, "ready", "ok", current=1, total=1)
        with patch.object(cli, "_publish_workspace_restored") as publish:
            self.assertTrue(startup.report_worker_failure("Startup worker ended without completion: timeout"))
        publish.assert_called_once_with()
        before = json.loads(login_status.status_path().read_text())
        self.assertEqual(before["operation_state"], "running")
        self.assertEqual(login_finalize._finalize(), 0)
        after = json.loads(login_status.status_path().read_text())
        states = {stage["id"]: stage["state"] for stage in after["stages"]}
        self.assertEqual(after["operation_state"], "failed")
        self.assertEqual(states["browsers"], "failed")
        self.assertEqual(states["login-finalization"], "ready")
        self.assertTrue(all(state in login_status.TERMINAL_STATES for state in states.values()), states)

    @patch("workspace_state.login_finalize.time.sleep")
    @patch("workspace_state.login_finalize.update_stage")
    @patch("workspace_state.login_finalize._unit_properties", return_value={
        "ActiveState": "activating", "SubState": "start", "Result": "success",
    })
    @patch("workspace_state.login_finalize._run")
    @patch("workspace_state.login_finalize._mounted")
    def test_drives_start_together_and_finish_only_after_mount_verification(
        self, mounted, run, _properties, update, _sleep,
    ):
        seen: dict[str, int] = {}

        def mount_state(path):
            key = str(path)
            seen[key] = seen.get(key, 0) + 1
            return seen[key] >= 2

        mounted.side_effect = mount_state
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        results = login_finalize._start_drives()

        self.assertEqual(results, {drive.stage: True for drive in host_config.load().drives})
        starts = [
            call(
                "/usr/bin/systemctl", "--user", "start", "--no-block", drive.unit,
            )
            for drive in host_config.load().drives
        ]
        run.assert_has_calls(starts, any_order=False)
        for drive in host_config.load().drives:
            self.assertTrue(any(
                args[0] == drive.stage and args[1] == "ready"
                for args, _kwargs in update.call_args_list
            ))

    @patch("workspace_state.login_finalize.update_stage")
    @patch("workspace_state.login_finalize.fail_active")
    @patch("workspace_state.login_finalize.set_overall")
    @patch("workspace_state.login_finalize._warm_cloud_metadata", return_value=True)
    @patch("workspace_state.login_finalize._start_drives", return_value={"drive-a": False})
    def test_needs_attention_is_a_result_not_a_service_failure(self, _drives, _warmup, overall, fail_active, update):
        self.assertEqual(login_finalize._finalize(), 0)
        fail_active.assert_not_called()
        self.assertFalse(any(args.args[:1] == ("failed",) for args in overall.call_args_list))
        self.assertEqual(update.call_args_list[-1].args[:2], ("login-finalization", "ready"))
        self.assertIn("drive-a", update.call_args_list[-1].args[2])

    @patch("workspace_state.login_finalize.fail_active")
    def test_unexpected_exception_still_fails_the_service(self, fail_active):
        with patch.object(login_finalize.operations, "context_from_status",
                          return_value=operations.current()), \
             patch.object(login_finalize, "_finalize", side_effect=RuntimeError("boom")), \
             patch.object(login_finalize, "update_stage"), patch.object(login_finalize, "append_diagnostic"):
            self.assertEqual(login_finalize.main(), 1)
        fail_active.assert_called_once()

    @patch("workspace_state.login_finalize.time.sleep")
    @patch("workspace_state.login_finalize._run")
    @patch("workspace_state.login_finalize._mounted", return_value=True)
    def test_a_drive_that_is_not_started_by_the_finalizer_is_left_alone(self, _mounted, run, _sleep):
        synthetic_drives.install(self, synthetic_drives.toml(synthetic_drives.DRIVES[:1])
                                 + '\n[[drives]]\nstage = "vm-desktop"\nlabel = "VM"\nunit = "example-vm.service"\n'
                                 'mountpoint = "/home/example/Drives/vm/Desktop"\nstart = false\n')
        self.assertEqual(login_finalize._start_drives(), {"drive-a": True})
        run.assert_not_called()

    @patch("workspace_state.login_finalize._run")
    def test_no_host_configuration_starts_nothing_and_succeeds(self, run):
        synthetic_drives.install(self, False)
        self.assertEqual(login_finalize._start_drives(), {})
        run.assert_not_called()
        with patch.object(login_finalize, "_warm_cloud_metadata") as warmup, \
             patch.object(login_finalize, "set_overall"), patch.object(login_finalize, "fail_active"):
            self.assertEqual(login_finalize._finalize(), 0)
        warmup.assert_not_called()

    def test_an_invalid_host_configuration_fails_the_drive_configuration_stage_without_crashing(self):
        synthetic_drives.install(self, '[[drives]]\nstage = "BAD"\n')
        with patch.object(login_finalize, "update_stage") as update, patch.object(login_finalize, "append_diagnostic"):
            results = login_finalize._start_drives()
        self.assertEqual(results, {"drives": False})
        args = update.call_args.args
        self.assertEqual(args[:2], ("drives", "failed"))
        self.assertIn("drives #1 needs a non-empty text label", args[2])


if __name__ == "__main__":
    unittest.main()
