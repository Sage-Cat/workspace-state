import unittest
from unittest.mock import patch

from workspace_state import restore, tmux_names
from workspace_state.util import CommandError


class TmuxRestoreOrderTests(unittest.TestCase):
    def test_creation_appends_after_previous_live_pane_and_rebalances_space(self):
        window = {"panes": [{"index": index, "cwd": "/tmp"} for index in range(1, 6)],
                  "layout": "saved-layout"}
        with patch.object(restore, "run") as run, patch.object(
            restore, "_split_saved_pane", side_effect=["%11", "%12", "%13", "%14"],
        ) as split, patch.object(restore, "_finish_window") as finish:
            targets = restore._create_window_panes("@4", window, "%10", dry_run=False)
        self.assertEqual([call.args[0] for call in split.call_args_list], ["%10", "%11", "%12", "%13"])
        self.assertEqual(targets, {index: f"%{index + 9}" for index in range(1, 6)})
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["tmux", "set-option", "-w", "-t", "@4", "pane-base-index", "1"], commands)
        self.assertEqual(commands.count(["tmux", "select-layout", "-t", "@4", "tiled"]), 4)
        finish.assert_called_once_with("@4", window, targets, dry_run=False)

    def test_creation_dry_run_does_not_mutate_names_layout_or_indexes(self):
        window = {"panes": [{"index": index, "cwd": "/tmp"} for index in range(5)]}
        with patch.object(restore, "run") as run:
            targets = restore._create_window_panes("@4", window, "%10", dry_run=True)
        self.assertEqual(set(targets), set(range(5)))
        run.assert_not_called()

    def test_new_window_creation_and_final_rename_preserve_literal_name(self):
        name = "tab #{pane_id} #(printf expanded) ;"
        window = {"index": 3, "name": name, "panes": [{"index": 0, "cwd": "/tmp"}]}
        with patch.object(restore, "run", side_effect=["@4\n", "%10\n", ""]) as run, patch.object(
            restore, "_create_window_panes",
        ), patch.object(restore, "read_window_names", return_value={"name": "startup app name"}):
            self.assertEqual(restore._create_window("work", window, dry_run=False), "@4")
        escaped = tmux_names.literal_tmux_argument(name, expands_formats=True)
        creation = run.call_args_list[0].args[0]
        self.assertEqual(creation[creation.index("-n") + 1], escaped)
        self.assertEqual(run.call_args_list[-1].args[0], ["tmux", "rename-window", "-t", "@4", escaped])

    def test_first_session_window_preserves_literal_name(self):
        name = "first #{pane_id};"
        session = {"name": "work", "windows": [{"index": 0, "name": name,
                   "panes": [{"index": 0, "cwd": "/tmp"}]}]}

        def execute(command, **kwargs):
            if command[1] == "display-message":
                return {"#{window_id}": "@4\n", "#{window_index}": "0\n", "#{pane_id}": "%10\n"}[command[-1]]
            return ""

        with patch.object(restore, "run", side_effect=execute) as run, patch.object(
            restore, "_tmux_exists", return_value=False,
        ), patch.object(restore, "_tagged_restore_session", return_value=None), patch.object(
            restore, "_create_window_panes",
        ), patch.object(restore, "read_window_names", return_value={"name": name}):
            restore.recreate_tmux(session)
        creation = run.call_args_list[0].args[0]
        self.assertEqual(creation[creation.index("-n") + 1],
                         tmux_names.literal_tmux_argument(name, expands_formats=True))

    def test_matching_creation_name_keeps_literal_tabs_and_newlines(self):
        name = "tab\tname\nwith delimiters"
        with patch.object(restore, "read_window_names", return_value={"name": name}), patch.object(restore, "run") as run:
            restore._finish_created_window_name("@4", {"name": name})
        run.assert_not_called()

    def test_runtime_window_names_are_read_outside_delimited_pane_rows(self):
        name = "tab\tname\nwith delimiters"
        row = "0\t@4\t@4\t1\t%10\t100\t/tmp\tsh\n"
        with patch.object(restore, "run", return_value=row), patch.object(
            restore, "read_window_names", return_value={"name": name, "automatic_rename": False},
        ):
            state = restore._tmux_state("work")
        self.assertEqual(state[0]["name"], name)
        self.assertEqual(state[0]["panes"][1]["id"], "%10")

    def naming_recipe(self):
        saved = {"name": "work", "windows": [{"index": 0, "name": "tab", "panes": [
            {"index": 0, "id": "%10", "cwd": "/tmp", "label": "A"},
            {"index": 1, "id": "%11", "cwd": "/tmp", "label": "B"},
        ]}]}
        state = {0: {"id": "@4", "name": "tab", "panes": {
            0: {"id": "%20", "pid": 100, "cwd": "/tmp", "command": "sh"},
            1: {"id": "%21", "pid": 101, "cwd": "/tmp", "command": "sh"},
        }}}
        return saved, state

    def test_same_count_unanchored_panes_refuse_before_any_name_or_layout_mutation(self):
        saved, state = self.naming_recipe()
        with patch.object(restore, "run", return_value="") as run, patch.object(
            restore, "restore_pane_names",
        ) as names, self.assertRaisesRegex(CommandError, "ambiguous saved naming identity"):
            restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
        names.assert_not_called()
        self.assertTrue(all(call.args[0][1] == "show-options" for call in run.call_args_list))

    def test_fresh_restore_markers_bind_new_ids_to_saved_names(self):
        saved, state = self.naming_recipe()
        fingerprint = restore._session_fingerprint(saved)
        anchors = {"%20": f"{fingerprint}:0:0", "%21": f"{fingerprint}:0:1"}

        def execute(command, **kwargs):
            return anchors[command[command.index("-t") + 1]] + "\n" if command[1] == "show-options" else ""

        with patch.object(restore, "run", side_effect=execute), patch.object(restore, "restore_pane_names") as names:
            restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
        self.assertEqual([(call.args[0], call.args[2]["label"]) for call in names.call_args_list],
                         [("%20", "A"), ("%21", "B")])

    def test_swapped_restore_markers_refuse_same_count_mapping(self):
        saved, state = self.naming_recipe()
        fingerprint = restore._session_fingerprint(saved)
        with patch.object(restore, "run", return_value=f"{fingerprint}:0:1\n") as run, patch.object(
            restore, "restore_pane_names",
        ) as names, self.assertRaisesRegex(CommandError, "ambiguous saved naming identity"):
            restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
        names.assert_not_called()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][1], "show-options")

    def test_saved_ids_are_trusted_only_in_the_exact_server_session_and_boot(self):
        for same_boot in (True, False):
            with self.subTest(same_boot=same_boot):
                saved, state = self.naming_recipe()
                identity = {"server_pid": "123", "server_start_tick": "456", "session_id": "$7", "boot_id": "boot-a"}
                saved["tmux_identity"] = identity
                state[0]["panes"][0]["id"] = "%10"
                state[0]["panes"][1]["id"] = "%11"
                current_identity = identity if same_boot else {**identity, "boot_id": "boot-b"}
                with patch.object(restore, "tmux_runtime_identity", return_value=current_identity), patch.object(
                    restore, "run", return_value="",
                ) as run, patch.object(restore, "restore_pane_names") as names:
                    if same_boot:
                        restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
                        self.assertEqual(names.call_count, 2)
                        self.assertFalse(any(call.args[0][1] == "show-options" for call in run.call_args_list))
                    else:
                        with self.assertRaisesRegex(CommandError, "ambiguous saved naming identity"):
                            restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
                        names.assert_not_called()

    def test_exact_unique_codex_identities_anchor_saved_names(self):
        saved, state = self.naming_recipe()
        for pane, identity in zip(saved["windows"][0]["panes"], ("thread-a", "thread-b")):
            pane["codex"] = {"session_id": identity}
        with patch.object(restore, "_live_codex_ids", return_value={(0, 0): "thread-a", (0, 1): "thread-b"}), patch.object(
            restore, "run", return_value="",
        ), patch.object(restore, "restore_pane_names") as names:
            restore._reconcile_tmux("work", saved, state, dry_run=False, repair_processes=False)
        self.assertEqual(names.call_count, 2)

    def test_window_rename_policy_restores_captured_boolean(self):
        for policy in (True, False):
            with self.subTest(policy=policy), patch.object(restore, "run") as run, patch.object(
                restore, "read_window_names", return_value={"name": "tab", "automatic_rename": policy},
            ):
                restore._restore_window_rename_policy("@4", {"automatic_rename": policy})
                run.assert_called_once_with(["tmux", "set-option", "-w", "-t", "@4", "automatic-rename",
                                             "on" if policy else "off"])

    def test_window_rename_policy_refuses_invalid_value_and_reports_failed_verification(self):
        with patch.object(restore, "run") as run, self.assertRaisesRegex(CommandError, "Invalid saved"):
            restore._restore_window_rename_policy("@4", {"automatic_rename": "off"})
        run.assert_not_called()
        with patch.object(restore, "run"), patch.object(
            restore, "read_window_names", return_value={"automatic_rename": True},
        ), self.assertRaisesRegex(CommandError, "rename policy did not persist"):
            restore._restore_window_rename_policy("@4", {"automatic_rename": False})


if __name__ == "__main__":
    unittest.main()
