from dataclasses import replace
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import graphical_stop


GROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/wsctl-app-example-abcd1234.service"


class MigratedOwnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.cgroups = self.root / "cgroup"
        self.members = self.cgroups / GROUP.lstrip("/") / "cgroup.procs"
        self.members.parent.mkdir(parents=True)
        self.members.write_text("301\n302\n")
        self.exe = self.root / "example-browser"
        self.exe.write_text("fixture")
        self.write_process(300, 1, 100, str(Path(GROUP).parent / "app-org.chromium.Chromium-300.scope"))
        self.write_process(301, 300, 110, GROUP)
        self.write_process(302, 301, 120, GROUP)

    def write_process(self, pid, parent, started, group, executable=None):
        root = self.proc / str(pid)
        root.mkdir(parents=True, exist_ok=True)
        fields = ["S", str(parent), *(["0"] * 17), str(started)]
        (root / "stat").write_text(f"{pid} (name with space) " + " ".join(fields))
        (root / "cgroup").write_text(f"0::{group}\n")
        (root / "exe").unlink(missing_ok=True)
        (root / "exe").symlink_to(executable or self.exe)

    def owner(self):
        return graphical_stop.migrated_owner(GROUP, proc=self.proc, cgroups=self.cgroups)

    def editor_group(self, purpose="native-recovery", *, scope=None):
        group = str(Path(GROUP).parent / f"wsctl-app-vscode-{purpose}-abcd1234.service")
        members = self.cgroups / group.lstrip("/") / "cgroup.procs"
        members.parent.mkdir(parents=True, exist_ok=True)
        members.write_text("301\n302\n")
        self.write_process(300, 1, 100, scope or str(Path(group).parent / "app-com.microsoft.VSCode-300.scope"))
        self.write_process(301, 300, 110, group)
        self.write_process(302, 301, 120, group)
        return group

    def test_native_recovery_and_project_editor_units_prove_migrated_owner(self):
        for purpose in ("native-recovery", "project"):
            with self.subTest(purpose=purpose):
                group = self.editor_group(purpose)
                owner = graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups)
                self.assertIsNotNone(owner)
                self.assertEqual((owner.pid, owner.started), (300, 100))

    def test_editor_scope_is_not_adopted_by_unrelated_or_malformed_unit(self):
        for purpose in ("unrelated", "native-recovery-extra", "project-abcd", "project-ABCDEF00"):
            with self.subTest(purpose=purpose):
                group = self.editor_group(purpose)
                self.assertIsNone(graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups))

    def test_editor_scope_requires_exact_parent_pid_and_sibling_path(self):
        for scope in (
            str(Path(GROUP).parent / "app-com.microsoft.VSCode-301.scope"),
            str(Path(GROUP).parent / "app-com.microsoft.VSCodeExtra-300.scope"),
            str(Path(GROUP).parent / "app-com.microsoft.VSCode-300.scope-extra"),
            "/unrelated/app-com.microsoft.VSCode-300.scope",
        ):
            with self.subTest(scope=scope):
                group = self.editor_group(scope=scope)
                self.assertIsNone(graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups))

    def test_editor_scope_without_matching_live_child_identity_is_not_owned(self):
        group = self.editor_group()
        original = graphical_stop.process
        parent = original(300, self.proc)
        for altered in (replace(parent, uid=parent.uid + 1),
                        replace(parent, executable=(parent.executable[0], parent.executable[1] + 1)),
                        replace(parent, started=200)):
            with self.subTest(altered=altered), patch.object(
                graphical_stop, "process", side_effect=lambda pid, proc, owner=altered: owner if pid == 300 else original(pid, proc),
            ):
                self.assertIsNone(graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups))
        self.write_process(301, 300, 110, "/unrelated.scope")
        self.assertIsNone(graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups))

    def test_editor_two_real_migrated_owners_remain_ambiguous(self):
        group = self.editor_group()
        self.write_process(400, 1, 100, str(Path(group).parent / "app-com.microsoft.VSCode-400.scope"))
        self.write_process(302, 400, 120, group)
        with self.assertRaisesRegex(RuntimeError, "multiple"):
            graphical_stop.migrated_owner(group, proc=self.proc, cgroups=self.cgroups)

    def test_parent_child_executable_and_scope_prove_the_migrated_owner(self):
        self.assertEqual(self.owner().pid, 300)
        self.assertEqual(self.owner().started, 100)

    def test_google_chrome_scope_requires_the_same_proven_owner(self):
        self.write_process(300, 1, 100, str(Path(GROUP).parent / "app-com.google.Chrome-300.scope"))
        self.assertEqual(self.owner().pid, 300)
        self.assertEqual(self.owner().started, 100)

    def test_chrome_name_pid_and_sibling_path_spoofs_are_not_owned(self):
        for scope in (
            str(Path(GROUP).parent / "app-com.google.Chrome-301.scope"),
            str(Path(GROUP).parent / "app-com.google.ChromeExtra-300.scope"),
            str(Path(GROUP).parent / "app-com.google.Chrome-300.scope-extra"),
            "/unrelated/app-com.google.Chrome-300.scope",
        ):
            with self.subTest(scope=scope):
                self.write_process(300, 1, 100, scope)
                self.assertIsNone(self.owner())

    def test_chrome_scope_with_wrong_uid_or_executable_is_not_owned(self):
        self.write_process(300, 1, 100, str(Path(GROUP).parent / "app-com.google.Chrome-300.scope"))
        original = graphical_stop.process
        parent = original(300, self.proc)
        for altered in (replace(parent, uid=parent.uid + 1),
                        replace(parent, executable=(parent.executable[0], parent.executable[1] + 1)),
                        replace(parent, started=200)):
            with self.subTest(altered=altered), patch.object(
                graphical_stop, "process",
                side_effect=lambda pid, proc, owner=altered: owner if pid == 300 else original(pid, proc),
            ):
                self.assertIsNone(self.owner())

    def test_unrelated_parent_wrong_scope_and_reused_older_child_are_not_owned(self):
        for parent, started, group in ((1, 100, GROUP), (1, 100, "/unrelated.scope"),
                                        (1, 200, str(Path(GROUP).parent / "app-org.chromium.Chromium-300.scope"))):
            with self.subTest(started=started, group=group):
                self.write_process(300, parent, started, group)
                self.assertIsNone(self.owner())

    def test_different_executable_is_not_adopted(self):
        other = self.root / "unrelated"
        other.write_text("different")
        self.write_process(301, 300, 110, GROUP, other)
        self.assertIsNone(self.owner())

    def test_member_moved_out_of_this_unit_is_not_evidence(self):
        self.write_process(301, 300, 110, "/unrelated.scope")
        self.assertIsNone(self.owner())

    def test_two_independent_owners_are_ambiguous(self):
        self.write_process(400, 1, 100, str(Path(GROUP).parent / "app-org.chromium.Chromium-400.scope"))
        self.write_process(302, 400, 120, GROUP)
        with self.assertRaisesRegex(RuntimeError, "multiple"):
            self.owner()

    def test_missing_or_unreadable_process_is_not_owned(self):
        (self.proc / "300/stat").unlink()
        self.assertIsNone(self.owner())


class GracefulStopTests(unittest.TestCase):
    owner = graphical_stop.Process(300, 1, 100, (1, 10), os.getuid(), "/app-org.chromium.Chromium-300.scope")

    def test_exact_pidfd_signalling_precedes_bounded_wait(self):
        with patch.object(graphical_stop, "migrated_owner", return_value=self.owner), patch.object(
            graphical_stop.os, "pidfd_open", return_value=77,
        ) as opened, patch.object(graphical_stop.signal, "pidfd_send_signal") as signalled, patch.object(
            graphical_stop.select, "select", return_value=([77], [], []),
        ) as waited, patch.object(graphical_stop.os, "close") as closed:
            self.assertTrue(graphical_stop.stop_migrated_owner(GROUP))
        opened.assert_called_once_with(300)
        signalled.assert_called_once_with(77, signal.SIGTERM)
        waited.assert_called_once_with([77], [], [], 4.0)
        closed.assert_called_once_with(77)

    def test_reused_pid_and_changed_evidence_are_not_signalled(self):
        for changed in (None, replace(self.owner, started=101), replace(self.owner, executable=(1, 11))):
            with self.subTest(changed=changed), patch.object(
                graphical_stop, "migrated_owner", side_effect=[self.owner, changed],
            ), patch.object(graphical_stop.os, "pidfd_open", return_value=77), patch.object(
                graphical_stop.signal, "pidfd_send_signal",
            ) as signalled, patch.object(graphical_stop.os, "close") as closed:
                with self.assertRaisesRegex(RuntimeError, "ownership changed"):
                    graphical_stop.stop_migrated_owner(GROUP)
                signalled.assert_not_called()
                closed.assert_called_once_with(77)

    def test_normal_nonsplit_application_is_left_to_systemd(self):
        with patch.object(graphical_stop, "migrated_owner", return_value=None), patch.object(
            graphical_stop.os, "pidfd_open",
        ) as opened:
            self.assertFalse(graphical_stop.stop_migrated_owner(GROUP))
        opened.assert_not_called()

    def test_timeout_stays_visible_without_escalating_to_a_pid_kill(self):
        with patch.object(graphical_stop, "migrated_owner", return_value=self.owner), patch.object(
            graphical_stop.os, "pidfd_open", return_value=77,
        ), patch.object(graphical_stop.signal, "pidfd_send_signal") as signalled, patch.object(
            graphical_stop.select, "select", return_value=([], [], []),
        ), patch.object(graphical_stop.os, "close"):
            with self.assertRaisesRegex(RuntimeError, "graceful stop budget"):
                graphical_stop.stop_migrated_owner(GROUP)
        signalled.assert_called_once_with(77, signal.SIGTERM)

    def test_pidfd_wait_observes_real_disposable_process_graceful_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            ready = Path(directory) / "ready"
            exited = Path(directory) / "exited"
            code = (
                "import pathlib,signal,time,sys\n"
                "def stop(*_):\n pathlib.Path(sys.argv[2]).write_text('graceful'); sys.exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\npathlib.Path(sys.argv[1]).write_text('ready')\n"
                "while True: time.sleep(0.01)\n"
            )
            child = subprocess.Popen([sys.executable, "-c", code, str(ready), str(exited)])
            try:
                import time
                deadline = time.monotonic() + 3
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(ready.exists())
                owner = replace(self.owner, pid=child.pid)
                with patch.object(graphical_stop, "migrated_owner", return_value=owner):
                    self.assertTrue(graphical_stop.stop_migrated_owner(GROUP, timeout=2))
                self.assertEqual(child.wait(timeout=1), 0)
                self.assertEqual(exited.read_text(), "graceful")
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait()


if __name__ == "__main__":
    unittest.main()
