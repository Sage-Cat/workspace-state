from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import cli, operations, storage


PROJECT = Path(__file__).resolve().parents[1]
UUID = "11111111-1111-4111-8111-111111111111"


class ShutdownTmuxHookTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "WSCTL_OPERATION_CONTEXT": "",
            "WSCTL_SHUTDOWN_OPERATION_ID": "b" * 32,
        }))
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.stack.enter_context(redirect_stderr(io.StringIO()))
        runtime = self.root / "runtime/workspace-state"
        runtime.mkdir(mode=0o700, parents=True)
        (runtime / "login-generation").write_text("a" * 16 + "\n")
        self.context = operations.OperationContext.create("a" * 16, "shutdown", operation_id="b" * 32)
        self.document = {
            "schema_version": 1, "mode": "shutdown", "session_id": "a" * 16,
            "operation_id": "b" * 32, "shutdown_action": "poweroff",
            "shutdown_origin": "preflight", "operation_state": "preparing",
            "overall_state": "running", "cancelled": False,
            "operation_context": self.context.to_dict(),
        }
        self.status = runtime / "login-hud-status.json"
        self.write_status()
        self.resurrect = self.root / "resurrect"
        self.resurrect.mkdir()
        self.old = self.resurrect / "tmux_resurrect_20260101T010101.txt"
        self.old.write_text("old layout\n")
        self.candidate = self.resurrect / "tmux_resurrect_20260101T010102.txt"
        self.candidate.write_text(
            "pane\tmain\t1\t1\t:*\t1\ttitle\t:/synthetic\t1\tcodex\t:codex\n"
            "window\tmain\t1\t:current-task\t1\t:*\tlayout\toff\n"
        )
        (self.resurrect / "last").symlink_to(self.old.name)
        self.snapshot = {"created_at": "new", "desktop": {"shell_companion": True},
                         "terminals": [], "sessions": [{
            "name": "main", "windows": [{
                "index": 1, "name": "current-task", "layout": "layout",
                "panes": [{"index": 1, "cwd": "/synthetic", "command": "codex",
                           "codex": {"session_id": UUID}}],
            }],
        }]}
        storage.save({"created_at": "old", "sessions": [], "terminals": []})

    def write_status(self):
        self.status.write_text(json.dumps(self.document))
        self.status.chmod(0o600)

    def test_verified_shutdown_captures_latest_layout_even_when_startup_is_unarmed(self):
        self.assertFalse(cli._autosave_marker().exists())
        with patch.object(cli, "capture", return_value=self.snapshot), \
                patch("workspace_state.resurrect.codex_resume_token", return_value=f"wsctl-codex {UUID}"):
            self.assertEqual(cli.cmd_tmux_save(Namespace(state_file=str(self.candidate))), 0)
        self.assertIn("current-task", self.candidate.read_text())
        self.assertIn(UUID, self.candidate.read_text())
        self.assertEqual(storage.load()["sessions"], self.snapshot["sessions"])
        self.assertEqual(self.old.read_text(), "old layout\n")

    def test_unverified_unarmed_hook_keeps_periodic_preservation(self):
        self.document["operation_id"] = "c" * 32
        self.write_status()
        with patch.object(cli, "capture") as capture, patch.object(cli, "annotate_state_file") as annotate:
            self.assertEqual(cli.cmd_tmux_save(Namespace(state_file=str(self.candidate))), 0)
        capture.assert_not_called()
        annotate.assert_not_called()
        self.assertEqual(self.candidate.read_text(), self.old.read_text())

    def test_shutdown_bypass_rejects_cancelled_superseded_and_completed_operations(self):
        self.assertEqual(cli.cmd_tmux_save_allowed(Namespace(shutdown_operation="b" * 32)), 0)
        for key, value in (("cancelled", True), ("operation_id", "c" * 32),
                           ("session_id", "c" * 16), ("operation_state", "completed")):
            with self.subTest(key=key):
                original = self.document[key]
                self.document[key] = value
                self.write_status()
                self.assertEqual(cli.cmd_tmux_save_allowed(Namespace(shutdown_operation="b" * 32)), 2)
                self.document[key] = original
        self.write_status()


class ShutdownTmuxWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runtime = self.root / "runtime"
        self.lock_root = self.runtime / "workspace-state"
        self.lock_root.mkdir(mode=0o700, parents=True)
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.wrapper = self.fake_bin / "wsctl-continuum-save"
        shutil.copy2(PROJECT / "bin/wsctl-continuum-save", self.wrapper)
        self.write_executable(self.fake_bin / "wsctl", "exit ${TEST_GUARD_STATUS:-0}\n")
        self.write_executable(self.fake_bin / "tmux", "printf '%s\\n' \"$*\" >> \"$TEST_TMUX_LOG\"\n")
        self.write_executable(self.fake_bin / "date", "cat \"$TEST_SECOND_FILE\"\n")
        self.write_executable(self.fake_bin / "sleep", "printf '12346\\n' > \"$TEST_SECOND_FILE\"\n")
        self.plugin = self.root / "plugins/tmux-resurrect/scripts"
        self.plugin.mkdir(parents=True)
        self.write_executable(self.plugin / "save.sh", "printf 'saved\\n' >> \"$TEST_SAVE_LOG\"\n")
        self.second = self.root / "second"
        self.second.write_text("12345\n")
        self.save_log = self.root / "save.log"
        self.tmux_log = self.root / "tmux.log"
        self.environment = {
            **os.environ, "PATH": f"{self.fake_bin}:{os.environ['PATH']}",
            "XDG_RUNTIME_DIR": str(self.runtime), "TMUX_PLUGIN_MANAGER_PATH": str(self.root / "plugins"),
            "TEST_SAVE_LOG": str(self.save_log), "TEST_TMUX_LOG": str(self.tmux_log),
            "TEST_SECOND_FILE": str(self.second),
        }

    def write_executable(self, path, content):
        path.write_text("#!/bin/sh\nset -eu\n" + content)
        path.chmod(0o755)

    def run_wrapper(self, *, shutdown=True):
        args = ["--shutdown-operation", "b" * 32, "quiet"] if shutdown else []
        return subprocess.run([self.wrapper, *args], env=self.environment,
                              capture_output=True, text=True, timeout=5, check=False)

    def test_shutdown_waits_for_new_filename_with_or_without_runtime_stamp(self):
        for stamped in (False, True):
            with self.subTest(stamped=stamped):
                self.second.write_text("12345\n")
                stamp = self.lock_root / "tmux-resurrect-save.second"
                stamp.unlink(missing_ok=True)
                if stamped:
                    stamp.write_text("12345\n")
                old = self.root / "tmux_resurrect_12345.txt"
                old.write_text("old layout\n")
                (self.root / "last").unlink(missing_ok=True)
                (self.root / "last").symlink_to(old.name)
                self.write_executable(self.plugin / "save.sh",
                    'second=$(cat "$TEST_SECOND_FILE")\n'
                    'target="${TEST_SAVE_LOG%/*}/tmux_resurrect_${second}.txt"\n'
                    'test "$target" != "$(readlink -f "${TEST_SAVE_LOG%/*}/last")" || exit 9\n'
                    'printf "new layout\\n" > "$target"\n'
                    'ln -sfn "${target##*/}" "${TEST_SAVE_LOG%/*}/last"\n')
                result = self.run_wrapper()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(old.read_text(), "old layout\n")
                self.assertEqual((self.root / "last").read_text(), "new layout\n")
                self.assertEqual(stamp.read_text(), "12346\n")

    def test_periodic_save_still_skips_same_second_and_busy_lock(self):
        (self.lock_root / "tmux-resurrect-save.second").write_text("12345\n")
        self.assertEqual(self.run_wrapper(shutdown=False).returncode, 0)
        (self.lock_root / "tmux-resurrect-save.second").unlink()
        lock = os.open(self.lock_root, os.O_RDONLY)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.run_wrapper(shutdown=False).returncode, 0)
        finally:
            os.close(lock)
        self.assertFalse(self.save_log.exists())

    def test_required_save_waits_for_busy_lock_instead_of_returning_success(self):
        self.write_executable(self.fake_bin / "flock",
            'printf "%s\\n" "$*" > "${TEST_SAVE_LOG}.lock"\nexit 1\n')
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 2)
        self.assertIn("timed out", result.stderr)
        self.assertEqual(Path(str(self.save_log) + ".lock").read_text(), "-w 10 9\n")
        self.assertFalse(self.save_log.exists())

    def test_stuck_clock_fails_without_overwriting_last(self):
        self.write_executable(self.fake_bin / "sleep", ":\n")
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 2)
        self.assertIn("distinct shutdown tmux checkpoint second", result.stderr)
        self.assertFalse(self.save_log.exists())

    def test_invalid_shutdown_authority_mutates_neither_tmux_nor_checkpoint(self):
        self.environment["TEST_GUARD_STATUS"] = "2"
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.tmux_log.exists())
        self.assertFalse(self.save_log.exists())

    def test_authority_is_rechecked_after_waiting_for_a_distinct_second(self):
        self.write_executable(self.fake_bin / "wsctl",
            'count_file="${TEST_SAVE_LOG}.guard-count"\n'
            'count=0\n[ ! -f "$count_file" ] || count=$(cat "$count_file")\n'
            'count=$((count + 1))\nprintf "%s\\n" "$count" > "$count_file"\n'
            '[ "$count" -lt 3 ] || exit 2\n')
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.second.read_text(), "12346\n")
        self.assertFalse(self.tmux_log.exists())
        self.assertFalse(self.save_log.exists())

    def test_cancelled_drain_protection_precedes_shutdown_mutations(self):
        self.environment["TEST_GUARD_STATUS"] = "1"
        result = self.run_wrapper()
        self.assertEqual(result.returncode, 0)
        self.assertIn("pre-drain checkpoint retained", result.stderr)
        self.assertFalse(self.tmux_log.exists())
        self.assertFalse(self.save_log.exists())


if __name__ == "__main__":
    unittest.main()
