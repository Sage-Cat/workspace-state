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
