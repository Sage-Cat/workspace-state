from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from workspace_state import capture, codex_resume, restore


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


class CodexIdentityGuardTests(unittest.TestCase):
    def _process(self, root, pid, command, *, parent=0, comm="codex", ticks=10,
                 group=None, terminal_session=456, foreground=None,
                 tty="/dev/pts/1", tty_number=1):
        process = root / str(pid)
        process.mkdir(exist_ok=True)
        fields = ["S", str(parent), str(group or pid), str(terminal_session),
                  str(tty_number), str(foreground or group or pid), *(["0"] * 13), str(ticks)]
        (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (process / "comm").write_text(comm + "\n")
        (process / "cmdline").write_bytes(command)
        (process / "fd").mkdir(exist_ok=True)
        (process / "fd" / "0").symlink_to(tty)

    def _capture(self, owned, command, *, comm="codex"):
        with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
            root = Path(temporary)
            self._process(root, 456, command, comm=comm)
            stack.enter_context(patch.object(capture, "_open_rollout_sessions", return_value=owned))
            return capture.codex_for_pane(456, "/work", proc_root=root)

    def _pane(self, root):
        self._process(root, 456, b"zsh\0", comm="zsh", foreground=789)
        self._process(root, 789, b"codex\0", parent=456, group=789)

    def _rollout(self, root, pid, identity, descriptor=1):
        sessions = root / "sessions"
        sessions.mkdir(exist_ok=True)
        rollout = sessions / f"rollout-2000-01-01T00-00-00-{identity}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": identity, "session_id": identity, "source": "cli"}}) + "\n")
        (root / str(pid) / "fd" / str(descriptor)).symlink_to(rollout)

    def test_later_dedicated_codex_owner_is_not_hidden_by_first_client(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pane(root)
            self._process(root, 790, b"codex\0", parent=789, group=789)
            self._rollout(root, 790, FIRST)
            self.assertEqual(capture.codex_for_pane(456, "/work", proc_root=root),
                             {"pid": 790, "session_id": FIRST, "confidence": "open-rollout"})

    def test_shared_daemon_rollouts_cannot_identify_the_interactive_client(self):
        for identities in ({FIRST}, {FIRST, SECOND}):
            with self.subTest(identities=identities), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._pane(root)
                # Even misleading inherited TTY/ancestry must not turn an
                # app-server's global descriptors into client ownership.
                self._process(root, 790, b"codex\0app-server\0--listen\0unix:///server\0",
                              parent=789, group=789)
                for descriptor, identity in enumerate(identities, 1):
                    self._rollout(root, 790, identity, descriptor)
                result = capture.codex_for_pane(456, "/work", proc_root=root)
                self.assertIsNone(result["session_id"])
                self.assertEqual(result["confidence"], "unknown")

    def test_noninteractive_helper_children_cannot_supply_an_identity(self):
        for role in ("exec", "daemon", "mcp-server", "debug"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._pane(root)
                self._process(root, 790, f"codex\0{role}\0{SECOND}\0".encode(), parent=789, group=789)
                self._rollout(root, 790, SECOND)
                self.assertIsNone(capture.codex_for_pane(456, "/work", proc_root=root)["session_id"])

    def test_conflicting_dedicated_owners_remain_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pane(root)
            self._process(root, 790, b"codex\0", parent=789, group=789)
            self._rollout(root, 789, FIRST)
            self._rollout(root, 790, SECOND)
            result = capture.codex_for_pane(456, "/work", proc_root=root)
            self.assertIsNone(result["session_id"])
            self.assertEqual(result["confidence"], "conflicting-rollouts")

    def test_resume_and_wrapper_must_agree_when_no_rollout_is_owned(self):
        for resume, confidence in ((FIRST, "command-line"), (SECOND, "conflicting-identities")):
            with self.subTest(resume=resume), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                self._process(root, 456, f"python3\0-m\0workspace_state.codex_resume\0{FIRST}\0".encode(), comm="python3")
                self._process(root, 789, f"codex\0resume\0{resume}\0".encode(), parent=456, group=456)
                result = capture.codex_for_pane(456, "/work", proc_root=root)
                self.assertEqual(result["confidence"], confidence)
                self.assertEqual(result["session_id"], FIRST if resume == FIRST else None)

    def test_different_terminal_background_and_unrelated_processes_are_rejected(self):
        for kind in ("other-terminal", "background", "unrelated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
                root = Path(temporary)
                self._process(root, 456, b"zsh\0", comm="zsh", foreground=789)
                self._process(root, 789, b"codex\0", parent=999 if kind == "unrelated" else 456,
                              group=888 if kind == "background" else 789, foreground=789,
                              tty="/dev/pts/2" if kind == "other-terminal" else "/dev/pts/1",
                              tty_number=2 if kind == "other-terminal" else 1)
                self._rollout(root, 789, FIRST)
                stack.enter_context(patch.object(capture, "_proc_children", return_value=[789]))
                self.assertIsNone(capture.codex_for_pane(456, "/work", proc_root=root))

    def test_pid_reuse_during_ownership_read_cannot_supply_a_uuid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pane(root)
            self._rollout(root, 789, FIRST)
            read_owned = capture._open_rollout_sessions

            def replaced_process(pid, *, proc_root):
                owned = read_owned(pid, proc_root=proc_root)
                stat = proc_root / str(pid) / "stat"
                fields = stat.read_text().split()
                fields[-1] = "999"
                stat.write_text(" ".join(fields))
                return owned

            with patch.object(capture, "_open_rollout_sessions", side_effect=replaced_process):
                self.assertIsNone(capture.codex_for_pane(456, "/work", proc_root=root))

    def test_rollout_switch_during_capture_cannot_supply_a_stale_uuid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._pane(root)
            self._rollout(root, 789, FIRST)
            second = root / "sessions" / f"rollout-2000-01-01T00-00-00-{SECOND}.jsonl"
            second.write_text(json.dumps({"type": "session_meta", "payload": {
                "id": SECOND, "session_id": SECOND, "source": "cli"}}) + "\n")
            read_owned = capture._open_rollout_sessions

            def switched_rollout(pid, *, proc_root):
                owned = read_owned(pid, proc_root=proc_root)
                descriptor = proc_root / str(pid) / "fd" / "1"
                descriptor.unlink()
                descriptor.symlink_to(second)
                return owned

            with patch.object(capture, "_open_rollout_sessions", side_effect=switched_rollout):
                self.assertIsNone(capture.codex_for_pane(456, "/work", proc_root=root))

    def test_stale_descendant_graph_cycles_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._process(root, 456, b"zsh\0", comm="zsh", parent=789)
            self._process(root, 789, b"codex\0", parent=456)
            self.assertEqual(capture._proc_children(456, proc_root=root), [789])

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

    def test_fresh_client_without_exact_identity_stays_unknown(self):
        result = self._capture(set(), b"codex")
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["confidence"], "unknown")
        self.assertEqual(result["start_ticks"], "10")
        self.assertEqual(result["tty"], "/dev/pts/1")

    def test_prompt_words_are_not_treated_as_command_roles(self):
        for word in ("exec", "review", "daemon"):
            for command in (f"codex\0resume\0{FIRST}\0{word}\0",
                            f"codex\0--\0{word}\0"):
                with self.subTest(command=command):
                    result = self._capture({FIRST}, command.encode())
                    self.assertEqual(result["session_id"], FIRST)
                    self.assertEqual(result["confidence"], "open-rollout")

    def test_known_options_do_not_hide_noninteractive_role(self):
        self.assertIsNone(self._capture({FIRST}, b"codex\0--config\0key=value\0app-server\0"))
        result = self._capture({FIRST}, b"codex\0--model=synthetic\0--\0exec\0")
        self.assertEqual(result["session_id"], FIRST)

    def test_unknown_option_grammar_stays_unknown_even_with_rollout(self):
        result = self._capture({FIRST}, b"codex\0--future-option\0exec\0")
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["confidence"], "unknown")

    def test_unambiguous_rollout_and_explicit_resume_identity_still_win(self):
        result = self._capture({FIRST}, f"codex\0resume\0{SECOND}".encode())
        self.assertEqual(result["session_id"], FIRST)
        self.assertEqual(result["confidence"], "open-rollout")
        result = self._capture(set(), f"codex\0resume\0{FIRST}".encode())
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
                result = self._capture(set(), argv.encode())
                self.assertIsNone(result["session_id"] if result is not None else None)

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
