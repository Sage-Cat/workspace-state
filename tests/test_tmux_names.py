import unittest
from unittest.mock import patch

from workspace_state import capture, restore, storage, tmux_names
from workspace_state.util import CommandError


class TmuxNamesTests(unittest.TestCase):
    def test_read_preserves_whitespace_unicode_and_literal_semicolon(self):
        title = "  Учёба; #{pane_id}  "
        with patch.object(tmux_names, "run", side_effect=[f"%7\t@3\n{title}\n", "label;\n"]) as run:
            result = tmux_names.read_pane_names("%7", window_id="@3")
        self.assertEqual(result, {"title": title, "label": "label;"})
        self.assertEqual(run.call_args_list[0].args[0], [
            "tmux", "display-message", "-p", "-t", "%7",
            "#{pane_id}\t#{window_id}\n#{pane_title}",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "tmux", "show-options", "-p", "-qv", "-t", "%7", "@pane_label",
        ])

    def test_read_rejects_invalid_or_moved_identity(self):
        with self.assertRaisesRegex(CommandError, "explicit stable"):
            tmux_names.read_pane_names("7")
        for output in ("%8\t@3\nTitle\n", "%7\t@4\nTitle\n", "%7\nTitle\n"):
            with self.subTest(output=output), patch.object(tmux_names, "run", return_value=output):
                with self.assertRaisesRegex(CommandError, "moved or disappeared"):
                    tmux_names.read_pane_names("%7", window_id="@3")

    def test_validation_rejects_nul_oversize_and_wrong_types(self):
        for pane in (
            {"title": "bad\0title"},
            {"label": "x" * 16385},
            {"title": 4},
            {"label": []},
            {"allow_rename": "off"},
        ):
            with self.subTest(pane=pane), self.assertRaises(ValueError):
                tmux_names.validate_pane_names(pane)
        tmux_names.validate_pane_names({"title": "", "label": ""})
        tmux_names.validate_pane_names({"title": "title", "label": None})

    def test_read_window_names_preserves_delimiters_and_effective_rename_policy(self):
        name = "tab\tname\n#{literal};"
        with patch.object(tmux_names, "run", side_effect=[f"@3\n{name}\n", "off\n"]) as run:
            self.assertEqual(tmux_names.read_window_names("@3"), {"name": name, "automatic_rename": False})
        self.assertEqual(run.call_args_list[-1].args[0], [
            "tmux", "show-options", "-A", "-w", "-qv", "-t", "@3", "automatic-rename",
        ])

    def test_read_window_names_rejects_missing_or_wrong_identity(self):
        with self.assertRaisesRegex(CommandError, "explicit stable"):
            tmux_names.read_window_names("3")
        for output in ("@4\nwrong window\n", "@3"):
            with patch.object(tmux_names, "run", return_value=output), self.assertRaises(CommandError):
                tmux_names.read_window_names("@3")

    def test_effective_pane_rename_policy_is_validated(self):
        for value, expected in (("on\n", True), ("off\n", False), ("1\n", True), ("0\n", False)):
            with patch.object(tmux_names, "run", return_value=value):
                self.assertEqual(tmux_names.read_pane_rename_policy("%7"), expected)
        with patch.object(tmux_names, "run", return_value="unknown\n"), self.assertRaisesRegex(CommandError, "invalid rename policy"):
            tmux_names.read_pane_rename_policy("%7")

    def test_runtime_identity_includes_boot_and_process_start_tick(self):
        boot = "12345678-1234-1234-1234-123456789abc"
        stat = "321 (tmux: server) " + " ".join(["S", *(["0"] * 18), "456"])
        with patch.object(tmux_names, "run", return_value="321\t$7\n"), patch.object(
            tmux_names.Path, "read_text", side_effect=[stat, boot + "\n"],
        ):
            self.assertEqual(tmux_names.tmux_runtime_identity("work"), {
                "server_pid": "321", "server_start_tick": "456", "session_id": "$7", "boot_id": boot,
            })
        with patch.object(tmux_names, "run", return_value="321\t$7\n"), patch.object(
            tmux_names.Path, "read_text", side_effect=OSError("gone"),
        ):
            self.assertIsNone(tmux_names.tmux_runtime_identity("work"))

    def test_restore_can_apply_only_a_captured_pane_rename_policy(self):
        with patch.object(tmux_names, "read_pane_names", return_value={"title": "app", "label": None}), patch.object(
            tmux_names, "read_pane_rename_policy", side_effect=[True, False],
        ), patch.object(tmux_names, "run") as run:
            tmux_names.restore_pane_names("%7", "@3", {"allow_rename": False})
        run.assert_called_once_with(["tmux", "set-option", "-p", "-t", "%7", "allow-rename", "off"])

    def test_restore_reports_pane_rename_policy_that_does_not_persist(self):
        with patch.object(tmux_names, "read_pane_names", return_value={"title": "app", "label": None}), patch.object(
            tmux_names, "read_pane_rename_policy", return_value=True,
        ), patch.object(tmux_names, "run"), self.assertRaisesRegex(CommandError, "rename policy did not persist"):
            tmux_names.restore_pane_names("%7", "@3", {"allow_rename": False})

    def test_legacy_pane_metadata_is_a_noop(self):
        with patch.object(tmux_names, "run") as run, patch.object(
            tmux_names, "read_pane_names",
        ) as read:
            tmux_names.restore_pane_names("%7", "@3", {"index": 0, "cwd": "/tmp"})
        run.assert_not_called()
        read.assert_not_called()

    def test_restore_maps_new_stable_pane_id_and_escapes_title_literals(self):
        pane = {"title": "old", "label": "Name#{pane_id};"}
        with patch.object(tmux_names, "read_pane_names", side_effect=[
            {"title": "old", "label": "old-label"},
            {"title": "Name#{pane_id};", "label": "Name#{pane_id};"},
        ]), patch.object(tmux_names, "run") as run:
            tmux_names.restore_pane_names("%9", "@4", pane)
        self.assertEqual(run.call_args_list[0].args[0], [
            "tmux", "set-option", "-p", "-t", "%9", "@pane_label", "Name#{pane_id}\\;",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "tmux", "select-pane", "-t", "%9", "-T", "Name##{pane_id}\\;",
        ])
        self.assertNotIn("%old", str(run.call_args_list))

    def test_literal_arguments_escape_formats_only_for_expanding_commands(self):
        value = "name #{pane_id} #(printf literal) ##;"
        self.assertEqual(tmux_names.literal_tmux_argument(value), value[:-1] + r"\;")
        self.assertEqual(tmux_names.literal_tmux_argument(value, expands_formats=True),
                         value[:-1].replace("#", "##") + r"\;")

    def test_empty_label_keeps_distinction_and_null_unsets_local_option(self):
        for output, expected in (("", None), ("\n", ""), (" \n", " ")):
            with patch.object(tmux_names, "run", side_effect=["%9\t@4\nold\n", output]):
                self.assertEqual(tmux_names.read_pane_names("%9")["label"], expected)
        with patch.object(tmux_names, "read_pane_names", side_effect=[
            {"title": "old", "label": "set"},
            {"title": "saved title", "label": None},
        ]), patch.object(tmux_names, "run") as run:
            tmux_names.restore_pane_names("%9", "@4", {"title": "saved title", "label": None})
        self.assertEqual(run.call_args_list[0].args[0], [
            "tmux", "set-option", "-p", "-u", "-t", "%9", "@pane_label",
        ])
        self.assertEqual(run.call_args_list[1].args[0], [
            "tmux", "select-pane", "-t", "%9", "-T", "saved title",
        ])

    def test_empty_label_is_restored_as_empty_not_unset(self):
        with patch.object(tmux_names, "read_pane_names", side_effect=[
            {"title": "old", "label": None}, {"title": "old", "label": ""},
        ]), patch.object(tmux_names, "run") as run:
            tmux_names.restore_pane_names("%9", "@4", {"label": ""})
        run.assert_called_once_with(["tmux", "set-option", "-p", "-t", "%9", "@pane_label", ""])

    def test_label_verification_failure_is_visible(self):
        with patch.object(tmux_names, "read_pane_names", return_value={"title": "x", "label": None}), patch.object(
            tmux_names, "run",
        ), self.assertRaisesRegex(CommandError, "label did not persist"):
            tmux_names.restore_pane_names("%9", "@4", {"label": "saved"})

    def test_application_title_updates_do_not_override_explicit_label_or_fail_restore(self):
        with patch.object(tmux_names, "read_pane_names", side_effect=[
            {"title": "app", "label": None}, {"title": "new app title", "label": "saved"},
        ]), patch.object(tmux_names, "run"):
            tmux_names.restore_pane_names("%9", "@4", {"label": "saved", "title": "old app title"})

    def test_finish_window_dry_run_does_not_change_names_or_selection(self):
        with patch.object(restore, "restore_pane_names") as names, patch.object(restore, "run") as run:
            restore._finish_window("@1", {"panes": [{"index": 0, "label": "saved", "active": True}]}, {0: "%1"}, dry_run=True)
        names.assert_not_called()
        run.assert_not_called()

    def test_ambiguous_extra_panes_are_rejected_before_any_mutation(self):
        saved = {"windows": [{"index": 0, "name": "DEBUG_WINDOW", "panes": [
            {"index": 0, "label": "saved one"}, {"index": 1, "label": "saved two"},
        ]}]}
        state = {0: {"id": "@1", "name": "DEBUG_WINDOW", "panes": {0: {}, 1: {}, 2: {}}}}
        with patch.object(restore, "run") as run, patch.object(restore, "restore_pane_names") as names:
            with self.assertRaisesRegex(CommandError, "additional live panes"):
                restore._reconcile_tmux("session", saved, state, dry_run=False, repair_processes=False)
        run.assert_not_called()
        names.assert_not_called()

    def test_snapshot_validation_checks_optional_pane_names(self):
        pane = {"index": 0, "cwd": "/tmp", "command": "sh"}
        snapshot = {"sessions": [{"name": "s", "windows": [{"index": 0, "name": "w", "layout": "l", "panes": [pane]}]}]}
        storage.validate(snapshot)
        pane.update(label=None, title="saved")
        storage.validate(snapshot)
        pane["label"] = 7
        with self.assertRaisesRegex(ValueError, "pane label"):
            storage.validate(snapshot)

    def test_capture_records_name_error_without_dropping_pane(self):
        pane_row = ["s", "0", "@2", "layout", "1", "0", "%1", "11", "/tmp", "bash", "1", "@2"]
        with patch.object(capture, "capture_shell", return_value={"available": True, "windows": [], "workspaces": []}), patch.object(
            capture, "workspace_names", return_value=[],
        ), patch.object(capture, "_rows", side_effect=[[], [pane_row]]), patch.object(
            capture, "_alacritty_ancestor", return_value=None,
        ), patch.object(capture, "codex_for_pane", return_value=None), patch.object(
            capture, "read_window_names", return_value={"name": "win", "automatic_rename": False},
        ), patch.object(capture, "tmux_runtime_identity", return_value=None), patch.object(
            capture, "read_pane_names", side_effect=CommandError("tmux unavailable"),
        ):
            result = capture.capture()
        pane = result["sessions"][0]["windows"][0]["panes"][0]
        self.assertNotIn("title", pane)
        self.assertNotIn("label", pane)
        self.assertIn("pane names for %1: tmux unavailable", result["capture_errors"]["tmux"])

    def test_restore_uses_mapped_new_pane_id_instead_of_snapshot_id(self):
        pane = {"index": 0, "id": "%old", "title": "title"}
        window = {"index": 0, "panes": [pane]}
        with patch.object(restore, "restore_pane_names") as restore_names:
            restore._restore_window_pane_names("@7", window, {0: "%new"})
        restore_names.assert_called_once_with("%new", "@7", pane)


if __name__ == "__main__":
    unittest.main()
