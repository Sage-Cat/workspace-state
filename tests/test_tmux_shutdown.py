from __future__ import annotations

import os
import socket
import stat
import subprocess
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import tmux_shutdown


class TmuxShutdownTests(unittest.TestCase):
    def test_running_system_is_noop(self):
        with patch.object(tmux_shutdown, "_system_is_stopping", return_value=False), \
             patch.object(tmux_shutdown.subprocess, "run") as command:
            self.assertEqual(tmux_shutdown.run(), 0)
        command.assert_not_called()

    def test_stopping_requests_normal_kill_for_valid_server(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "tmux-1000"
            parent.mkdir()
            socket_path = parent / "default"
            # Use a real Unix socket only to exercise lstat validation.
            live_socket = socket.socket(socket.AF_UNIX)
            self.addCleanup(live_socket.close)
            live_socket.bind(str(socket_path))
            identity = (socket_path.stat().st_dev, socket_path.stat().st_ino, 1000, stat.S_IFSOCK)
            with patch.object(tmux_shutdown.os, "getuid", return_value=1000), \
                     patch.object(tmux_shutdown, "_socket_path", return_value=socket_path), \
                 patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
                 patch.object(tmux_shutdown, "_socket_identity", side_effect=[identity, identity]), \
                 patch.object(tmux_shutdown, "_server_pid", return_value=4321), \
                 patch.object(tmux_shutdown, "_is_tmux_server", return_value=True), \
                 patch.object(tmux_shutdown.subprocess, "run") as command:
                command.return_value.returncode = 0
                self.assertEqual(tmux_shutdown.run(), 0)
            self.assertEqual(command.call_args.args[0], [tmux_shutdown.TMUX, "-S", str(socket_path), "kill-server"])

    def test_socket_replacement_is_noop(self):
        with patch.object(tmux_shutdown.os, "getuid", return_value=1000), \
             patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
             patch.object(tmux_shutdown, "_socket_identity", side_effect=[(1, 2, 1000, stat.S_IFSOCK), (1, 3, 1000, stat.S_IFSOCK)]), \
             patch.object(tmux_shutdown, "_server_pid", return_value=4321), \
             patch.object(tmux_shutdown, "_is_tmux_server", return_value=True), \
             patch.object(tmux_shutdown.subprocess, "run") as command:
            self.assertEqual(tmux_shutdown.run(), 0)
        command.assert_not_called()

    def test_kill_command_failure_is_benign(self):
        with patch.object(tmux_shutdown.os, "getuid", return_value=1000), \
             patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
             patch.object(tmux_shutdown, "_socket_identity", return_value=(1, 2, 1000, stat.S_IFSOCK)), \
             patch.object(tmux_shutdown, "_server_pid", return_value=4321), \
             patch.object(tmux_shutdown, "_is_tmux_server", return_value=True), \
             patch.object(tmux_shutdown.subprocess, "run") as command:
            command.return_value.returncode = 1
            self.assertEqual(tmux_shutdown.run(), 0)
        self.assertEqual(command.call_count, 1)

    def test_server_pid_deduplicates_sessions_and_rejects_conflicts(self):
        completed = subprocess.CompletedProcess([], 0, stdout="42\n42\n", stderr="")
        with patch.object(tmux_shutdown.subprocess, "run", return_value=completed):
            self.assertEqual(tmux_shutdown._server_pid(Path("/tmp/socket")), 42)
        completed = subprocess.CompletedProcess([], 0, stdout="42\n43\n", stderr="")
        with patch.object(tmux_shutdown.subprocess, "run", return_value=completed):
            self.assertIsNone(tmux_shutdown._server_pid(Path("/tmp/socket")))

    def test_exact_system_shutdown_state_required(self):
        for state, expected in [("stopping", True), ("running", False), ("degraded", False), ("offline", False)]:
            with patch.object(tmux_shutdown.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, state + "\n", "")) as command:
                self.assertEqual(tmux_shutdown._system_is_stopping(), expected)
            self.assertEqual(command.call_args.args[0], [tmux_shutdown.SYSTEMCTL, "is-system-running"])
        with patch.object(tmux_shutdown.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 2)):
            self.assertFalse(tmux_shutdown._system_is_stopping())

    def test_socket_permissions_and_parent_type_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "tmux-1000"
            parent.mkdir(mode=0o755)
            socket = parent / "default"
            import socket as socket_module
            live_socket = socket_module.socket(socket_module.AF_UNIX)
            self.addCleanup(live_socket.close)
            live_socket.bind(str(socket))
            self.assertIsNone(tmux_shutdown._socket_identity(socket, os.getuid()))

    def test_standard_0770_socket_is_safe_inside_private_owner_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            socket_path = root / "default"
            with socket.socket(socket.AF_UNIX) as server:
                server.bind(str(socket_path))
                socket_path.chmod(0o770)
                self.assertIsNotNone(tmux_shutdown._socket_identity(socket_path, os.getuid()))
                socket_path.chmod(0o772)
                self.assertIsNone(tmux_shutdown._socket_identity(socket_path, os.getuid()))
                socket_path.chmod(0o770)
                root.chmod(0o750)
                self.assertIsNone(tmux_shutdown._socket_identity(socket_path, os.getuid()))

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required for isolated integration")
    def test_isolated_real_two_session_server_is_cleanly_stopped(self):
        production_socket = Path(f"/tmp/tmux-{os.getuid()}/default")
        original_identity = tmux_shutdown._socket_identity(production_socket, os.getuid())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "tmux-1000"
            parent.mkdir(mode=0o700)
            socket_path = parent / "default"
            environment = dict(os.environ)
            environment.pop("TMUX", None)
            environment.pop("TMUX_PANE", None)
            base = ["/usr/bin/tmux", "-f", "/dev/null", "-S", str(socket_path)]
            try:
                for name in ("test-one", "test-two"):
                    subprocess.run(
                        [*base, "new-session", "-d", "-s", name, "/bin/sleep", "30"],
                        env=environment, check=True, capture_output=True, text=True, timeout=5,
                    )
                with patch.dict(os.environ, {"TMUX": "", "TMUX_PANE": ""}), \
                     patch.object(tmux_shutdown.os, "getuid", return_value=os.getuid()), \
                     patch.object(tmux_shutdown, "_socket_path", return_value=socket_path), \
                     patch.object(tmux_shutdown, "_system_is_stopping", return_value=True):
                    self.assertEqual(tmux_shutdown.run(), 0)
                deadline = time.monotonic() + 1
                while socket_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                # tmux may leave a stale pathname briefly (and in tmux 3.4),
                # but the server must have exited and the socket no longer
                # accepts connections.
                self.assertIsNone(tmux_shutdown._server_pid(socket_path))
                probe = socket.socket(socket.AF_UNIX)
                self.addCleanup(probe.close)
                self.assertNotEqual(probe.connect_ex(str(socket_path)), 0)
            finally:
                subprocess.run(
                    [*base, "kill-server"], env=environment,
                    check=False, capture_output=True, text=True, timeout=5,
                )
        self.assertEqual(tmux_shutdown._socket_identity(production_socket, os.getuid()), original_identity)

    def test_invalid_server_identity_is_noop(self):
        with patch.object(tmux_shutdown.os, "getuid", return_value=1000), \
             patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
             patch.object(tmux_shutdown, "_socket_identity", return_value=(1, 2, 1000, stat.S_IFSOCK)), \
             patch.object(tmux_shutdown, "_server_pid", return_value=4321), \
             patch.object(tmux_shutdown, "_is_tmux_server", return_value=False), \
             patch.object(tmux_shutdown.subprocess, "run") as command:
            self.assertEqual(tmux_shutdown.run(), 0)
        command.assert_not_called()
