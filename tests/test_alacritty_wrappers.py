from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


class WrapperHarness:
    """Copies of the wrappers with fake alacritty/tmux/launcher/startup directory."""

    def __init__(self, root: Path):
        self.root = root
        self.bin = root / "bin"
        self.fake = root / "fake"
        self.startup = root / "startup"
        self.log = root / "calls.log"
        for directory in (self.bin, self.fake, self.startup, root / "src/workspace_state"):
            directory.mkdir(parents=True, exist_ok=True)
        (root / "src/workspace_state/__init__.py").write_text("")
        (root / "src/workspace_state/startup.py").write_text(
            "import os\nimport sys\n"
            "command = sys.argv[1] if len(sys.argv) > 1 else 'directory'\n"
            "if command == 'saved-sessions':\n"
            "    rc = int(os.environ.get('FAKE_SAVED_RC', '0'))\n"
            "    if rc:\n"
            "        raise SystemExit(rc)\n"
            "    saved = os.environ.get('FAKE_SAVED_SESSIONS', '')\n"
            "    if saved:\n"
            "        print(saved)\n"
            "else:\n"
            "    print(os.environ['FAKE_STARTUP_DIR'])\n")
        for name in ("alacritty-wsctl", "alacritty-tmux-session"):
            text = (BIN / name).read_text()
            text = text.replace("/usr/bin/alacritty", str(self.fake / "alacritty"))
            text = text.replace("/usr/bin/tmux", str(self.fake / "tmux"))
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.script(self.bin / "wsctl-startup-launch", 'exit "${FAKE_LAUNCH_RC}"')
        self.script(self.fake / "alacritty", 'echo "alacritty $*" >>"${FAKE_LOG}"')
        self.script(self.fake / "tmux", '''echo "tmux $*" >>"${FAKE_LOG}"
case "$1" in
  list-sessions) printf '%s\\n' ${FAKE_LIVE_SESSIONS} ;;
  display-message) echo 0 ;;
  new-session)
    for live in ${FAKE_LIVE_SESSIONS}; do
      [ "$live" = "$4" ] && exit 1
    done ;;
esac
exit 0''')

    @staticmethod
    def script(path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def run(self, name: str, *arguments: str, rc: int, claimed=False, done=False, python=True,
            saved=None, saved_rc=0, live=()):
        for marker, present in (("launcher.claimed", claimed), ("terminals.done", done)):
            (self.startup / marker).unlink(missing_ok=True)
            if present:
                (self.startup / marker).touch()
        self.log.write_text("")
        environment = {
            "PATH": f"{self.fake}:/usr/bin:/bin", "HOME": str(self.root),
            "FAKE_LAUNCH_RC": str(rc), "FAKE_LOG": str(self.log),
            "FAKE_STARTUP_DIR": str(self.startup if python else ""),
            "FAKE_SAVED_SESSIONS": "\n".join(saved or ()), "FAKE_SAVED_RC": str(saved_rc),
            "FAKE_LIVE_SESSIONS": " ".join(live),
        }
        if not python:
            environment["PATH"] = f"{self.root / 'nopython'}:{self.fake}"
            (self.root / "nopython").mkdir(exist_ok=True)
            for tool in ("readlink", "dirname", "sed", "tr", "sort", "tail", "env", "cat"):
                target = shutil.which(tool, path="/usr/bin:/bin")
                link = self.root / "nopython" / tool
                if target and not link.exists():
                    link.symlink_to(target)
        subprocess.run(["/bin/sh", str(self.bin / name), *arguments], env=environment,
                       check=False, capture_output=True, timeout=30)
        return self.log.read_text().splitlines()


class AlacrittyWrapperTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.harness = WrapperHarness(Path(directory.name))

    def temporary_session(self, calls):
        return any(call.startswith("tmux new-session -d -s Workspace-1") for call in calls) and any(
            call.startswith("alacritty") and "attach-session" in call for call in calls)

    def plain(self, calls):
        return calls == ["alacritty "]

    def test_session_wrapper_never_creates_main_while_the_restore_is_unfinished(self):
        for rc, claimed in ((0, False), (2, False), (1, True), (3, True)):
            with self.subTest(rc=rc):
                calls = self.harness.run("alacritty-tmux-session", rc=rc, claimed=claimed)
                self.assertTrue(self.temporary_session(calls), calls)

    def test_session_wrapper_uses_main_only_after_the_terminal_restore_finished(self):
        for rc in (0, 2):
            with self.subTest(rc=rc):
                calls = self.harness.run("alacritty-tmux-session", rc=rc, claimed=True, done=True)
                self.assertTrue(self.plain(calls), calls)
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True, done=True)
        self.assertTrue(self.temporary_session(calls), calls)

    def test_session_wrapper_without_python_stays_off_main_after_a_claim(self):
        calls = self.harness.run("alacritty-tmux-session", rc=2, claimed=True, python=False)
        self.assertFalse(self.plain(calls), calls)

    def new_sessions(self, calls):
        return [call.split()[4] for call in calls if call.startswith("tmux new-session")]

    def test_temporary_session_skips_saved_names_on_a_later_launch(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True,
                                 saved=["Workspace-1", "Workspace-2", "main"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-3"], calls)

    def test_login_launch_with_pending_restore_skips_saved_names(self):
        # rc 0/2 without terminals.done is the terminal opened during login restore.
        for name in ("alacritty-tmux-session", "alacritty-wsctl"):
            for rc in (0, 2):
                with self.subTest(name=name, rc=rc):
                    calls = self.harness.run(name, rc=rc, saved=["Workspace-1", "main"])
                    self.assertEqual(self.new_sessions(calls), ["Workspace-2"], calls)

    def test_every_tmux_target_names_the_session_exactly(self):
        # A closed Workspace-1 must never resolve to a restored Workspace-10 by prefix.
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True,
                                 saved=["Workspace-11"], live=["Workspace-10"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-12"], calls)
        targeted = [call for call in calls if call.startswith("tmux ") and " -t " in call]
        self.assertIn("tmux kill-session -t =Workspace-12", calls)
        self.assertEqual(len(targeted), 3, calls)
        for call in targeted:
            self.assertIn(call.split(" -t ", 1)[1].split()[0], {"=Workspace-12", "=Workspace-12:"}, call)
        self.assertTrue(any(call.endswith("set-option -t =Workspace-12: destroy-unattached on")
                            for call in calls), calls)
        self.assertTrue(any(call.endswith("attach-session -t =Workspace-12") for call in calls), calls)

    def test_temporary_session_skips_saved_names_after_restore_finished(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True, done=True,
                                 saved=["Workspace-1", "Workspace-2"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-3"], calls)

    def test_temporary_session_skips_saved_names_and_live_sessions(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True,
                                 saved=["Workspace-2", "Workspace-3"], live=["Workspace-1"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-4"], calls)

    def test_saved_name_match_is_exact(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True,
                                 saved=["Workspace-10", "xWorkspace-1", "Workspace-1 "])
        self.assertEqual(self.new_sessions(calls), ["Workspace-1"], calls)

    def test_unreadable_saved_list_uses_a_per_launch_suffix(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True, saved_rc=1)
        names = self.new_sessions(calls)
        self.assertEqual(len(names), 1, calls)
        self.assertRegex(names[0], r"^Workspace-1-[0-9]+$")
        self.assertNotEqual(names[0], "Workspace-1")

    def test_empty_saved_list_keeps_the_first_name(self):
        calls = self.harness.run("alacritty-tmux-session", rc=1, claimed=True, saved=[])
        self.assertEqual(self.new_sessions(calls), ["Workspace-1"], calls)

    def test_plain_wrapper_hands_a_pending_login_to_the_temporary_session(self):
        for rc, claimed in ((0, False), (2, False), (1, True)):
            with self.subTest(rc=rc):
                calls = self.harness.run("alacritty-wsctl", rc=rc, claimed=claimed)
                self.assertTrue(self.temporary_session(calls), calls)

    def test_plain_wrapper_is_plain_after_the_restore_or_without_a_claim(self):
        calls = self.harness.run("alacritty-wsctl", rc=1, claimed=True, done=True)
        self.assertTrue(self.plain(calls), calls)
        calls = self.harness.run("alacritty-wsctl", rc=3, claimed=False)
        self.assertTrue(self.plain(calls), calls)

    def test_plain_wrapper_without_python_uses_the_temporary_session(self):
        calls = self.harness.run("alacritty-wsctl", rc=1, claimed=True, python=False)
        self.assertTrue(self.temporary_session(calls), calls)

    def test_explicit_arguments_pass_through_unchanged(self):
        calls = self.harness.run("alacritty-wsctl", "--title", "x", rc=0)
        self.assertEqual(calls, ["alacritty --title x"])


if __name__ == "__main__":
    unittest.main()
