from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from workspace_state.codex_directories import _directory_accessible, directory_ready, saved_cwd


class CodexDirectoriesTests(unittest.TestCase):
    def _database(self, root, version, cwd):
        path = root / f"state_{version}.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, cwd TEXT)")
            connection.execute("INSERT INTO threads VALUES (?, ?)", ("exact-session", cwd))
            connection.commit()
        return path

    def test_saved_cwd_uses_highest_numeric_schema_and_exact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._database(root, 9, "/old")
            database = self._database(root, 10, "/saved/work")
            before = database.read_bytes()
            self.assertEqual(saved_cwd("exact-session", root), Path("/saved/work"))
            self.assertIsNone(saved_cwd("another-session", root))
            self.assertEqual(database.read_bytes(), before)
            self.assertEqual(sorted(path.name for path in root.iterdir()), ["state_10.sqlite", "state_9.sqlite"])

    def test_unavailable_or_invalid_state_does_not_fall_back_or_guess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertIsNone(saved_cwd("exact-session", root))
            self.assertEqual(list(root.iterdir()), [])
            self._database(root, 9, "/old")
            (root / "state_10.sqlite").write_text("invalid database")
            self.assertIsNone(saved_cwd("exact-session", root))

    def test_missing_schema_and_invalid_directories_are_unknown(self):
        for cwd in ("", "relative/work", None, "bad\0path"):
            with self.subTest(cwd=cwd), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._database(root, 5, cwd)
                self.assertIsNone(saved_cwd("exact-session", root))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "state_5.sqlite").touch()
            self.assertIsNone(saved_cwd("exact-session", root))

    def test_unmounted_drive_does_not_touch_requested_directory(self):
        home = Path("/home/test")
        with patch("workspace_state.codex_directories.os.path.ismount", return_value=False) as mounted, patch(
            "workspace_state.codex_directories.os.scandir",
        ) as scandir:
            self.assertFalse(_directory_accessible(home / "Drives/pdrive/Work", home))
        mounted.assert_called_once_with(home / "Drives/pdrive")
        scandir.assert_not_called()

    def test_drive_mount_gate_precedes_directory_read(self):
        home = Path("/home/test")
        events = []

        def mounted(path):
            events.append(("mount", path))
            return True

        def unreadable(path):
            events.append(("read", path))
            raise PermissionError()

        with patch("workspace_state.codex_directories.os.path.ismount", side_effect=mounted), patch(
            "workspace_state.codex_directories.os.scandir", side_effect=unreadable,
        ):
            self.assertFalse(_directory_accessible(home / "Drives/pdrive/Work", home))
        self.assertEqual(events, [("mount", home / "Drives/pdrive"), ("read", home / "Drives/pdrive/Work")])

    def test_existing_local_directory_and_missing_path(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(directory_ready(Path(directory)))
            self.assertFalse(directory_ready(Path(directory) / "missing"))
        self.assertFalse(directory_ready(None))
        self.assertFalse(directory_ready("relative/path"))

    def test_stalled_directory_probe_returns_false(self):
        with patch("workspace_state.codex_directories.subprocess.Popen") as popen:
            process = popen.return_value
            process.wait.side_effect = subprocess.TimeoutExpired("probe", 0.1)
            self.assertFalse(directory_ready("/local", timeout=0.1))
        process.kill.assert_called_once_with()
        self.assertEqual([call.kwargs["timeout"] for call in process.wait.call_args_list], [0.1, 0.1])


if __name__ == "__main__":
    unittest.main()
