import errno
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
loader = importlib.machinery.SourceFileLoader("remmina_agent_prepare", str(ROOT / "system-integration/wsctl-remmina-agent-prepare"))
spec = importlib.util.spec_from_loader(loader.name, loader)
helper = importlib.util.module_from_spec(spec)
loader.exec_module(helper)


class RemminaAgentPrepareTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="wsctl-remmina-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.revision = self.root / "1234"
        self.revision.mkdir(mode=0o755)
        self.path = self.revision / helper.NAME

    def bound(self, listen=False):
        agent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(agent.close)
        agent.bind(str(self.path))
        self.path.chmod(0o600)
        if listen:
            agent.listen(1)
        return agent

    def prepare(self):
        return helper.prepare(self.revision, root=self.root, owner=os.geteuid())

    def test_stale_socket_reproduces_address_in_use_and_can_be_rebound_after_repair(self):
        self.bound().close()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as agent:
            with self.assertRaises(OSError) as error:
                agent.bind(str(self.path))
            self.assertEqual(error.exception.errno, errno.EADDRINUSE)
            self.assertTrue(self.prepare())
            agent.bind(str(self.path))

    def test_absent_socket_is_noop(self):
        self.assertFalse(self.prepare())

    def test_listening_and_bound_unlistening_sockets_are_preserved(self):
        for listening in (False, True):
            with self.subTest(listening=listening):
                agent = self.bound(listening)
                before = self.path.stat().st_ino
                with self.assertRaisesRegex(RuntimeError, "still bound"):
                    self.prepare()
                self.assertEqual(self.path.stat().st_ino, before)
                agent.close()
                self.path.unlink()

    def test_relative_kernel_socket_address_is_ambiguous_and_preserved(self):
        table = self.root / "unix"
        table.write_text("header\n0: 0002 0 0 1 01 123 ssh-agent.socket\n")
        self.assertTrue(helper.kernel_owns_socket(self.path, table=table))

    def test_regular_file_symlink_directory_and_public_socket_are_preserved(self):
        for kind in ("file", "symlink", "directory", "public-socket"):
            with self.subTest(kind=kind):
                if kind == "file":
                    self.path.write_text("preserve")
                elif kind == "symlink":
                    self.path.symlink_to(self.root / "unrelated")
                elif kind == "directory":
                    self.path.mkdir()
                else:
                    self.bound().close()
                    self.path.chmod(0o666)
                before = self.path.lstat()
                with self.assertRaises(RuntimeError):
                    self.prepare()
                self.assertEqual(self.path.lstat().st_ino, before.st_ino)
                self.path.rmdir() if kind == "directory" else self.path.unlink()

    def test_unknown_owner_and_writable_directory_are_preserved(self):
        self.bound().close()
        self.revision.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, "unsafe Remmina directory"):
            self.prepare()
        self.assertTrue(self.path.exists())
        self.revision.chmod(0o755)
        with self.assertRaisesRegex(RuntimeError, "unsafe Remmina directory"):
            helper.prepare(self.revision, root=self.root, owner=os.geteuid() + 100)

    def test_unknown_revision_and_symlink_revision_are_refused(self):
        with self.assertRaises(RuntimeError):
            helper.prepare(self.root, root=self.root, owner=os.geteuid())
        link = self.root / "5678"
        link.symlink_to(self.revision)
        with self.assertRaises(RuntimeError):
            helper.prepare(link, root=self.root, owner=os.geteuid())

    def test_kernel_probe_error_and_new_owner_are_preserved(self):
        self.bound().close()
        for failure in (OSError("unreadable table"), [False, True]):
            with self.subTest(failure=failure), patch.object(helper, "kernel_owns_socket", side_effect=failure):
                with self.assertRaises((RuntimeError, OSError)):
                    self.prepare()
                self.assertTrue(self.path.exists())

    def test_permission_error_and_timeout_do_not_authorize_removal(self):
        self.bound().close()
        for error in (PermissionError(errno.EACCES, "no access"), TimeoutError("timeout")):
            with self.subTest(error=error), patch.object(helper.socket, "socket") as probe:
                probe.return_value.__enter__.return_value.connect.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    self.prepare()
                self.assertTrue(self.path.exists())

    def test_replaced_path_during_probe_is_preserved(self):
        self.bound().close()
        def replace(_path):
            self.path.unlink()
            self.path.write_text("new occupant")
            raise ConnectionRefusedError(errno.ECONNREFUSED, "stale")
        with patch.object(helper.socket, "socket") as probe:
            probe.return_value.__enter__.return_value.connect.side_effect = replace
            with self.assertRaisesRegex(RuntimeError, "changed"):
                self.prepare()
        self.assertEqual(self.path.read_text(), "new occupant")


if __name__ == "__main__":
    unittest.main()
