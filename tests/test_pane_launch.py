"""How a saved pane is launched: tab ID argv, dashboard directory and shell fallback."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from workspace_state import agent_deck, restore

SCOPE = {"profile": "work", "group": "project"}


def pane(index=0, **extra):
    return {"index": index, "id": f"%{index}", "cwd": "/tmp", "command": "zsh", **extra}


def window(index, name, *panes):
    return {"index": index, "name": name, "layout": "", "panes": list(panes) or [pane()]}


TAB = "0123456789abcdef0123456789abcdef"


class PaneLaunchTests(unittest.TestCase):
    def setUp(self):
        patcher = patch("os.environ", {"HOME": "/home/example", "SHELL": "/bin/zsh"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_plain_shell_with_a_tab_id_starts_as_argv_with_the_id_in_its_environment(self):
        self.assertEqual(restore._pane_tmux_command(pane(tab_id=TAB)),
                         ["env", f"WSCTL_TAB_ID={TAB}", "/bin/zsh"])

    def test_without_a_valid_tab_id_the_shell_starts_as_before(self):
        for extra in ({}, {"tab_id": "NOT-AN-ID"}, {"tab_id": TAB.upper()}, {"tab_id": TAB + "0"}, {"tab_id": 5}):
            with self.subTest(extra=extra):
                self.assertEqual(restore._pane_tmux_command(pane(**extra)), ["exec /bin/zsh"])

    def test_dashboards_and_conversations_never_get_a_tab_id(self):
        uuid = "00000000-0000-4000-8000-000000000001"
        with patch.object(agent_deck, "dashboard_command", return_value=["agent-deck"]):
            self.assertEqual(restore._pane_tmux_command(pane(agent_deck=dict(SCOPE), tab_id=TAB)),
                             ["agent-deck; exec /bin/zsh"])
        command = restore._pane_tmux_command(pane(codex={"session_id": uuid}, tab_id=TAB))
        self.assertEqual(len(command), 1)
        self.assertNotIn("WSCTL_TAB_ID", command[0])
        self.assertIn(uuid, command[0])

    def test_creation_commands_pass_the_argv_and_never_set_the_id_on_the_session(self):
        tab = pane(1, tab_id=TAB)
        with patch.object(restore, "run", return_value="%9\n") as run:
            restore._split_saved_pane("%1", tab, dry_run=False, placeholder="x")
            restore._create_window("work", {"index": 4, "name": "w", "panes": [pane(0, tab_id=TAB)]}, dry_run=True)
        split = run.call_args.args[0]
        self.assertEqual(split[-3:], ["env", f"WSCTL_TAB_ID={TAB}", "/bin/zsh"])
        self.assertNotIn("-e", split)
        with patch.object(restore, "run", side_effect=["@4\n", "%10\n", "", ""]) as run, patch.object(
                restore, "_create_window_panes"), patch.object(restore, "_finish_created_window_name"):
            restore._create_window("work", {"index": 4, "name": "w", "panes": [pane(0, tab_id=TAB)]}, dry_run=False)
        created = run.call_args_list[0].args[0]
        self.assertEqual(created[:5], ["tmux", "new-window", "-d", "-t", "=work:4"])
        self.assertEqual(created[-3:], ["env", f"WSCTL_TAB_ID={TAB}", "/bin/zsh"])
        self.assertNotIn("-e", created)

    def test_new_session_for_the_first_window_passes_the_argv_without_dash_e(self):
        saved = {"name": "work", "windows": [window(2, "w", pane(0, tab_id=TAB))]}
        calls = []
        with patch.object(restore, "_tmux_exists", return_value=False), patch.object(
                restore, "_tagged_restore_session", return_value=None), patch.object(
                restore.tmux_server, "new_session", side_effect=calls.append), patch.object(
                restore, "run", side_effect=lambda argv: "2\n" if "#{window_index}" in argv else "@1\n"), patch.object(
                restore, "_create_window_panes"), patch.object(
                restore, "_finish_created_window_name"):
            restore.recreate_tmux(saved)
        arguments = calls[0]
        self.assertEqual(arguments[-3:], ["env", f"WSCTL_TAB_ID={TAB}", "/bin/zsh"])
        self.assertNotIn("-e", arguments)

    def test_a_missing_agent_deck_executable_leaves_a_shell_not_a_failed_session(self):
        recipe = pane(agent_deck=dict(SCOPE))
        with patch.object(agent_deck, "dashboard_command", side_effect=agent_deck.AgentDeckError("missing")):
            self.assertEqual(restore._pane_tmux_command(recipe), ["exec /bin/zsh"])
            self.assertEqual(restore._pane_command(recipe), ["/bin/zsh"])

    def test_a_dashboard_pane_starts_in_home_and_others_keep_their_directory(self):
        self.assertEqual(restore._pane_cwd(pane(agent_deck=dict(SCOPE))), "/home/example")
        self.assertEqual(restore._pane_cwd(pane()), "/tmp")


if __name__ == "__main__":
    unittest.main()
