from __future__ import annotations

import subprocess
import unittest
from unittest.mock import call, patch

from workspace_state import login_finalize


class LoginFinalizeTests(unittest.TestCase):
    def setUp(self):
        ownership = patch.object(login_finalize, "_check_operation")
        ownership.start()
        self.addCleanup(ownership.stop)
        failures = patch.object(login_finalize, "_failed_startup_stages", return_value=[])
        failures.start()
        self.addCleanup(failures.stop)
        # Drive assertions are independent of the user's current app markers.
        for name in ("finish_deferred_file_manager", "finish_deferred_vscode", "finish_deferred_codex"):
            deferred = patch.object(login_finalize, name, return_value=True)
            deferred.start()
            self.addCleanup(deferred.stop)

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

        self.assertEqual(results, {drive.stage: True for drive in login_finalize.DRIVES})
        starts = [
            call(
                "/usr/bin/systemctl", "--user", "start", "--no-block", drive.unit,
            )
            for drive in login_finalize.DRIVES
        ]
        run.assert_has_calls(starts, any_order=False)
        for drive in login_finalize.DRIVES:
            self.assertTrue(any(
                args[0] == drive.stage and args[1] == "ready"
                for args, _kwargs in update.call_args_list
            ))

    @patch("workspace_state.login_finalize.fail_active")
    @patch("workspace_state.login_finalize.set_overall")
    @patch("workspace_state.login_finalize._warm_cloud_metadata", return_value=True)
    @patch("workspace_state.login_finalize._start_drives", return_value={"gdrive": False})
    def test_main_keeps_failure_visible(self, _drives, _warmup, overall, fail_active):
        self.assertEqual(login_finalize._finalize(), 1)
        overall.assert_any_call("failed", "Login completed with failures: gdrive")
        fail_active.assert_called_once_with("Login completed with failures: gdrive")


if __name__ == "__main__":
    unittest.main()
