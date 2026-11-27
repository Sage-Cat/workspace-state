"""Tabs created by real restore code paths on an isolated tmux socket.

`scripts/run-tests.py` points TMUX_TMPDIR at a private directory, so the bare
`tmux` of the product reaches only this server, never the desktop's.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import agent_deck, restore, tmux_server

TAB = "0123456789abcdef0123456789abcdef"
CLEAN_PATH = "/usr/local/bin:/usr/bin:/bin"
POISON = "/poison/bin"
SCOPE = {"profile": "work", "group": ""}


def pane(index, **extra):
    return {"index": index, "cwd": "/tmp", "command": "sh", **extra}


def window(index, name, *panes):
    return {"index": index, "name": name, "layout": "", "panes": list(panes)}


@unittest.skipUnless(shutil.which("tmux"), "requires tmux")
class RealTabTests(unittest.TestCase):
    def setUp(self):
        if "wsctl-unit-tests-" not in os.environ.get("TMUX_TMPDIR", ""):
            self.skipTest("the isolated tmux socket directory is not set")
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name).resolve()
        (self.home / ".tmux.conf").write_text("set -g renumber-windows on\nset -g base-index 1\n")
        environment = {
            "PATH": f"{POISON}:{CLEAN_PATH}", "WSCTL_BIN_DIR": POISON, "SHELL": "/bin/sh",
            "HOME": str(self.home), "PYTHONPATH": f"/poison/src:{os.environ.get('PYTHONPATH', '')}",
            "TMUX_TMPDIR": os.environ["TMUX_TMPDIR"],
        }
        patcher = patch.dict(os.environ, environment)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.kill_server)
        self.manager = {"PATH": CLEAN_PATH, "HOME": str(self.home), "LANG": "C.UTF-8"}
        for target, value in (("workspace_state.tmux_server._manager_environment", self.manager),
                              ("workspace_state.tmux_server.shutil.which", None)):
            started = patch(target, return_value=value)
            started.start()
            self.addCleanup(started.stop)
        # No tmux client may ever address a server outside the isolated directory.
        real_run = subprocess.run
        isolated = os.environ["TMUX_TMPDIR"]

        def guarded(command, *args, **kwargs):
            if command and os.path.basename(str(command[0])) == "tmux":
                env = kwargs.get("env")
                if env is not None and env.get("TMUX_TMPDIR") != isolated:
                    raise AssertionError(f"tmux client outside the isolated socket directory: {command}")
            return real_run(command, *args, **kwargs)

        guard = patch.object(subprocess, "run", guarded)
        guard.start()
        self.addCleanup(guard.stop)
        self.dashboard = patch.object(agent_deck, "dashboard_command", return_value=["/bin/sleep", "300"])
        self.dashboard.start()
        self.addCleanup(self.dashboard.stop)
        self.stderr = patch("sys.stderr")
        self.stderr.start()
        self.addCleanup(self.stderr.stop)

    def tmux(self, *argv):
        return subprocess.run(["tmux", *argv], capture_output=True, text=True, timeout=10,
                              env={**os.environ, "PATH": CLEAN_PATH}).stdout

    def kill_server(self):
        subprocess.run(["tmux", "kill-server"], capture_output=True, timeout=10,
                       env={**os.environ, "PATH": CLEAN_PATH})
        deadline = time.monotonic() + 5
        while self.tmux("list-sessions"):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        time.sleep(0.1)

    def start_server(self):
        started = subprocess.run(["tmux", "-f", "/dev/null", "new-session", "-d", "-s", "keeper", "sleep 300"],
                       capture_output=True, text=True, timeout=10, env={
                           "PATH": CLEAN_PATH, "HOME": str(self.home),
                           "TMUX_TMPDIR": os.environ["TMUX_TMPDIR"]})
        self.assertEqual(started.returncode, 0, started.stderr)
        self.tmux("set-option", "-g", "renumber-windows", "on")

    def windows(self, session):
        return self.tmux("list-windows", "-t", f"={session}:", "-F", "#{window_index}:#{window_name}").splitlines()

    def panes(self, session):
        rows = self.tmux("list-panes", "-s", "-t", f"={session}:", "-F",
                         "#{window_index}.#{pane_index} #{pane_pid} #{pane_current_command}").splitlines()
        return [(row.split()[0], int(row.split()[1]), row.split()[2]) for row in rows]

    @staticmethod
    def environ(pid):
        raw = (Path("/proc") / str(pid) / "environ").read_bytes().split(b"\0")
        return dict(item.decode().split("=", 1) for item in raw if b"=" in item)

    def wait_for_panes(self, session, count):
        deadline = time.monotonic() + 5
        while len(self.panes(session)) < count:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.05)

    def saved(self, indexes):
        first, second, third, fourth = indexes
        return {"name": "deck", "windows": [
            window(first, "alpha", pane(0), pane(1, tab_id=TAB)),
            window(second, "DECK", pane(0, agent_deck=dict(SCOPE))),
            window(third, "beta", pane(0)),
            window(fourth, "gamma", pane(0, tab_id="f" * 32)),
        ]}

    def check_panes(self, session):
        found = self.panes(session)
        self.assertEqual(len(found), 5)
        for position, pid, _command in found:
            path = self.environ(pid)["PATH"]
            self.assertNotIn(POISON, path, position)
            self.assertEqual(path, CLEAN_PATH, position)
            self.assertNotIn(POISON, self.environ(pid).get("PYTHONPATH", ""), position)
        return {position: (pid, command) for position, pid, command in found}

    def test_every_new_tab_gets_the_clean_environment_whatever_the_callers_path(self):
        for label, manager in (("manager", True), ("fallback", False)):
            for running in (False, True):
                with self.subTest(source=label, server_running=running):
                    self.kill_server()
                    if running:
                        self.start_server()
                    name = f"deck-{label}-{int(running)}"
                    saved = {**self.saved((1, 2, 4, 5)), "name": name}
                    patched = patch("workspace_state.tmux_server._manager_environment",
                                    return_value=dict(self.manager) if manager else None)
                    with patched:
                        actual, _actions = restore.recreate_tmux(saved)
                    self.assertEqual(actual, name)
                    self.wait_for_panes(name, 5)
                    self.check_panes(name)

    def test_fallback_without_launcher_variable_still_drops_the_package_directory(self):
        package = tmux_server._package_bin_dir()
        with patch.dict(os.environ, {"PATH": f"{package}:{CLEAN_PATH}"}), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("WSCTL_BIN_DIR")
            with patch("workspace_state.tmux_server._manager_environment", return_value=None):
                environment = tmux_server.server_environment(quiet=True)
        self.assertEqual(environment["PATH"], CLEAN_PATH)

    def test_dashboard_tab_keeps_its_saved_index_and_name_with_renumbering_on(self):
        for indexes in ((1, 2, 4, 5), (3, 5, 6, 9), (2, 4, 5, 8)):
            with self.subTest(indexes=indexes):
                self.kill_server()
                actual, _actions = restore.recreate_tmux({**self.saved(indexes), "name": "gaps"})
                self.assertEqual(actual, "gaps")
                self.wait_for_panes("gaps", 5)
                self.assertEqual(self.windows("gaps"), [
                    f"{indexes[0]}:alpha", f"{indexes[1]}:DECK", f"{indexes[2]}:beta", f"{indexes[3]}:gamma"])
                # Nothing was created or killed afterwards that could renumber.
                time.sleep(0.2)
                self.assertEqual([row.split(":")[0] for row in self.windows("gaps")],
                                 [str(index) for index in indexes])

    def test_dashboard_starts_in_the_home_directory_not_the_saved_one(self):
        restore.recreate_tmux({**self.saved((1, 2, 3, 4)), "name": "home"})
        self.wait_for_panes("home", 5)
        pid = next(pid for position, pid, _ in self.panes("home") if position == "2.0")
        self.assertEqual((Path("/proc") / str(pid) / "cwd").readlink(), self.home)
        other = next(pid for position, pid, _ in self.panes("home") if position == "3.0")
        self.assertEqual((Path("/proc") / str(other) / "cwd").readlink(), Path("/tmp"))

    def test_shell_with_a_tab_id_is_the_pane_process_and_the_id_stays_in_that_pane(self):
        restore.recreate_tmux({**self.saved((1, 2, 3, 4)), "name": "tabs"})
        self.wait_for_panes("tabs", 5)
        found = self.check_panes("tabs")
        pid, command = found["1.1"]
        self.assertEqual(command, "sh")
        self.assertEqual((Path("/proc") / str(pid) / "cmdline").read_bytes(), b"/bin/sh\0")
        self.assertEqual(self.environ(pid)["WSCTL_TAB_ID"], TAB)
        self.assertEqual(self.environ(found["4.0"][0])["WSCTL_TAB_ID"], "f" * 32)
        for position in ("1.0", "3.0"):
            self.assertNotIn("WSCTL_TAB_ID", self.environ(found[position][0]), position)
        # The ID never reaches the session, so a later tab cannot inherit it.
        self.assertNotIn("WSCTL_TAB_ID", self.tmux("show-environment", "-t", "=tabs:"))
        self.assertNotIn("WSCTL_TAB_ID", self.tmux("show-environment", "-g"))
        created = self.tmux("new-window", "-d", "-t", "=tabs:8", "-P", "-F", "#{pane_pid}", "/bin/sh").strip()
        self.assertNotIn("WSCTL_TAB_ID", self.environ(int(created)))

    def test_an_invalid_saved_tab_id_is_not_passed_on(self):
        saved = {"name": "bad", "windows": [window(1, "one", pane(0, tab_id="NOT-AN-ID"))]}
        restore.recreate_tmux(saved)
        self.wait_for_panes("bad", 1)
        (_position, pid, _command), = self.panes("bad")
        self.assertNotIn("WSCTL_TAB_ID", self.environ(pid))

    def test_split_and_additional_windows_of_a_live_session_use_the_clean_environment(self):
        restore.recreate_tmux({"name": "live", "windows": [window(1, "one", pane(0))]})
        self.wait_for_panes("live", 1)
        state = restore._tmux_state("live")
        saved = {"name": "live", "windows": [window(1, "one", pane(0), pane(1, tab_id=TAB)),
                                              window(3, "three", pane(0))]}
        restore._reconcile_tmux("live", saved, state, dry_run=False)
        self.wait_for_panes("live", 3)
        for _position, pid, _command in self.panes("live"):
            self.assertEqual(self.environ(pid)["PATH"], CLEAN_PATH)
        self.assertEqual(self.windows("live"), ["1:one", "3:three"])


if __name__ == "__main__":
    unittest.main()
