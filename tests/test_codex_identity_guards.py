from __future__ import annotations

import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from workspace_state import capture, codex_resume, restore


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


class CodexIdentityGuardTests(unittest.TestCase):
    def _capture(self, owned, command, candidates=(), *, comm="codex"):
        with ExitStack() as stack:
            stack.enter_context(patch.object(capture, "_proc_children", return_value=[]))
            stack.enter_context(patch.object(Path, "read_text", return_value=comm + "\n"))
            stack.enter_context(patch.object(Path, "read_bytes", return_value=command))
            stack.enter_context(patch.object(capture, "_open_rollout_sessions", return_value=owned))
            stack.enter_context(patch.object(capture, "_process_start", return_value=100.0))
            stack.enter_context(patch.object(capture, "_session_candidates", return_value=candidates))
            return capture.codex_for_pane(456, "/work")

    def test_conflicting_rollouts_block_command_line_fallback(self):
        result = self._capture({FIRST, SECOND}, f"codex\0resume\0{FIRST}".encode())
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["confidence"], "conflicting-rollouts")

    def test_conflicting_rollouts_block_daemon_prompt_fallback(self):
        with patch.object(codex_resume, "_open_rollout_sessions", return_value={FIRST, SECOND}), patch.object(
            codex_resume, "pane_text", return_value="› Continue\n100% context left",
        ), patch.object(codex_resume, "loaded_thread_ids", return_value={FIRST}) as loaded:
            self.assertFalse(codex_resume.resumed_session(456, FIRST, "%123"))
        loaded.assert_not_called()

    def test_nearby_sessions_in_same_directory_are_not_selected_by_proximity(self):
        result = self._capture(set(), b"codex", [(100.1, FIRST), (102.0, SECOND)])
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["confidence"], "unknown")

    def test_one_distinct_nearby_identity_still_supports_legacy_capture(self):
        result = self._capture(set(), b"codex", [(100.1, FIRST), (102.0, FIRST), (200.0, SECOND)])
        self.assertEqual(result["session_id"], FIRST)
        self.assertEqual(result["confidence"], "start-time")

    def test_unambiguous_rollout_and_explicit_resume_identity_still_win(self):
        result = self._capture({FIRST}, f"codex\0resume\0{SECOND}".encode())
        self.assertEqual(result["session_id"], FIRST)
        self.assertEqual(result["confidence"], "open-rollout")
        result = self._capture(set(), f"codex\0resume\0{FIRST}".encode(), [(100.1, SECOND)])
        self.assertEqual(result["session_id"], FIRST)
        self.assertEqual(result["confidence"], "command-line")

    def test_waiting_restore_wrapper_keeps_exact_identity_for_checkpoint(self):
        result = self._capture(set(), f"python3\0-m\0workspace_state.codex_resume\0{FIRST}\0".encode(), comm="python3")
        self.assertEqual(result, {"pid": 456, "session_id": FIRST, "confidence": "restore-wrapper"})

    def test_arbitrary_python_command_with_uuid_is_not_a_restore_wrapper(self):
        for argv in (f"python3\0other.py\0{FIRST}", f"python3\0-m\0workspace_state.codex_resume\0{FIRST}\0extra"):
            with self.subTest(argv=argv):
                self.assertIsNone(self._capture(set(), argv.encode(), comm="python3"))

    def test_uuid_in_prompt_or_option_is_not_explicit_resume_identity(self):
        for argv in (f"codex\0Explain ticket {FIRST}", f"codex\0resume\0--model\0{FIRST}",
                     f"codex\0exec\0resume\0{FIRST}"):
            with self.subTest(argv=argv):
                self.assertIsNone(self._capture(set(), argv.encode())["session_id"])

    def test_canonical_resume_wrapper_preserves_exact_positional_identity(self):
        result = self._capture(set(), f"codex\0resume\0--no-alt-screen\0{FIRST}\0Mention {SECOND}".encode())
        self.assertEqual(result["session_id"], FIRST)

    def test_waiting_wrapper_cannot_be_verified_by_stale_pane_or_daemon(self):
        state = {1: {"panes": {1: {"pid": 456, "cwd": "/work", "id": "%123"}}}}
        with patch.object(restore, "codex_for_pane", return_value={
            "pid": 456, "session_id": FIRST, "confidence": "restore-wrapper",
        }), patch.object(restore, "resumed_session", return_value=True) as resumed:
            self.assertEqual(restore._live_codex_ids(state), {(1, 1): FIRST})
            self.assertEqual(restore._live_codex_ids(state, require_ready=True), {})
        resumed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
