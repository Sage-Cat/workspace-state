from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import claude_resume, operations, restore
from workspace_state.conversations import identity, saved_ids


CODEX = "11111111-1111-4111-8111-111111111111"
CLAUDE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"


def recipe():
    return {"name": "main", "windows": [{
        "index": 1, "name": "current", "layout": "tiled", "panes": [
            {"index": 0, "cwd": "/work", "codex": {"session_id": CODEX}},
            {"index": 1, "cwd": "/work", "conversation": {
                "provider": "claude", "session_id": CLAUDE, "persisted": True,
            }},
        ],
    }]}


class MixedConversationRestoreTests(unittest.TestCase):
    def test_missing_claude_resumes_once_without_relaunching_live_codex(self):
        saved = recipe()
        state = {1: {"id": "@1", "name": "current", "panes": {
            0: {"id": "%1", "pid": 10, "cwd": "/work", "command": "codex"},
        }}}
        with patch.object(restore, "_tmux_exists", return_value=True), \
                patch.object(restore, "_tmux_state", return_value=state), \
                patch.object(restore, "_tagged_restore_session", return_value=None), \
                patch.object(restore, "_restore_fingerprint", return_value=None), \
                patch.object(restore, "_live_codex_ids", return_value={(1, 0): CODEX}), \
                patch("workspace_state.claude_identity.claude_for_pane", return_value=None), \
                patch.object(restore, "_create_window", return_value="@2") as create:
            actual, actions = restore.recreate_tmux(saved)
        self.assertEqual(actual, "main")
        self.assertEqual(create.call_count, 1)
        recovered = create.call_args.args[1]
        self.assertEqual(len(recovered["panes"]), 1)
        self.assertEqual(identity(recovered["panes"][0]), ("claude", CLAUDE))
        self.assertTrue(any("missing conversation" in action for action in actions))

    def test_repeated_restore_never_copies_the_session_or_starts_a_conversation_twice(self):
        saved = recipe()
        saved["windows"].append({"index": 2, "name": "extra", "layout": "tiled", "panes": [
            {"index": 0, "cwd": "/work", "codex": {"session_id": OTHER}}]})
        # First pass already ran: Codex and Claude (a carried window) are live;
        # the conversation in window 2 is still missing.
        state = {1: {"id": "@1", "name": "current", "panes": {
            0: {"id": "%1", "pid": 10, "cwd": "/work", "command": "codex"},
            1: {"id": "%2", "pid": 11, "cwd": "/work", "command": "claude"}}}}
        live = {(1, 0): "codex:" + CODEX, (1, 1): "claude:" + CLAUDE}

        def second_restore(live_ids):
            with patch.object(restore, "_tmux_exists", side_effect=lambda name: name == "main"), \
                    patch.object(restore, "_tmux_state", return_value=state), \
                    patch.object(restore, "_tagged_restore_session", return_value=None), \
                    patch.object(restore, "_restore_fingerprint", return_value=None), \
                    patch.object(restore, "_live_conversation_ids", return_value=live_ids), \
                    patch.object(restore.conversation_guard, "live_locations", return_value={}), \
                    patch.object(restore, "run", side_effect=AssertionError("tmux must not be mutated")), \
                    patch.object(restore, "_reconcile_tmux", return_value=[]), \
                    patch.object(restore, "_create_window", return_value="@3") as create:
                return restore.recreate_tmux(saved), create

        (name, actions), create = second_restore(live)
        self.assertEqual(name, "main")
        self.assertFalse(any("-wsctl" in action for action in actions))
        self.assertEqual(create.call_count, 1)
        started = [identity(pane) for pane in create.call_args.args[1]["panes"]]
        self.assertEqual(started, [("codex", OTHER)])  # neither live conversation starts again
        # A third pass, with every conversation live, creates nothing at all.
        (name, actions), create = second_restore(live | {(2, 0): "codex:" + OTHER})
        self.assertEqual(name, "main")
        create.assert_not_called()

    def test_unpersisted_claude_cannot_become_a_restore_recipe(self):
        saved = copy.deepcopy(recipe())
        saved["windows"][0]["panes"][1]["conversation"]["persisted"] = False
        self.assertEqual(saved_ids(saved), {(1, 0): "codex:" + CODEX})
        self.assertEqual(restore._pane_command(saved["windows"][0]["panes"][1])[-1],
                         restore.os.environ.get("SHELL", "/bin/sh"))

    def test_claude_launch_refuses_cancelled_startup_before_exec(self):
        context = operations.OperationContext.create("synthetic-login", "startup")
        with patch.object(claude_resume, "decode_startup_context", return_value=context), \
                patch.object(operations, "current", return_value=None), \
                patch.object(operations.OperationContext, "check", side_effect=operations.StartupAborted("stopped")), \
                patch.object(claude_resume.os, "execvp") as launch:
            self.assertEqual(claude_resume.main([CLAUDE, "--startup-context=synthetic"]), 0)
        launch.assert_not_called()

    def test_codex_only_fingerprint_remains_compatible(self):
        saved = {"name": "main", "windows": [{
            "index": 1, "name": "current", "panes": [{
                "index": 0, "cwd": "/work", "codex": {"session_id": CODEX},
            }],
        }]}
        old = {"name": "main", "windows": [{"index": 1, "name": "current",
               "panes": [{"index": 0, "cwd": "/work", "codex": CODEX}]}]}
        expected = hashlib.sha256(json.dumps(old, sort_keys=True,
                                           separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(restore._session_fingerprint(saved), expected)

    def test_native_claude_wrapper_normalizes_scoped_context_before_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "python3"
            stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            stub.chmod(0o755)
            wrapper = Path(__file__).resolve().parents[1] / "bin/wsctl-claude-resume"
            argument = "--startup-context=synthetic"
            result = subprocess.run([wrapper, argument, CLAUDE], text=True,
                                    capture_output=True, check=False,
                                    env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ["-m", "workspace_state.claude_resume", CLAUDE, argument])


if __name__ == "__main__":
    unittest.main()
