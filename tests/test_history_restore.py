import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import checkpoint, cli, storage


class HistoryRestoreTests(unittest.TestCase):
    def test_restore_selects_verified_generation_without_writing_current(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            checkpoint.keep_generation(directory, {"version": 6, "sessions": [], "created_at": "earlier"})
            path, = directory.glob("*.json")
            args = cli.parser().parse_args(["restore", "vscode", "--from-history", str(path), "--dry-run"])
            with patch.object(cli, "_startup_lock"), patch.object(cli, "load") as canonical, \
                 patch("workspace_state.workspace_switch.suppress", side_effect=lambda data: data), \
                 patch.object(cli, "_restore", return_value={k: 0 for k in (*cli.CATEGORIES, "codex_total")}) as restore:
                self.assertEqual(cli.cmd_restore(args), 0)
            self.assertEqual(restore.call_args.args[0]["created_at"], "earlier")
            canonical.assert_not_called()
            path.write_text('{"sessions":[]}')
            with self.assertRaisesRegex(ValueError, "hash"):
                storage.load_history(path)

    def test_place_only_requires_one_existing_client_and_preserves_tmux(self):
        args = cli.parser().parse_args(["restore", "terminals", "--workspace", "Lab", "--session", "work",
                                        "--terminal-placement-only"])
        client = {"placement": {"id": 12}, "alacritty_pid": 42}
        saved = {"placement": {"workspace_name": "Lab"}, "launch_terminal": True}
        with patch.object(cli, "_filtered_terminal_items", return_value=[saved]), \
             patch.object(cli, "_live_terminal_clients", return_value={"work": [client]}) as clients, \
             patch.object(cli, "place_terminal") as place, patch.object(cli, "recreate_tmux") as recreate:
            place.return_value.success = True
            place.return_value.request_id = None
            self.assertEqual(cli._place_attached_terminal({}, args), 0)
            place.assert_called_once_with(client, saved["placement"], dry_run=False, require_local=True)
            clients.return_value = {"work": [client, client]}
            with self.assertRaisesRegex(cli.CommandError, "one attached"):
                cli._place_attached_terminal({}, args)
            recreate.assert_not_called()
