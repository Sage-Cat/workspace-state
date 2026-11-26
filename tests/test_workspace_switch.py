from __future__ import annotations

import copy
import io
import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from workspace_state import cli, desktop, operations, storage, workspace_switch as switch
from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderCount, ProviderItemResult

NAMES = ["Alpha", "Beta"]


def native(wid, workspace=0, *, pid=None):
    return {"id": wid, "pid": pid or wid * 100, "workspace": workspace, "sticky": False,
            "close_blocker": None, "monitor": 0, "state": "normal", "title": "Nemo"}


def desktop_state(*windows, epoch="current", dynamic=False, blockers=0):
    return {"available": True, "enable_epoch": epoch, "capabilities": ["close_window"],
            "dynamic_workspaces": dynamic, "screen_unavailable": False, "monitors": [],
            "workspaces": [{"index": index, "name": name, "named": True,
                            "blocking_windows": blockers if index == 0 else 0}
                           for index, name in enumerate(NAMES)],
            "windows": list(windows)}


def recipe(*windows):
    placements = [{"_native_id": wid, "location": f"folder-{wid}",
                   "placement": {"workspace": index, "workspace_name": NAMES[index]}}
                  for wid, index in windows]
    return {"version": 7, "name": "current", "created_at": "fictional",
            "desktop": {"workspace_names": NAMES}, "sessions": [], "terminals": [],
            "file_manager": {"provider": "nemo", "windows": placements}}


class WorkspaceSwitchTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(operations.bind, operations.current())
        operations.bind(None)
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"XDG_DATA_HOME": temp, "XDG_RUNTIME_DIR": temp}))
        self.enterContext(patch.object(cli, "_graphical_session_ending", return_value=None))
        self.capture = self.enterContext(patch.object(desktop, "capture_shell", side_effect=lambda: self.shell))
        self.close = self.enterContext(patch.object(desktop, "close_window"))
        self.out = self.enterContext(redirect_stdout(io.StringIO()))
        self.err = self.enterContext(redirect_stderr(io.StringIO()))
        self.shell = desktop_state()

    def list_rows(self):
        self.out.seek(0)
        self.out.truncate()
        self.assertEqual(cli.main(["workspace", "list", "--json"]), 0)
        return json.loads(self.out.getvalue())

    def apply(self, action):
        return cli.main(["workspace", action, "--", "Alpha"])

    def archive(self):
        return json.loads(switch._path("Alpha").read_text())

    def test_empty_workspace_switch_is_durable_and_serialized(self):
        self.assertEqual(self.apply("off"), 0)
        path = switch._path("Alpha")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
        old = path.read_bytes()
        self.shell = desktop_state(epoch="new")
        self.assertEqual(self.list_rows()["workspaces"][0]["state"], "off")
        self.assertEqual(self.apply("off"), 0)
        self.assertEqual(path.read_bytes(), old)
        with switch._switch_lock():
            self.assertTrue(self.list_rows()["busy"])
            self.assertEqual(self.apply("on"), 2)
        self.assertEqual(self.apply("on"), 0)
        self.assertEqual(self.list_rows()["workspaces"][0]["state"], "on")
        self.close.assert_not_called()

    def test_inventory_refuses_unsupported_and_dialog_windows_before_closure(self):
        self.shell = desktop_state(native(14), blockers=1)
        self.assertEqual(self.apply("off"), 2)
        self.assertFalse(switch._path("Alpha").exists())
        self.shell = desktop_state({**native(14), "close_blocker": "unsupported_client"})
        self.assertEqual(self.apply("off"), 2)
        self.close.assert_not_called()

    def test_missed_native_window_fails_closed(self):
        self.shell = desktop_state(native(14))
        with patch.object(cli, "_capture_all", return_value=recipe()):
            self.assertEqual(self.apply("off"), 2)
        self.assertIn("no unique, complete provider capture", self.err.getvalue())
        self.assertFalse(switch._path("Alpha").exists())
        self.close.assert_not_called()

    def test_new_window_after_capture_leaves_archive_and_windows_untouched(self):
        self.shell = desktop_state(native(14))
        with patch.object(switch, "_snapshot", side_effect=self.add_window_after_capture):
            self.assertEqual(self.apply("off"), 2)
        self.assertIn("changed during capture", self.err.getvalue())
        self.assertFalse(switch._path("Alpha").exists())
        self.close.assert_not_called()

    def add_window_after_capture(self, *_):
        self.shell["windows"].append(native(15))
        return recipe((14, 0))

    def test_archive_precedes_close_and_shared_pid_remains(self):
        self.shell = desktop_state(native(14), native(15, 1, pid=1400))

        def close(wid, *, workspace, name, pid, epoch):
            self.assertEqual(self.archive()["phase"], "closing")
            self.assertEqual((wid, workspace, name, pid, epoch), (14, 0, "Alpha", 1400, "current"))
            self.shell["windows"].pop(0)

        self.close.side_effect = close
        with patch.object(switch, "_snapshot", return_value=recipe((14, 0))):
            self.assertEqual(self.apply("off"), 0)
        self.assertEqual(self.archive()["phase"], "off")
        self.assertEqual(switch._native_ids(self.archive()["snapshot"]), [14])
        self.assertEqual([win["id"] for win in self.shell["windows"]], [15])

    def test_partial_close_retry_preserves_complete_snapshot(self):
        self.shell = desktop_state(native(14), native(15))

        def close(wid, **_):
            if wid == 15:
                raise desktop.CommandError("client still open")
            self.shell["windows"].pop(0)

        self.close.side_effect = close
        with patch.object(switch, "_snapshot", return_value=recipe((14, 0), (15, 0))):
            self.assertEqual(self.apply("off"), 3)
        self.assertEqual(self.list_rows()["workspaces"][0]["target"], "off")
        before = self.archive()
        self.close.side_effect = lambda *_a, **_k: self.shell["windows"].clear()
        with patch.object(switch, "_snapshot", return_value=recipe((15, 0))):
            self.assertEqual(self.apply("off"), 0)
        self.assertEqual(self.archive()["snapshot"], before["snapshot"])
        self.assertEqual(self.archive()["token"], before["token"])

    def test_partial_on_requires_positive_evidence_and_keeps_archive(self):
        self.shell = desktop_state(native(14))
        self.close.side_effect = lambda *_a, **_k: self.shell["windows"].clear()
        with patch.object(switch, "_snapshot", return_value=recipe((14, 0))):
            self.assertEqual(self.apply("off"), 0)
        before = self.archive()
        self.shell = desktop_state(epoch="after-login")
        counts = {**dict.fromkeys(cli.CATEGORIES, 0), "codex_verified": 1}
        with patch.object(cli, "_restore", return_value=counts):
            self.assertEqual(self.apply("on"), 3)
        self.assertEqual(self.archive()["snapshot"], before["snapshot"])
        self.assertEqual(self.list_rows()["workspaces"][0]["target"], "on")

        def restore(snapshot, args):
            self.assertEqual(switch._native_ids(snapshot), [14])
            self.assertTrue(args.workspace_switch)
            self.assertEqual(args.switch_token, before["token"])
            self.shell["windows"].append(native(24))
            verified = PhaseEvidence(EvidenceState.VERIFIED)
            evidence = ProviderItemResult("nemo", "folder", verified, verified, verified)
            return {**counts, "file-manager": ProviderCount(1, [evidence])}

        with patch.object(cli, "_restore", side_effect=restore), patch.object(storage, "save") as save:
            self.assertEqual(self.apply("on"), 0)
            self.assertNotIn("_native_id", save.call_args.args[0]["file_manager"]["windows"][0])
        self.assertEqual(self.archive()["phase"], "on")

    def test_login_projection_keeps_shared_terminal_and_other_windows(self):
        self.assertEqual(self.apply("off"), 0)
        saved = recipe((14, 0), (15, 1))
        saved["sessions"] = [{"name": name} for name in ("shared", "hidden", "detached")]
        saved["terminals"] = [{"session": name, "placement": {"workspace": workspace}}
                              for name, workspace in (("shared", 0), ("shared", 1), ("hidden", 0))]
        original = copy.deepcopy(saved)
        filtered = switch.suppress(saved)
        self.assertEqual(saved, original)
        self.assertEqual([item["name"] for item in filtered["sessions"]], ["shared", "detached"])
        self.assertEqual(filtered["terminals"], [saved["terminals"][1]])
        self.assertEqual(filtered["file_manager"]["windows"], [saved["file_manager"]["windows"][1]])

    def test_dynamic_duplicate_and_default_slots_are_never_archived(self):
        self.shell = desktop_state(dynamic=True)
        self.assertEqual(self.list_rows()["workspaces"], [])
        self.assertEqual(self.apply("off"), 2)
        self.shell["dynamic_workspaces"] = False
        self.shell["workspaces"][1]["name"] = "alpha"
        self.assertEqual(self.list_rows()["workspaces"], [])
        self.shell["workspaces"][1].update(name="Workspace 2", named=False)
        self.assertEqual([row["name"] for row in self.list_rows()["workspaces"]], ["Alpha"])
        self.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
