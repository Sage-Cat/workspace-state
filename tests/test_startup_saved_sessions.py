from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from workspace_state import storage
from workspace_state.cli import _restore_items

SRC = Path(__file__).resolve().parents[1] / "src"


def fixture() -> dict:
    return {
        "version": 2,
        "terminals": [{"session": "Workspace-1", "placement": None}],
        "sessions": [
            {"name": "Workspace-1", "windows": []},
            {"name": "main", "windows": []},
            {"name": "Workspace-4", "windows": []},
        ],
    }


class SavedSessionsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data = Path(directory.name)

    def snapshot_path(self) -> Path:
        path = self.data / "workspace-state" / "snapshots" / "current.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def run_command(self):
        environment = {**os.environ, "XDG_DATA_HOME": str(self.data), "PYTHONPATH": str(SRC)}
        return subprocess.run([sys.executable, "-m", "workspace_state.startup", "saved-sessions"],
                              env=environment, capture_output=True, text=True, timeout=30)

    def test_helper_matches_the_sessions_restore_recreates(self):
        snapshot = fixture()
        self.assertEqual(set(storage.saved_session_names(snapshot)),
                         {item["name"] for item in _restore_items(snapshot)})
        self.assertEqual(storage.saved_session_names({"sessions": [{"name": ""}, {"name": 3}, {}]}), [])

    def test_prints_saved_names(self):
        self.snapshot_path().write_text(json.dumps(fixture()))
        result = self.run_command()
        self.assertEqual((result.returncode, result.stdout.split()),
                         (0, ["Workspace-1", "main", "Workspace-4"]), result.stderr)

    def test_missing_snapshot_prints_nothing(self):
        result = self.run_command()
        self.assertEqual((result.returncode, result.stdout), (0, ""), result.stderr)

    def test_malformed_snapshot_fails_without_output(self):
        self.snapshot_path().write_text("{not json")
        result = self.run_command()
        self.assertEqual((result.returncode, result.stdout), (1, ""))


if __name__ == "__main__":
    unittest.main()
