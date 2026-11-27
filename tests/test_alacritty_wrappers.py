from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[1] / "bin"


class WrapperHarness:
    """Copies of the wrappers with fake alacritty/tmux and a fake `startup` module."""

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
            "if command == 'terminals-settled':\n"
            "    if os.environ['FAKE_SETTLED_RC'] == 'raise':\n"
            "        raise RuntimeError('helper crashed')\n"
            "    raise SystemExit(int(os.environ['FAKE_SETTLED_RC']))\n"
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
        # The wrappers start no restore; any call of the retired launcher is a bug.
        self.script(self.bin / "wsctl-startup-launch", 'touch "${FAKE_LAUNCHER_CALLED}"; exit 0')
        self.script(self.fake / "alacritty", 'echo "alacritty $*" >>"${FAKE_LOG}"')
        self.script(self.fake / "tmux", '''echo "tmux $*" >>"${FAKE_LOG}"
case "$1" in
  list-sessions) printf '%s\\n' ${FAKE_LIVE_SESSIONS} ;;
  display-message) echo 0 ;;
  new-session)
    echo "${PATH}" >>"${FAKE_LOG}.path"
    for live in ${FAKE_LIVE_SESSIONS}; do
      [ "$live" = "$4" ] && exit 1
    done ;;
esac
exit 0''')

    @staticmethod
    def script(path: Path, body: str) -> None:
        path.write_text("#!/bin/sh\n" + body + "\n")
        path.chmod(0o755)

    def run(self, name: str, *arguments: str, pending=False, settled_rc=None, python=True,
            saved=None, saved_rc=0, live=(), extra_env=None):
        (self.root / "launcher.called").unlink(missing_ok=True)
        self.log.write_text("")
        environment = {
            "PATH": f"{self.fake}:/usr/bin:/bin", "HOME": str(self.root),
            "FAKE_SETTLED_RC": str(1 if pending else 0) if settled_rc is None else str(settled_rc),
            "FAKE_LAUNCHER_CALLED": str(self.root / "launcher.called"), "FAKE_LOG": str(self.log),
            "FAKE_STARTUP_DIR": str(self.startup if python else ""),
            "FAKE_SAVED_SESSIONS": "\n".join(saved or ()), "FAKE_SAVED_RC": str(saved_rc),
            "FAKE_LIVE_SESSIONS": " ".join(live),
        }
        environment.update(extra_env or {})
        (self.root / "calls.log.path").write_text("")
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
        assert not (self.root / "launcher.called").exists(), "wrapper called the retired launcher"
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

    def test_session_wrapper_always_uses_a_temporary_session(self):
        for pending in (True, False):
            with self.subTest(pending=pending):
                calls = self.harness.run("alacritty-tmux-session", pending=pending)
                self.assertTrue(self.temporary_session(calls), calls)

    def test_session_wrapper_without_python_still_uses_a_temporary_session(self):
        calls = self.harness.run("alacritty-tmux-session", python=False)
        self.assertFalse(self.plain(calls), calls)

    def new_sessions(self, calls):
        return [call.split()[4] for call in calls if call.startswith("tmux new-session")]

    def test_temporary_session_skips_saved_names_on_a_later_launch(self):
        calls = self.harness.run("alacritty-tmux-session",
                                 saved=["Workspace-1", "Workspace-2", "main"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-3"], calls)

    def test_login_launch_with_pending_restore_skips_saved_names(self):
        # The terminal opened during the login restore must not take a saved name.
        for name in ("alacritty-tmux-session", "alacritty-wsctl"):
            with self.subTest(name=name):
                calls = self.harness.run(name, pending=True, saved=["Workspace-1", "main"])
                self.assertEqual(self.new_sessions(calls), ["Workspace-2"], calls)

    def test_every_tmux_target_names_the_session_exactly(self):
        # A closed Workspace-1 must never resolve to a restored Workspace-10 by prefix.
        calls = self.harness.run("alacritty-tmux-session",
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
        calls = self.harness.run("alacritty-tmux-session", saved=["Workspace-1", "Workspace-2"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-3"], calls)

    def test_temporary_session_skips_saved_names_and_live_sessions(self):
        calls = self.harness.run("alacritty-tmux-session",
                                 saved=["Workspace-2", "Workspace-3"], live=["Workspace-1"])
        self.assertEqual(self.new_sessions(calls), ["Workspace-4"], calls)

    def test_saved_name_match_is_exact(self):
        calls = self.harness.run("alacritty-tmux-session",
                                 saved=["Workspace-10", "xWorkspace-1", "Workspace-1 "])
        self.assertEqual(self.new_sessions(calls), ["Workspace-1"], calls)

    def test_unreadable_saved_list_uses_a_per_launch_suffix(self):
        calls = self.harness.run("alacritty-tmux-session", saved_rc=1)
        names = self.new_sessions(calls)
        self.assertEqual(len(names), 1, calls)
        self.assertRegex(names[0], r"^Workspace-1-[0-9]+$")
        self.assertNotEqual(names[0], "Workspace-1")

    def test_empty_saved_list_keeps_the_first_name(self):
        calls = self.harness.run("alacritty-tmux-session", saved=[])
        self.assertEqual(self.new_sessions(calls), ["Workspace-1"], calls)

    def test_plain_wrapper_hands_a_pending_login_to_the_temporary_session(self):
        calls = self.harness.run("alacritty-wsctl", pending=True)
        self.assertTrue(self.temporary_session(calls), calls)

    def test_plain_wrapper_is_plain_when_nothing_is_pending(self):
        calls = self.harness.run("alacritty-wsctl", pending=False)
        self.assertTrue(self.plain(calls), calls)

    def test_a_failing_or_missing_helper_counts_as_unfinished(self):
        # Only exit status 0 means settled; a crash, a timeout (124) or a missing
        # python must not let the user's shell create `main` before the restore.
        for rc in (2, 124, 127, "raise"):
            with self.subTest(rc=rc):
                calls = self.harness.run("alacritty-wsctl", settled_rc=rc)
                self.assertTrue(self.temporary_session(calls), calls)
        calls = self.harness.run("alacritty-wsctl", python=False)
        self.assertTrue(self.temporary_session(calls), calls)

    def test_plain_wrapper_with_arguments_never_asks_or_uses_a_session(self):
        calls = self.harness.run("alacritty-wsctl", "-e", "true", pending=True)
        self.assertEqual(calls, ["alacritty -e true"])

    def test_wrappers_never_read_the_retired_markers(self):
        for name in ("alacritty-wsctl", "alacritty-tmux-session"):
            text = (BIN / name).read_text()
            for retired in ("wsctl-startup-launch", "launcher.claimed", "terminals.done"):
                self.assertNotIn(retired, text, name)

    def test_session_wrapper_titles_the_window_by_session_and_lets_tmux_rename_it(self):
        calls = self.harness.run("alacritty-tmux-session")
        launch = next(call for call in calls if call.startswith("alacritty"))
        self.assertIn("--title Workspace-1 ", launch + " ")
        self.assertIn("window.dynamic_title=true", launch)
        self.assertNotIn("dynamic_title=false", launch)

    def test_new_session_never_inherits_the_launcher_directories_in_path(self):
        launcher = str(self.harness.bin)
        poison = f"{launcher}:{self.harness.root}/.local/bin:{self.harness.fake}:/usr/bin:/bin"
        calls = self.harness.run("alacritty-tmux-session",
                                 extra_env={"PATH": poison, "HOME": str(self.harness.root),
                                            "WSCTL_BIN_DIR": "/poison/bin"})
        self.assertTrue(self.temporary_session(calls), calls)
        seen = (self.harness.root / "calls.log.path").read_text().splitlines()
        self.assertEqual(seen, [f"{self.harness.fake}:/usr/bin:/bin"])
        # The user's own entry that follows stays; an unrelated directory is untouched.
        calls = self.harness.run("alacritty-tmux-session",
                                 extra_env={"PATH": f"/poison/bin:/opt/tools:{self.harness.fake}:/usr/bin:/bin",
                                            "WSCTL_BIN_DIR": "/poison/bin"})
        seen = (self.harness.root / "calls.log.path").read_text().splitlines()
        self.assertEqual(seen, [f"/opt/tools:{self.harness.fake}:/usr/bin:/bin"])

    def test_explicit_arguments_pass_through_unchanged(self):
        calls = self.harness.run("alacritty-wsctl", "--title", "x", pending=False)
        self.assertEqual(calls, ["alacritty --title x"])


if __name__ == "__main__":
    unittest.main()
