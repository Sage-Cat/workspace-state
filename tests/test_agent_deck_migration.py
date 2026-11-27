import argparse
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import agent_deck_migration as migration, cli, tmux_names


SCOPE = {"profile": "work", "group": "project/subgroup"}
SESSION = "example"
WORKSPACE = "Example"
PLACEHOLDER = "Legacy"
WINDOW = "Agent Deck"
SAVED = {
    "name": SESSION, "windows": [{"name": PLACEHOLDER, "index": 0, "panes": [{"index": 0}]}],
}
SNAPSHOT = {
    "sessions": [SAVED], "terminals": [{"session": SESSION, "placement": {"workspace": 0}}],
    "desktop": {"workspace_names": [WORKSPACE]},
}


def desktop():
    return {
        "available": True, "enable_epoch": "sample-epoch", "active_workspace": 0,
        "workspaces": [{"name": WORKSPACE, "index": 1}],
        "windows": [{"id": 789, "pid": 456, "workspace": 1, "sticky": False, "active": False}],
    }


class DashboardDispatchTest(unittest.TestCase):
    def options(self, **changes):
        return argparse.Namespace(**{
            "category": "terminals", "workspace": WORKSPACE, "session": [SESSION],
            "deck_dashboard": tuple(SCOPE.values()), "deck_window": None,
            "deck_placeholder": PLACEHOLDER, "select": False, "dry_run": True,
            **changes,
        })

    def test_dispatch_only_opens_the_dashboard_for_a_saved_placement(self):
        with patch.object(migration, "restore", return_value="verified") as restore, \
                patch("workspace_state.agent_deck_restore.restore") as conversations, \
                patch.object(cli, "_restore_terminal_layout") as layouts, redirect_stdout(StringIO()):
            counts = cli._restore(SNAPSHOT, self.options())
        self.assertEqual(counts["terminals"], 1)
        self.assertEqual(counts["codex_total"], 0)
        restore.assert_called_once_with(
            {**SAVED, "placement": {"workspace": 0, "workspace_name": WORKSPACE}, "launch_terminal": True},
            WORKSPACE, SCOPE, name=WINDOW, placeholder=PLACEHOLDER, dry_run=True,
        )
        layouts.assert_not_called()
        conversations.assert_not_called()

    def test_a_dashboard_away_from_its_saved_index_is_an_attention_item_of_the_manual_result(self):
        for message, attention in ((migration.Outcome("ok"), False),
                                   (migration.Outcome("moved", "tab 0 in use"), True)):
            with self.subTest(attention=attention), \
                    patch.object(migration, "restore", return_value=message), \
                    patch.object(cli, "_restore_terminal_layout"), redirect_stdout(StringIO()):
                counts = cli._restore(SNAPSHOT, self.options(dry_run=False))
            results = counts["terminals"].results
            self.assertEqual(bool(results), attention)
            if attention:
                self.assertFalse(results[0].success)
                self.assertEqual(results[0].attention, ("tab 0 in use",))
                self.assertEqual(results[0].item_id, f"{SESSION}:dashboard")

    def test_the_manual_command_exits_3_for_a_misplaced_dashboard_and_0_otherwise(self):
        from workspace_state import placement_guard
        for message, expected in ((migration.Outcome("ok"), 0), (migration.Outcome("moved", "tab 0 in use"), 3)):
            with self.subTest(expected=expected), \
                    patch.object(migration, "restore", return_value=message), \
                    patch.object(cli, "_restore_terminal_layout"), patch.object(cli, "load", return_value=SNAPSHOT), \
                    patch.object(cli, "_startup_lock"), patch.object(cli, "_checkpoint_login"), \
                    patch.object(cli, "_seen_directory"), patch.object(placement_guard, "record"), \
                    patch("workspace_state.workspace_switch.suppress", side_effect=lambda value: value), \
                    redirect_stdout(StringIO()), patch("sys.stderr", new_callable=StringIO) as stderr:
                code = cli.cmd_restore(self.options(dry_run=False, from_history=None, no_place=False,
                                                    maximize=False, terminal_placement_only=False))
            self.assertEqual(code, expected)
            self.assertEqual("need attention" in stderr.getvalue(), expected == 3)

    def test_a_preview_has_no_result_item_even_with_attention(self):
        with patch.object(migration, "restore", return_value=migration.Outcome("moved", "tab 0 in use")), \
                patch.object(cli, "_restore_terminal_layout"), redirect_stdout(StringIO()):
            counts = cli._restore(SNAPSHOT, self.options(dry_run=True))
        self.assertEqual(counts["terminals"].results, ())

    def test_requires_explicit_scope_and_exact_saved_terminal_placement(self):
        for state, changes in (
            (SNAPSHOT, {"category": None}),
            (SNAPSHOT, {"category": "browsers"}),
            (SNAPSHOT, {"deck_dashboard": None}),
            (SNAPSHOT, {"session": [SESSION[:3]]}),
            (SNAPSHOT, {"session": [SESSION, "other"]}),
            (SNAPSHOT, {"workspace": WORKSPACE.lower()}),
            (SNAPSHOT, {"select": True}),
            ({**SNAPSHOT, "terminals": []}, {}),
            ({**SNAPSHOT, "terminals": SNAPSHOT["terminals"] * 2}, {}),
            ({**SNAPSHOT, "terminals": [{"session": SESSION, "placement": {"workspace": 99}}]},
             {"workspace": "Unassigned"}),
        ):
            with self.subTest(changes=changes), patch.object(migration, "restore") as restore:
                with self.assertRaises(ValueError):
                    cli._restore(state, self.options(**changes))
                restore.assert_not_called()

    def test_terminal_binding_distinguishes_active_space_and_active_window(self):
        shell = desktop()
        window = shell["windows"][0]
        for state, expected in (
            (shell, True),
            ({**shell, "active_workspace": 1}, False),
            ({**shell, "windows": [{**window, "active": True}]}, False),
        ):
            with self.subTest(state=state), \
                    patch.object(migration, "capture_shell", return_value=state), \
                    patch.object(migration, "run", return_value="$1|123\n"), \
                    patch.object(migration, "_alacritty_ancestor", return_value=456):
                self.assertEqual(migration._terminal_binding("$1", WORKSPACE).background, expected)

    def test_refuses_ambiguous_or_rebound_live_terminal(self):
        shell = desktop()
        window = shell["windows"][0]
        for state, clients, alacritty in (
            (shell, "", 456),
            (shell, "$1|123\n$1|124\n", 456),
            (shell, "$2|123\n", 456),
            (shell, "$1|123\n", None),
            ({**shell, "windows": [{**window, "workspace": 0}]}, "$1|123\n", 456),
            ({**shell, "windows": [{**window, "sticky": True}]}, "$1|123\n", 456),
            ({**shell, "windows": [window, {**window, "id": 700}]}, "$1|123\n", 456),
            ({**shell, "workspaces": shell["workspaces"] * 2}, "$1|123\n", 456),
        ):
            with self.subTest(state=state, clients=clients), \
                    patch.object(migration, "capture_shell", return_value=state), \
                    patch.object(migration, "run", return_value=clients), \
                    patch.object(migration, "_alacritty_ancestor", return_value=alacritty):
                with self.assertRaises(RuntimeError):
                    migration._terminal_binding("$1", WORKSPACE)


@unittest.skipUnless(shutil.which("tmux") and shutil.which("bash"), "requires tmux and bash")
class DashboardTmuxTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.socket = str(Path(tmp.name) / "tmux")
        self.command = [sys.executable, "-c", "import time; time.sleep(60)", "-p", "work", "-g", "project/subgroup"]
        self.scopes = {tuple(self.command): SCOPE}
        self.tmux(["tmux", "new-session", "-d", "-s", SESSION, "-n", PLACEHOLDER,
                   "env", "PS1=$ ", "bash", "--noprofile", "--norc", "-i"])
        self.addCleanup(self.tmux, ["tmux", "kill-server"])
        self.clients = "$0|123\n"
        self.desktop = desktop()
        self.enterContext(patch.object(migration, "run", side_effect=self.tmux))
        self.enterContext(patch.object(migration, "_alacritty_ancestor", return_value=456))
        self.enterContext(patch.object(migration, "capture_shell", side_effect=lambda **_: self.desktop))
        self.enterContext(patch.object(migration, "tmux_runtime_identity", side_effect=self.identity))
        self.observe = self.enterContext(patch.object(migration.agent_deck, "dashboard", side_effect=self.dashboard))
        self.launch = self.enterContext(patch.object(migration.agent_deck, "dashboard_command",
                                                    return_value=self.command))
        self.initial = self.active_pane()
        self.wait_for_prompt()

    def tmux(self, argv):
        if argv[1] == "list-clients":
            self.assertEqual(argv[2:4], ["-t", "$0"])
            return self.clients
        return subprocess.check_output(["tmux", "-f", "/dev/null", "-S", self.socket, *argv[1:]], text=True)

    def identity(self, name):
        return tmux_names.tmux_runtime_identity(name, command_runner=self.tmux)

    def dashboard(self, pid):
        # The dashboard may run under the shell that keeps its tab alive.
        try:
            children = (Path("/proc") / str(pid) / "task" / str(pid) / "children").read_text().split()
        except OSError:
            children = []
        for candidate in [pid, *map(int, children)]:
            try:
                argv = (Path("/proc") / str(candidate) / "cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            except OSError:
                continue
            if (scope := self.scopes.get(tuple(argv))) is not None:
                return scope
        return None

    def windows(self):
        return self.tmux(["tmux", "list-windows", "-t", f"={SESSION}:", "-F", "#{window_index}:#{window_name}"]).splitlines()

    def active_pane(self):
        return self.tmux(["tmux", "display-message", "-p", "-t", f"={SESSION}:", "#{pane_id}"]).strip()

    def screen(self, pane):
        return self.tmux(["tmux", "capture-pane", "-p", "-t", pane])

    def wait_for_prompt(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.screen(self.initial).partition("\n")[0].strip() == "$":
                return
            time.sleep(0.02)
        self.fail("tmux shell prompt did not appear")

    def run_restore(self, **options):
        return migration.restore(SAVED, WORKSPACE, SCOPE, name=WINDOW, placeholder=PLACEHOLDER, **options)

    def test_empty_background_placeholder_becomes_the_dashboard_in_its_saved_tab(self):
        self.assertIn("Would open", self.run_restore(dry_run=True))
        self.assertEqual(len(migration._panes("$0")), 1)
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}"])

        self.assertIn("in its saved tab", self.run_restore())
        # Same pane, index and neighbours; no window was created or killed.
        self.assertEqual(self.active_pane(), self.initial)
        self.assertEqual(self.windows(), [f"0:{WINDOW}"])
        self.assertEqual([pane.id for pane in migration._panes("$0")], [self.initial])

        self.assertIn("already verified", self.run_restore())
        self.assertEqual(self.windows(), [f"0:{WINDOW}"])
        self.assertEqual(self.launch.call_count, 2)  # Preview and the single actual launch.

    def test_dashboard_in_place_starts_in_the_home_directory_and_survives_its_exit(self):
        self.run_restore()
        cwd = Path(f"/proc/{migration._panes('$0')[0].pid}/cwd").readlink()
        self.assertEqual(cwd, Path(os.environ["HOME"]))
        # The dashboard runs under a shell, so a crash keeps the tab.
        pane = migration._panes("$0")[0]
        for child in (Path("/proc") / str(pane.pid) / "task" / str(pane.pid) / "children").read_text().split():
            os.kill(int(child), 9)
        deadline = time.monotonic() + 3
        while self.dashboard(pane.pid) is not None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertEqual(self.windows(), [f"0:{WINDOW}"])

    def test_foreground_terminal_keeps_its_tab(self):
        self.desktop["active_workspace"] = 1
        self.assertIn("at window 1", self.run_restore())
        self.assertEqual(self.active_pane(), self.initial)
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}", f"1:{WINDOW}"])

    def test_existing_draft_keeps_its_tab_and_input(self):
        self.tmux(["tmux", "send-keys", "-t", self.initial, "-l", "unfinished draft"])
        deadline = time.monotonic() + 3
        while "unfinished draft" not in self.screen(self.initial):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        before = self.screen(self.initial)
        self.assertIn("at window 1", self.run_restore())
        self.assertEqual(self.active_pane(), self.initial)
        self.assertEqual(self.screen(self.initial), before)

    def test_name_used_by_another_tab_is_shared_and_the_dashboard_keeps_its_index(self):
        self.tmux(["tmux", "new-window", "-d", "-t", f"={SESSION}:3", "-n", WINDOW, "sleep", "60"])
        message = self.run_restore()
        self.assertIn("also used by window 3", message)
        self.assertEqual(self.windows(), [f"0:{WINDOW}", f"3:{WINDOW}"])
        self.assertEqual(self.active_pane(), self.initial)
        self.assertEqual(self.launch.call_count, 1)

    def test_owner_layout_with_the_placeholder_named_like_the_dashboard(self):
        message = migration.restore(SAVED, WORKSPACE, SCOPE, name=PLACEHOLDER, placeholder=PLACEHOLDER)
        self.assertIn("in its saved tab", message)
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}"])

    def test_same_name_in_a_foreground_terminal_is_a_second_tab_not_a_failure(self):
        self.desktop["active_workspace"] = 1
        message = migration.restore(SAVED, WORKSPACE, SCOPE, name=PLACEHOLDER, placeholder=PLACEHOLDER)
        self.assertIn("at window 1", message)
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}", f"1:{PLACEHOLDER}"])

    def test_saved_index_gap_is_kept_with_renumbering_on(self):
        self.tmux(["tmux", "set-option", "-g", "renumber-windows", "on"])
        self.tmux(["tmux", "rename-window", "-t", f"={SESSION}:0", "Other"])
        self.tmux(["tmux", "move-window", "-s", f"={SESSION}:0", "-t", f"={SESSION}:5"])
        for index in (1, 2):
            self.tmux(["tmux", "new-window", "-d", "-t", f"={SESSION}:{index}", "-n", f"tab{index}", "sleep", "60"])
        saved = {"name": SESSION, "windows": [{"name": PLACEHOLDER, "index": 4, "panes": [{"index": 0}]}]}
        message = migration.restore(saved, WORKSPACE, SCOPE, name=WINDOW, placeholder=PLACEHOLDER)
        self.assertIn("at window 4", message)
        self.assertEqual(self.windows(), ["1:tab1", "2:tab2", f"4:{WINDOW}", "5:Other"])
        # With the saved index taken, the dashboard goes after the last tab.
        other = {**saved, "windows": [{"name": PLACEHOLDER, "index": 5, "panes": [{"index": 0}]}]}
        scope = {"profile": "work", "group": "other"}
        second = [*self.command, "-x"]
        self.scopes[tuple(second)] = scope
        self.launch.return_value = second
        self.assertIn("at window 6", migration.restore(other, WORKSPACE, scope, name="Second", placeholder=PLACEHOLDER))
        self.assertEqual(self.windows(), ["1:tab1", "2:tab2", f"4:{WINDOW}", "5:Other", "6:Second"])

    def test_new_dashboard_window_survives_the_dashboard_and_keeps_the_other_indexes(self):
        self.tmux(["tmux", "set-option", "-g", "renumber-windows", "on"])
        self.desktop["active_workspace"] = 1
        self.tmux(["tmux", "new-window", "-d", "-t", f"={SESSION}:3", "-n", "later", "sleep", "60"])
        self.assertIn("at window 4", self.run_restore())
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}", "3:later", f"4:{WINDOW}"])
        pane = next(item for item in migration._panes("$0") if item.index == 4)
        for child in (Path("/proc") / str(pane.pid) / "task" / str(pane.pid) / "children").read_text().split():
            os.kill(int(child), 9)
        deadline = time.monotonic() + 3
        while self.dashboard(pane.pid) is not None:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        time.sleep(0.3)
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}", "3:later", f"4:{WINDOW}"])

    def test_a_dashboard_that_cannot_take_its_saved_index_says_so(self):
        self.desktop["active_workspace"] = 1
        message = self.run_restore()
        self.assertIn("ATTENTION", message)
        self.assertIn("saved tab 0", message)
        self.assertIn("window 1", message)
        self.assertIn("saved tab 0", message.attention)

    def test_the_preview_shows_the_same_attention(self):
        self.desktop["active_workspace"] = 1
        preview = self.run_restore(dry_run=True)
        self.assertIn("ATTENTION", preview)
        self.assertIn("saved tab 0", preview.attention)
        self.assertEqual(len(migration._panes("$0")), 1)

    def test_a_placeholder_that_changes_after_the_check_is_never_killed(self):
        original = migration._empty_shell

        def check_then_type(pane):
            blank = original(pane)
            self.tmux(["tmux", "send-keys", "-t", pane.id, "-l", "late input"])
            deadline = time.monotonic() + 3
            while "late input" not in self.screen(pane.id):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.02)
            return blank

        before = migration._panes("$0")[0].pid
        with patch.object(migration, "_empty_shell", side_effect=check_then_type):
            message = self.run_restore()
        self.assertIn("at window 1", message)
        self.assertEqual(migration._panes("$0")[0].pid, before)
        self.assertIn("late input", self.screen(self.initial))
        self.assertEqual(self.windows(), [f"0:{PLACEHOLDER}", f"1:{WINDOW}"])

    def test_dashboard_is_created_with_the_server_environment(self):
        seen = []
        original = migration.run

        def record(argv):
            from workspace_state import tmux_server
            seen.append((argv[1], tmux_server.pane_environment(argv) is not None))
            return original(argv)

        self.desktop["active_workspace"] = 1
        with patch.object(migration, "run", side_effect=record):
            self.run_restore()
        self.assertIn(("new-window", True), seen)
        self.assertIn(("display-message", False), seen)

    def test_detached_and_misplaced_terminals_refuse_to_create_a_window(self):
        self.clients = ""
        with self.assertRaisesRegex(RuntimeError, "Expected one attached client"):
            self.run_restore()
        self.clients = "$0|123\n"
        self.desktop["windows"][0]["workspace"] = 0
        with self.assertRaisesRegex(RuntimeError, "not uniquely placed"):
            self.run_restore()
        self.assertEqual(len(migration._panes("$0")), 1)
        self.launch.assert_not_called()

    def test_rechecks_live_binding_after_start_before_reporting_ready(self):
        def switch_client_on_dashboard_start(pid):
            scope = self.dashboard(pid)
            if scope is not None:
                self.clients = "$0|124\n"
            return scope

        self.desktop["active_workspace"] = 1
        self.observe.side_effect = switch_client_on_dashboard_start
        with self.assertRaisesRegex(RuntimeError, "Alacritty binding changed"):
            self.run_restore()
        self.assertEqual(len(migration._panes("$0")), 2)
        self.assertEqual(self.active_pane(), self.initial)

    def test_an_existing_dashboard_requires_the_live_workspace_binding(self):
        self.desktop["active_workspace"] = 1
        self.assertIn("verified", self.run_restore())
        self.desktop["windows"][0]["workspace"] = 0
        with self.assertRaisesRegex(RuntimeError, "not uniquely placed"):
            self.run_restore()
        self.assertEqual(len(migration._panes("$0")), 2)
        self.launch.assert_called_once()
