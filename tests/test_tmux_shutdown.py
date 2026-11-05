from __future__ import annotations

import io
import os
import signal
import socket
import stat
import subprocess
import shutil
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from workspace_state import tmux_shutdown


class TmuxShutdownTests(unittest.TestCase):
    def test_running_system_is_noop(self):
        with patch.object(tmux_shutdown, "_system_is_stopping", return_value=False), \
             patch.object(tmux_shutdown.subprocess, "run") as command:
            self.assertEqual(tmux_shutdown.run(), 0)
        command.assert_not_called()

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

    def fake_proc(self, root, *, socket_path, inode=777, processes=()):
        """Build a minimal /proc: (pid, comm, uid, held inodes) per process."""
        (root / "net").mkdir(parents=True)
        (root / "net/unix").write_text(
            "Num       RefCount Protocol Flags    Type St Inode Path\n"
            f"0000: 00000002 00000000 00010000 0001 01 {inode} {socket_path}\n"
            f"0000: 00000003 00000000 00000000 0001 03 {inode + 1} {socket_path}\n"
        )
        for pid, comm, uid, inodes in processes:
            directory = root / str(pid)
            (directory / "fd").mkdir(parents=True)
            (directory / "comm").write_text(comm + "\n")
            (directory / "status").write_text(
                f"Name:\t{comm}\nState:\tS (sleeping)\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n")
            for number, held in enumerate(inodes):
                (directory / "fd" / str(number)).symlink_to(f"socket:[{held}]")
        return root

    def test_listening_inode_ignores_connected_sockets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fake_proc(Path(directory), socket_path=Path("/tmp/tmux-1000/default"))
            with patch.object(tmux_shutdown, "PROC", root):
                self.assertEqual(tmux_shutdown._listening_inode(Path("/tmp/tmux-1000/default")), 777)
                self.assertIsNone(tmux_shutdown._listening_inode(Path("/tmp/other")))

    def test_server_is_found_by_the_socket_inode_without_asking_tmux(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fake_proc(Path(directory), socket_path=Path("/tmp/tmux-1000/default"), processes=[
                (11, "tmux: server", 1000, [3, 777]),
                (12, "tmux: client", 1000, [778]),       # a client holds the connected end
                (13, "tmux: server", 1001, [777]),       # another user's process is ignored
                (14, "bash", 1000, [777]),               # not tmux
            ])
            with patch.object(tmux_shutdown, "PROC", root), \
                 patch.object(tmux_shutdown.subprocess, "run") as command:
                self.assertEqual(tmux_shutdown._find_server(777, 1000), 11)
            command.assert_not_called()

    def test_missing_and_ambiguous_servers_are_distinguished(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.fake_proc(Path(directory), socket_path=Path("/tmp/s"), processes=[
                (21, "tmux: server", 1000, [1]), (22, "tmux: server", 1000, [777]),
                (23, "tmux", 1000, [777])])
            with patch.object(tmux_shutdown, "PROC", root):
                with self.assertRaises(tmux_shutdown.ServerLookupError) as ambiguous:
                    tmux_shutdown._find_server(777, 1000)
                self.assertEqual(ambiguous.exception.kind, "ambiguous")
                with self.assertRaises(tmux_shutdown.ServerLookupError) as missing:
                    tmux_shutdown._find_server(999, 1000)
                self.assertEqual(missing.exception.kind, "not-found")

    def run_with_server(self, *, find=None, alive=True, identities=None):
        identity = (1, 2, 1000, stat.S_IFSOCK)
        output = io.StringIO()
        lookup = {"side_effect": find} if find is not None else {"return_value": 4321}
        with patch.object(tmux_shutdown.os, "getuid", return_value=1000), \
             patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
             patch.object(tmux_shutdown, "_socket_identity", side_effect=identities or [identity, identity]), \
             patch.object(tmux_shutdown, "_listening_inode", return_value=777), \
             patch.object(tmux_shutdown, "_find_server", **lookup), \
             patch.object(tmux_shutdown, "_is_server", return_value=True), \
             patch.object(tmux_shutdown, "_wait_for_exit", return_value=alive), \
             patch.object(tmux_shutdown.os, "kill") as kill, \
             patch.object(tmux_shutdown.subprocess, "run") as command, \
             redirect_stdout(output):
            self.assertEqual(tmux_shutdown.run(), 0)
        command.assert_not_called()
        return kill, output.getvalue()

    def test_stopping_sends_sigterm_to_the_server_and_waits(self):
        kill, output = self.run_with_server()
        kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertIn("tmux server stopped (pid 4321)", output)

    def test_exit_timeout_is_reported_as_a_timeout(self):
        kill, output = self.run_with_server(alive=False)
        kill.assert_called_once_with(4321, signal.SIGTERM)
        self.assertIn("timeout", output)

    def test_missing_server_is_reported_and_nothing_is_signalled(self):
        kill, output = self.run_with_server(find=tmux_shutdown.ServerLookupError("not-found", "none"))
        kill.assert_not_called()
        self.assertIn("server not found", output)

    def test_ambiguous_server_is_reported_and_nothing_is_signalled(self):
        kill, output = self.run_with_server(find=tmux_shutdown.ServerLookupError("ambiguous", "two"))
        kill.assert_not_called()
        self.assertIn("ambiguous", output)

    def test_socket_replacement_is_noop(self):
        kill, _output = self.run_with_server(
            identities=[(1, 2, 1000, stat.S_IFSOCK), (1, 3, 1000, stat.S_IFSOCK)])
        kill.assert_not_called()

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
                inode = tmux_shutdown._listening_inode(socket_path)
                self.assertIsNotNone(inode)
                pid = tmux_shutdown._find_server(inode, os.getuid())
                with patch.dict(os.environ, {"TMUX": "", "TMUX_PANE": ""}), \
                     patch.object(tmux_shutdown.os, "getuid", return_value=os.getuid()), \
                     patch.object(tmux_shutdown, "_socket_path", return_value=socket_path), \
                     patch.object(tmux_shutdown, "_system_is_stopping", return_value=True), \
                     redirect_stdout(io.StringIO()):
                    self.assertEqual(tmux_shutdown.run(), 0)
                self.assertFalse(tmux_shutdown._is_server(pid, os.getuid()))
                probe = socket.socket(socket.AF_UNIX)
                self.addCleanup(probe.close)
                self.assertNotEqual(probe.connect_ex(str(socket_path)), 0)
            finally:
                subprocess.run(
                    [*base, "kill-server"], env=environment,
                    check=False, capture_output=True, text=True, timeout=5,
                )
        self.assertEqual(tmux_shutdown._socket_identity(production_socket, os.getuid()), original_identity)
