from __future__ import annotations

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import codex_resume, codex_title


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
FOOTER = "  GPT-6-Astra high · ~/Projects/research-workspac…"


class CodexComposerTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.proc_root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self._process(123, comm="zsh", command=b"zsh\0", group=123)
        self._process(456)
        self.row = 0
        self.column = 2
        self.in_mode = 0
        self.terminal = "/dev/pts/7"
        self.title = "Codex"
        self.screen = "› Ask Codex to do anything\n\n" + FOOTER
        self.stack.enter_context(patch.object(codex_resume, "PROC_ROOT", self.proc_root))
        self.tmux = self.stack.enter_context(patch.object(
            codex_resume.subprocess, "run", side_effect=self._metadata,
        ))
        self.capture = self.stack.enter_context(patch.object(
            codex_resume, "pane_text", side_effect=lambda _pane: self.screen,
        ))
        self.loaded = self.stack.enter_context(patch.object(
            codex_resume, "loaded_thread_ids", return_value={SESSION},
        ))
        self.title_proof = self.stack.enter_context(patch.object(
            codex_title, "native_title_proof", return_value=None,
        ))

    def _process(self, pid, *, comm="codex", command=None, parent=123,
                 group=456, foreground=456, terminal_session=123,
                 tty_number=34823, tty="/dev/pts/7", ticks=10):
        process = self.proc_root / str(pid)
        process.mkdir(exist_ok=True)
        fields = ["S", str(parent), str(group), str(terminal_session),
                  str(tty_number), str(foreground), *(["0"] * 13), str(ticks)]
        (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (process / "comm").write_text(comm + "\n")
        if command is None:
            command = f"codex\0resume\0--no-alt-screen\0{SESSION}\0".encode()
        (process / "cmdline").write_bytes(command)
        (process / "fd").mkdir(exist_ok=True)
        descriptor = process / "fd" / "0"
        if descriptor.is_symlink():
            descriptor.unlink()
        descriptor.symlink_to(tty)

    def _metadata(self, command, **options):
        self.assertEqual(command[:5], ["tmux", "display-message", "-p", "-t", "%987"])
        self.assertEqual(options["timeout"], 1)
        output = (f"%987\t123\t{self.title}\n" if command[-1] == "#{pane_id}\t#{pane_pid}\t#{pane_title}"
                  else f"{self.terminal}\t{self.row}\t{self.column}\t{self.in_mode}\t123\n")
        return subprocess.CompletedProcess(command, 0, output, "")

    def _ready(self):
        return codex_resume.resumed_session(456, SESSION, "%987")

    def _rollout(self, identity, descriptor=1):
        sessions = self.proc_root / "sessions"
        sessions.mkdir(exist_ok=True)
        rollout = sessions / f"rollout-2000-01-01T00-00-00-{identity}.jsonl"
        rollout.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": identity, "session_id": identity, "source": "cli",
        }}) + "\n")
        (self.proc_root / "456" / "fd" / str(descriptor)).symlink_to(rollout)

    def test_clipped_model_and_reasoning_footers_are_ready(self):
        footers = (
            FOOTER,
            "  GPT-6-Astra high · ~/Projects/research-workspace · weekly 95% …",
            "  gpt-5.4 xhigh · /work…",
            "  GPT-6-Sol low · /w",
            "  GPT-6-Astra high ·",
            "  GPT-6-Astra default · /work…",
        )
        for footer in footers:
            with self.subTest(footer=footer):
                self.screen = "› Ask Codex to do anything\n\n" + footer
                self.assertTrue(self._ready())

    def test_usage_text_alone_and_quoted_model_text_do_not_replace_a_footer(self):
        for footer in ("weekly 95% …", "GPT-6-Astra · /work", "GPT-6-Astra higher · /work",
                       "model: GPT-6-Astra high · /work", "The GPT-6-Astra high · /work"):
            with self.subTest(footer=footer):
                self.screen = "› Ask Codex to do anything\n" + footer
                self.assertFalse(self._ready())

    def test_each_layout_uses_its_current_composer_row(self):
        for row, height, footer in ((18, 22, FOOTER), (7, 10, "GPT-6-Astra high · /work · weekly 95% …"),
                                   (0, 3, FOOTER), (20, 24, "100% context left")):
            with self.subTest(row=row, height=height):
                self.row = row
                lines = [""] * height
                lines[row] = "› Ask Codex to do anything"
                lines[row + 2] = footer
                self.screen = "\n".join(lines)
                self.assertTrue(self._ready())

    def test_layout_change_during_daemon_probe_can_remain_ready(self):
        def resize():
            self.row = 3
            self.screen = "\n\n\n› Ask Codex to do anything\n\nGPT-6-Astra high · /work · weekly 95% …"
            return {SESSION}

        self.loaded.side_effect = resize
        self.assertTrue(self._ready())

    def test_initial_and_final_captures_exclude_scrollback(self):
        self.assertTrue(self._ready())
        self.assertEqual(self.capture.call_count, 2)
        self.assertEqual(self.capture.call_args_list[0].kwargs, {})
        self.assertEqual(self.capture.call_args_list[1].kwargs, {})

    def test_composer_and_footer_must_remain_near_the_live_cursor(self):
        for screen, row in ((self.screen + "\n" + "loading\n" * 13, 0),
                            ("› Ask Codex to do anything\n" + "\n" * 6 + FOOTER, 0),
                            (FOOTER + "\n› Ask Codex to do anything", 1),
                            ("shell output\n" + self.screen, 0)):
            with self.subTest(screen=screen, row=row):
                self.row = row
                self.screen = screen
                self.assertFalse(self._ready())

    def test_loading_signin_and_initialization_error_screens_are_unready(self):
        ready = self.screen
        for status in ("Resuming session", "Press enter to continue", "model:       loading",
                       "Sign in to Codex", "Sign in with ChatGPT",
                       "ERROR: failed to initialize state runtime: unavailable",
                       "Error: failed to load session"):
            with self.subTest(status=status):
                self.row = 1
                self.screen = status + "\n" + ready
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_signin_menu_cannot_use_a_stale_model_footer(self):
        self.screen = "› Sign in with ChatGPT\n\n" + FOOTER
        self.assertFalse(self._ready())
        self.assertFalse(codex_resume._composer_ready(456, "%987", self.screen))
        self.loaded.assert_not_called()

    def test_startup_screen_after_daemon_probe_cannot_use_an_earlier_composer(self):
        def loading():
            self.row = 1
            self.screen = "model:       loading\n" + self.screen
            return {SESSION}

        self.loaded.side_effect = loading
        self.assertFalse(self._ready())

    def test_exact_argv_identity_requires_a_loaded_thread_and_live_composer(self):
        for loaded in (None, set(), {OTHER}):
            with self.subTest(loaded=loaded):
                self.loaded.return_value = loaded
                self.assertFalse(self._ready())
        self.loaded.return_value = {SESSION}
        self.screen = f"codex resume {SESSION}"
        self.assertFalse(self._ready())

    def test_other_loaded_threads_cannot_bind_a_different_or_fresh_client(self):
        for command in (f"codex\0resume\0{OTHER}\0".encode(), b"codex\0",
                        f"codex\0Explain {SESSION}\0".encode()):
            with self.subTest(command=command):
                self._process(456, command=command)
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_stale_composer_in_a_shell_or_noninteractive_codex_is_unready(self):
        for comm, command in (("zsh", b"zsh\0"), ("codex", b"codex\0app-server\0"),
                              ("codex", f"codex\0exec\0{SESSION}\0".encode())):
            with self.subTest(comm=comm, command=command):
                self._process(456, comm=comm, command=command)
                self.assertFalse(self._ready())
                self.assertFalse(codex_resume._composer_ready(456, "%987", self.screen))
        self.loaded.assert_not_called()

    def test_background_unrelated_and_other_terminal_clients_are_unready(self):
        cases = ({"group": 888}, {"parent": 999}, {"terminal_session": 999},
                 {"tty": "/dev/pts/8", "tty_number": 34824}, {"tty_number": 0},
                 {"foreground": 888})
        for changes in cases:
            with self.subTest(changes=changes):
                self._process(456, **changes)
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_inherited_foreground_group_does_not_require_codex_group_leadership(self):
        self._process(123, comm="python3", command=b"python3\0-m\0workspace_state.codex_resume\0",
                      group=123, foreground=123)
        self._process(456, group=123, foreground=123)
        self.assertTrue(self._ready())

    def test_copy_mode_and_missing_or_malformed_tmux_metadata_fail_closed(self):
        self.in_mode = 1
        self.assertFalse(self._ready())
        for result in (subprocess.CompletedProcess([], 1, "", ""),
                       subprocess.CompletedProcess([], 0, "malformed", "")):
            with self.subTest(result=result):
                self.tmux.side_effect = None
                self.tmux.return_value = result
                self.assertFalse(self._ready())
        self.tmux.side_effect = subprocess.TimeoutExpired("tmux", 1)
        self.assertFalse(self._ready())

    def test_client_replacement_foreground_change_and_exec_during_probe_are_rejected(self):
        cases = ({"ticks": 999}, {"comm": "zsh", "command": b"zsh\0"},
                 {"group": 888}, {"parent": 999},
                 {"command": f"codex\0resume\0{OTHER}\0".encode()})
        for changes in cases:
            with self.subTest(changes=changes):
                self._process(456)

                def replace(changes=changes):
                    self._process(456, **changes)
                    return {SESSION}

                self.loaded.side_effect = replace
                self.assertFalse(self._ready())

    def test_pane_pid_reuse_during_probe_is_rejected(self):
        def replace():
            self._process(123, comm="zsh", command=b"zsh\0", group=123, ticks=999)
            return {SESSION}

        self.loaded.side_effect = replace
        self.assertFalse(self._ready())

    def test_owned_rollout_requires_exact_identity_and_foreground_terminal(self):
        self._rollout(SESSION)
        self.loaded.return_value = None
        self.screen = ""
        self.assertTrue(self._ready())
        self.assertFalse(codex_resume.resumed_session(456, OTHER, "%987"))
        self._process(456, group=888)
        self.assertFalse(self._ready())
        self._process(456, tty="/dev/pts/8", tty_number=34824)
        self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_owned_rollout_readiness_is_independent_of_copy_mode_scrollback(self):
        self._rollout(SESSION)
        self.in_mode = 1
        self.row = 99
        self.column = 0
        self.screen = "model:       loading\nResuming session\nold captured history"
        self.assertTrue(self._ready())
        self.capture.assert_not_called()
        self.loaded.assert_not_called()
        self.title_proof.assert_not_called()

    def test_copy_mode_without_owned_rollout_cannot_use_daemon_or_title_proof(self):
        self.in_mode = 1
        self.title = SESSION
        self.title_proof.return_value = self._native_proof()
        self.assertFalse(self._ready())
        self.assertFalse(codex_resume._composer_ready(456, "%987", self.screen))
        self.loaded.assert_not_called()
        self.title_proof.assert_not_called()

    def test_copy_mode_does_not_relax_owned_identity_or_foreground_guards(self):
        self._rollout(SESSION)
        self.in_mode = 1
        self.assertFalse(codex_resume.resumed_session(456, OTHER, "%987"))
        for changes in ({"group": 888}, {"parent": 999}, {"tty": "/dev/pts/8", "tty_number": 34824},
                        {"comm": "zsh", "command": b"zsh\0"}):
            with self.subTest(changes=changes):
                self._process(456, **changes)
                self.assertFalse(self._ready())

    def test_pid_reuse_during_rollout_ownership_read_is_rejected(self):
        self._rollout(SESSION)
        read_owned = codex_resume._open_rollout_sessions

        def replace(pid, *, proc_root):
            owned = read_owned(pid, proc_root=proc_root)
            self._process(456, ticks=999)
            return owned

        with patch.object(codex_resume, "_open_rollout_sessions", side_effect=replace):
            self.assertFalse(self._ready())

    def test_conflicting_rollouts_cannot_use_the_composer_or_loaded_daemon(self):
        self._rollout(SESSION)
        self._rollout(OTHER, descriptor=2)
        self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_rollout_switch_during_ownership_read_is_rejected(self):
        self._rollout(SESSION)
        read_owned = codex_resume._open_rollout_sessions

        def switch(pid, *, proc_root):
            owned = read_owned(pid, proc_root=proc_root)
            (self.proc_root / "456" / "fd" / "1").unlink()
            self._rollout(OTHER)
            return owned

        with patch.object(codex_resume, "_open_rollout_sessions", side_effect=switch):
            self.assertFalse(self._ready())

    def test_owned_rollout_does_not_override_a_startup_screen(self):
        self._rollout(SESSION)
        ready = self.screen
        for status in ("model:       loading", "Sign in to Codex", "Error: failed to load session"):
            with self.subTest(status=status):
                self.row = 1
                self.screen = status + "\n" + ready
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def _native_proof(self, identity=SESSION, **changes):
        fields = {"session_id": identity, "pid": 456, "pane_id": "%987", "pane_pid": 123,
                  "start_ticks": 10, "title": self.title}
        fields.update(changes)
        return codex_title.NativeTitleProof(**fields)

    def test_proven_native_title_supports_a_fresh_current_client(self):
        self._process(456, command=b"codex\0")
        self.title = SESSION[:29] + "..."
        self.title_proof.return_value = self._native_proof()
        self.loaded.return_value = None
        self.assertTrue(self._ready())
        self.title_proof.assert_called_once_with(456, "%987")
        self.loaded.assert_not_called()

    def test_proven_current_title_can_replace_stale_resume_argv(self):
        self._process(456, command=f"codex\0resume\0{OTHER}\0".encode())
        self.title = SESSION
        self.title_proof.return_value = self._native_proof()
        self.assertTrue(self._ready())
        self.assertFalse(codex_resume.resumed_session(456, OTHER, "%987"))
        self.loaded.assert_not_called()

    def test_conflicting_native_title_vetoes_stale_argv_even_if_both_ids_are_loaded(self):
        self.loaded.return_value = {SESSION, OTHER}
        for title in (OTHER, OTHER[:29] + "..."):
            with self.subTest(title=title):
                self.title = title
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()
        self.title_proof.assert_not_called()

    def test_an_unproven_matching_title_never_binds_a_fresh_client(self):
        self._process(456, command=b"codex\0")
        self.title = SESSION
        self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_matching_unproven_titles_preserve_exact_argv_fallback(self):
        for title in (SESSION, SESSION[:29] + "...", "Codex"):
            with self.subTest(title=title):
                self.title = title
                self.assertTrue(self._ready())

    def test_colliding_unproven_prefix_vetoes_exact_argv_fallback(self):
        self.title = SESSION[:29] + "..."
        collision = "11111111-1111-4111-8111-11111aaaaaaa"
        self.loaded.return_value = {SESSION, collision}
        self.assertFalse(self._ready())

    def test_title_change_during_daemon_probe_rejects_old_argv(self):
        self.title = SESSION

        def switch():
            self.title = OTHER
            return {SESSION, OTHER}

        self.loaded.side_effect = switch
        self.assertFalse(self._ready())

    def test_proof_for_another_client_or_uuid_is_rejected(self):
        self.title = SESSION[:29] + "..."
        collision = "11111111-1111-4111-8111-11111aaaaaaa"
        for changes in ({"session_id": collision}, {"pid": 999}, {"pane_id": "%999"},
                        {"pane_pid": 999}, {"start_ticks": 999}, {"title": OTHER}):
            with self.subTest(changes=changes):
                self.title_proof.return_value = self._native_proof(**changes)
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_process_or_title_replacement_during_proof_is_rejected(self):
        self.title = SESSION
        proof = self._native_proof()

        def replace():
            self._process(456, ticks=999)
            return proof

        self.title_proof.side_effect = lambda _pid, _pane: replace()
        self.assertFalse(self._ready())
        self._process(456)

        def switch(_pid, _pane):
            self.title = OTHER
            return proof

        self.title_proof.side_effect = switch
        self.assertFalse(self._ready())

    def test_startup_screen_after_native_proof_is_unready(self):
        self.title = SESSION
        proof = self._native_proof()

        def loading(_pid, _pane):
            self.row = 1
            self.screen = "model:       loading\n" + self.screen
            return proof

        self.title_proof.side_effect = loading
        self.assertFalse(self._ready())

    def test_open_rollout_stays_stronger_than_a_conflicting_title(self):
        self._rollout(SESSION)
        self.title = OTHER
        self.title_proof.return_value = self._native_proof(OTHER)
        self.assertTrue(self._ready())
        self.title_proof.assert_not_called()
        self.loaded.assert_not_called()

    def test_unavailable_or_unowned_title_metadata_fails_closed(self):
        for snapshot in (None, ("%987", 999, "Codex")):
            with self.subTest(snapshot=snapshot), patch.object(codex_resume, "_pane_title_snapshot", return_value=snapshot):
                self.assertFalse(self._ready())
        self.loaded.assert_not_called()

    def test_raw_title_snapshot_rejects_malformed_and_wrong_pane_metadata(self):
        for output in (f"%other\t123\t{SESSION}\n", f"%987\t0\t{SESSION}\n", "malformed",
                       f"%987\t123\t{SESSION}\n\n"):
            with self.subTest(output=output), patch.object(codex_resume.subprocess, "run", return_value=
                                                          subprocess.CompletedProcess([], 0, output, "")):
                self.assertIsNone(codex_resume._pane_title_snapshot("%987"))


if __name__ == "__main__":
    unittest.main()
