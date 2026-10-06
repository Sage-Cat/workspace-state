from __future__ import annotations

from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import codex_resume, codex_status


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
COLLISION = "11111111-1111-4111-8111-11111aaaaaaa"
HEADER = "  >_ OpenAI Codex (v0.160.1)"
FOOTER = "  GPT-6-Astra default · /work · ← for agents"


def block(identity=SESSION):
    return ["/status", "", HEADER, "", "  Server:              Local background server", "",
            "  Model:               GPT-6-Astra (reasoning low)",
            "  Model provider:      fixture", "  Directory:           /work",
            "  Permissions:         Workspace (Ask for approval)", "  Agents.md:           <none>",
            "  Collaboration mode:  Default", "  Session:             " + identity, "",
            "  Token usage:         0 total  (0 input + 0 output)", "  Limits:              data not available yet"]


class CodexStatusTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.proc = root / "proc"
        self.proc.mkdir()
        self.home = root / "codex"
        self.home.mkdir()
        self._process(123, comm="zsh", argv=b"zsh\0", group=123)
        self._process(456)
        self.clock = 0.0
        self.mode = "0"
        self.title = "Codex"
        self.pane_pid = 123
        self.width = 319
        self.height = 76
        self.sent = []
        self.render = True
        self.result_block = block()
        self.after_enter = None
        self.after_literal = None
        self.on_capture = None
        self.capture_count = 0
        self.log = [HEADER, "     /work", "", "  Fixture ready."]
        self._screen()
        self.stack.enter_context(patch.object(codex_resume, "PROC_ROOT", self.proc))
        self.stack.enter_context(patch.object(codex_status.time, "monotonic", side_effect=lambda: self.clock))
        self.stack.enter_context(patch.object(codex_status.time, "sleep", side_effect=self._sleep))
        self.stack.enter_context(patch.object(codex_status.subprocess, "run", side_effect=self._tmux))
        self.loaded = self.stack.enter_context(patch.object(codex_status, "loaded_thread_ids", return_value={SESSION}))

    def _sleep(self, duration):
        self.clock += duration

    def _process(self, pid, *, comm="codex", argv=b"codex\0--no-alt-screen\0", parent=123,
                 group=456, foreground=456, tty="/dev/pts/7", ticks=10):
        process = self.proc / str(pid)
        process.mkdir(exist_ok=True)
        fields = ["S", str(parent), str(group), "123", "34823", str(foreground),
                  *(["0"] * 13), str(ticks)]
        (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (process / "comm").write_text(comm + "\n")
        (process / "cmdline").write_bytes(argv)
        (process / "environ").write_bytes(os.fsencode(f"CODEX_HOME={self.home}") + b"\0")
        (process / "fd").mkdir(exist_ok=True)
        target = process / "fd" / "0"
        target.unlink(missing_ok=True)
        target.symlink_to(tty)

    def _screen(self, composer="› Ask Codex to do anything", footer=FOOTER):
        menu = (["› /status      show current session configuration and token usage",
                 "  /statusline  configure which items appear in the status line"]
                if composer == "› /status" else [])
        self.row = len(self.log) + len(menu) + 2
        self.column = 9 if composer == "› /status" else 2
        self.screen = "\n".join([*self.log, "", *menu, "", composer, "", footer]) + "\n"

    def _tmux(self, command, **options):
        self.assertEqual(command[0], "tmux")
        self.assertEqual(command[2:4], ["-t", "%987"])
        self.assertGreater(options["timeout"], 0)
        self.assertLessEqual(options["timeout"], .3)
        method = command[1]
        if method == "display-message":
            text = f"%987\t{self.pane_pid}\t/dev/pts/7\t{self.mode}\t{self.row}\t{self.column}\t{self.width}\t{self.height}\t{self.title}\n"
        elif method == "capture-pane":
            self.capture_count += 1
            if self.on_capture:
                self.on_capture(self.capture_count)
            text = self.screen
        elif method == "send-keys":
            self.sent.append(command[4:])
            if command[4:] == ["-l", "/status"]:
                if self.render:
                    self._screen("› /status")
                if self.after_literal:
                    self.after_literal()
            elif command[4:] == ["Enter"]:
                self.log += ["", *self.result_block]
                self._screen()
                if self.after_enter:
                    self.after_enter()
            else:
                self.fail(f"unexpected input: {command}")
            text = ""
        else:
            self.fail(f"unexpected command: {command}")
        return subprocess.CompletedProcess(command, 0, text, "")

    def proof(self, timeout=5):
        return codex_status.probe(456, "%987", timeout)

    def refused(self):
        with self.assertRaises(codex_status.StatusRefused):
            self.proof()

    def test_native_fresh_status_returns_immutable_proof_and_capture_record(self):
        proof = self.proof()
        self.assertEqual(proof.session_id, SESSION)
        self.assertEqual((proof.width, proof.height), (319, 76))
        self.assertEqual(proof.capture_record(), {"pid": 456, "session_id": SESSION,
                         "confidence": "native-status", "start_ticks": "10", "tty": "/dev/pts/7"})
        self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])
        self.assertTrue(codex_status.revalidate(proof))
        with self.assertRaises(FrozenInstanceError):
            proof.session_id = OTHER
        with self.assertRaises(TypeError):
            proof.client.chain[456] = proof.client.chain[123]

    def test_historical_status_is_supported_only_when_a_new_block_is_appended(self):
        self.log += ["", *block()]
        self._screen()
        proof = self.proof()
        self.assertEqual(proof.status_block, "\n".join(block()))
        self.assertTrue(codex_status.revalidate(proof))

    def test_account_shaped_native_status_uses_context_window_without_token_usage(self):
        self.result_block = [line for line in block() if not line.startswith(("  Token usage:", "  Limits:"))]
        self.result_block += ["  Account:             synthetic account", "  Thread name:         Synthetic task",
                              "  Context window:      100% left", "  Weekly limit:        95% left",
                              "  Credits:             0 credits"]
        proof = self.proof()
        self.assertEqual(proof.session_id, SESSION)
        self.assertTrue(codex_status.revalidate(proof))

    def test_scrolled_old_transcript_suffix_still_proves_new_block(self):
        self.log += ["", *block(OTHER)]
        self._screen()
        def scroll():
            self.log = self.log[8:]
            self._screen()
        self.after_enter = scroll
        self.assertEqual(self.proof().session_id, SESSION)

    def test_native_completion_menu_can_crop_two_old_rows_without_new_output(self):
        self.log += ["", *block(OTHER)]
        self._screen()
        def scroll():
            self.log = self.log[2:]
            self._screen("› /status")
        self.after_literal = scroll
        self.assertEqual(self.proof().session_id, SESSION)

    def test_native_alternate_screen_completion_can_overlay_one_final_status_metadata_row(self):
        for field in ("Limits", "Credits"):
            with self.subTest(field=field):
                self.setUp()
                historical = block()
                if field == "Credits":
                    historical[-2] = "  Context window:      100% left"
                historical[-1] = f"  {field}:              synthetic fixture"
                self.log += ["", *historical]
                self._screen()
                original = list(self.log)
                def overlay():
                    self.log = original[:-1]
                    self._screen("› /status")
                def appended():
                    self.log = [*original, "", *block()]
                    self._screen()
                self.after_literal, self.after_enter = overlay, appended
                self.assertEqual(self.proof().session_id, SESSION)
                self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])

    def test_alternate_screen_overlay_requires_exact_menu_complete_prior_block_and_one_known_row(self):
        for change in ("two rows", "model row", "ordinary output", "menu", "changed prefix"):
            with self.subTest(change=change):
                self.setUp()
                self.log += ["", *block()]
                if change == "model row":
                    self.log[-1] = "  Model: other model"
                elif change == "ordinary output":
                    self.log = [HEADER, "     /work", "", "  Limits: unrelated ordinary output"]
                self._screen()
                original = list(self.log)
                def overlay():
                    self.log = original[:-2] if change == "two rows" else original[:-1]
                    if change == "changed prefix":
                        self.log[-2] = "  Token usage: changed output"
                    self._screen("› /status")
                    if change == "menu":
                        self.screen = self.screen.replace("show current session configuration and token usage", "unrelated menu")
                self.after_literal = overlay
                self.refused()
                self.assertEqual(self.sent, [["-l", "/status"]])

    def test_allowed_native_overlay_never_promotes_historical_report_to_fresh_identity(self):
        self.log += ["", *block()]
        self._screen()
        original = list(self.log)
        self.after_literal = lambda: (setattr(self, "log", original[:-1]), self._screen("› /status"))
        self.after_enter = lambda: (setattr(self, "log", original), self._screen())
        self.refused()
        self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])

    def test_no_surviving_overlap_refuses_even_a_valid_new_status(self):
        self.after_enter = lambda: (setattr(self, "log", block()), self._screen())
        self.refused()

    def test_pending_draft_at_start_or_cursor_at_start_of_draft_never_gets_input(self):
        for composer, column in (("› draft", 7), ("› draft", 2), ("› /status", 9)):
            with self.subTest(composer=composer, column=column):
                self._screen(composer)
                self.column = column
                self.refused()
                self.assertEqual(self.sent, [])

    def test_busy_empty_composer_spinner_loading_and_modal_never_get_input(self):
        for hint in ("• Working (0s • esc to interrupt)", "Thinking", "model: loading",
                     "Press enter to continue", "Would you like to run this command?", "Select a model"):
            with self.subTest(hint=hint):
                self.log = [HEADER, "     /work", "", hint]
                self._screen()
                self.refused()
                self.assertEqual(self.sent, [])
        self.log = [HEADER, "     /work", "", "  Fixture ready."]
        self._screen(footer=FOOTER + " · ⠋")
        self.refused()
        self.assertEqual(self.sent, [])

    def test_copy_mode_shell_background_and_unowned_terminal_never_get_input(self):
        for change in ("copy", "shell", "background", "tty", "ancestor"):
            with self.subTest(change=change):
                self.mode = "1" if change == "copy" else "0"
                self._process(456, comm="zsh" if change == "shell" else "codex",
                              foreground=123 if change == "background" else 456,
                              tty="/dev/pts/8" if change == "tty" else "/dev/pts/7",
                              parent=999 if change == "ancestor" else 123)
                self.refused()
                self.assertEqual(self.sent, [])

    def test_input_or_output_change_between_stable_samples_refuses(self):
        def change(count):
            if count == 2:
                self._screen("› user draft")
        self.on_capture = change
        self.refused()
        self.assertEqual(self.sent, [])

    def test_pid_replacement_owner_and_title_change_before_enter_refuse_without_clear(self):
        for change in ("pid", "owner", "title", "draft"):
            with self.subTest(change=change):
                self.setUp()
                def mutate():
                    if change == "pid":
                        self._process(456, ticks=11)
                    elif change == "owner":
                        self._process(456, foreground=123)
                    elif change == "title":
                        self.title = OTHER
                    else:
                        self._screen("› /status plus draft")
                self.after_literal = mutate
                self.refused()
                self.assertEqual(self.sent, [["-l", "/status"]])

    def test_literal_render_timeout_never_presses_enter_and_has_five_second_bound(self):
        self.render = False
        with self.assertRaisesRegex(codex_status.StatusRefused, "deadline"):
            self.proof(timeout=100)
        self.assertEqual(self.clock, 5)
        self.assertEqual(self.sent, [["-l", "/status"]])

    def test_unknown_or_changed_completion_menu_never_gets_enter(self):
        self.after_literal = lambda: setattr(self, "screen", self.screen.replace(
            "show current session configuration and token usage", "unrelated menu"))
        self.refused()
        self.assertEqual(self.sent, [["-l", "/status"]])

    def test_owned_render_transition_after_enter_is_observed_without_resending_input(self):
        def transition(count):
            if count == 6:
                self.log.append("")
                self._screen()
        self.on_capture = transition
        self.assertEqual(self.proof().session_id, SESSION)
        self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])

    def test_resize_during_an_observation_refuses_before_input(self):
        def resize(count):
            if count == 2:
                self.height = 72
        self.on_capture = resize
        with self.assertRaisesRegex(codex_status.StatusRefused, "pane resized during observation"):
            self.proof()
        self.assertEqual(self.sent, [])

    def test_resize_after_literal_refuses_without_enter_or_clearing_input(self):
        self.after_literal = lambda: setattr(self, "width", 315)
        with self.assertRaisesRegex(codex_status.StatusRefused, "pane resized during diagnostic"):
            self.proof()
        self.assertEqual(self.sent, [["-l", "/status"]])
        self.assertIn("› /status", self.screen)

    def test_resize_after_enter_refuses_immediately_instead_of_waiting_for_freshness(self):
        self.after_enter = lambda: setattr(self, "height", 72)
        with self.assertRaisesRegex(codex_status.StatusRefused, "pane resized during diagnostic"):
            self.proof()
        self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])
        self.assertLess(self.clock, 1)

    def test_resize_rejects_late_proof_even_when_owner_cursor_and_screen_are_unchanged(self):
        for dimension in ("width", "height"):
            with self.subTest(dimension=dimension):
                self.setUp()
                proof = self.proof()
                setattr(self, dimension, getattr(self, dimension) - 4)
                sent = list(self.sent)
                self.assertFalse(codex_status.revalidate(proof))
                self.assertEqual(self.sent, sent)

    def test_resize_during_late_catalog_query_rejects_proof(self):
        proof = self.proof()
        def resize(*arguments, **options):
            self.height = 72
            return {SESSION}
        self.loaded.side_effect = resize
        self.assertFalse(codex_status.revalidate(proof))

    def test_stale_block_and_identical_periodic_screen_cannot_prove_freshness(self):
        self.log += ["", *block()]
        self._screen()
        original = list(self.log)
        self.after_enter = lambda: (setattr(self, "log", original), self._screen())
        self.refused()
        self.assertEqual(self.sent, [["-l", "/status"], ["Enter"]])

    def test_malformed_duplicate_or_echo_only_reports_are_rejected(self):
        for result in (["/status"], block(SESSION[:29] + "..."), [*block(), "  Session: " + SESSION],
                       [*block(), "", *block()], [*block(), "unrelated output"],
                       [*block(), "  Token usage: 0 total"], [*block(), "  Context window: 100% left"]):
            with self.subTest(result=result):
                self.setUp()
                self.result_block = result
                self.refused()

    def test_status_uuid_must_be_loaded_but_unrelated_catalog_growth_is_supported(self):
        self.result_block = block(OTHER)
        self.refused()
        self.setUp()
        self.after_enter = lambda: setattr(self.loaded, "return_value", {SESSION, OTHER})
        proof = self.proof()
        self.assertEqual(proof.session_id, SESSION)
        self.assertTrue(codex_status.revalidate(proof))

    def test_native_uuid_title_conflict_and_prefix_collision_refuse(self):
        self.title = OTHER
        self.loaded.return_value = {SESSION, OTHER}
        self.refused()
        self.setUp()
        self.title = SESSION[:29] + "..."
        self.loaded.return_value = {SESSION, COLLISION}
        self.refused()
        self.assertEqual(self.sent, [])

    def test_matching_native_uuid_and_unique_native_prefix_are_supported(self):
        for title in (SESSION, SESSION[:29] + "..."):
            with self.subTest(title=title):
                self.setUp()
                self.title = title
                self.assertEqual(self.proof().session_id, SESSION)

    def test_catalog_unavailability_refuses_before_input(self):
        self.loaded.return_value = None
        self.refused()
        self.assertEqual(self.sent, [])

    def test_revalidation_rejects_any_new_output_thread_title_input_or_client_change(self):
        for change in ("output", "title", "draft", "pid", "catalog", "copy", "footer"):
            with self.subTest(change=change):
                self.setUp()
                proof = self.proof()
                if change == "output":
                    self.log.append("  New output")
                    self._screen()
                elif change == "title":
                    self.title = OTHER
                elif change == "draft":
                    self._screen("› user draft")
                elif change == "pid":
                    self._process(456, ticks=11)
                elif change == "catalog":
                    self.loaded.return_value = {OTHER}
                elif change == "copy":
                    self.mode = "1"
                else:
                    self._screen(footer=FOOTER + " · weekly 95%")
                sent = list(self.sent)
                self.assertFalse(codex_status.revalidate(proof))
                self.assertEqual(self.sent, sent)

    def test_unrelated_catalog_growth_after_proof_is_valid_but_matching_prefix_collision_refuses(self):
        proof = self.proof()
        self.loaded.return_value = {SESSION, OTHER}
        self.assertTrue(codex_status.revalidate(proof))
        self.setUp()
        self.title = SESSION[:29] + "..."
        proof = self.proof()
        self.loaded.return_value = {SESSION, COLLISION}
        self.assertFalse(codex_status.revalidate(proof))

    def test_output_or_title_change_during_catalog_revalidation_is_rejected(self):
        for change in ("output", "title"):
            with self.subTest(change=change):
                self.setUp()
                proof = self.proof()
                def mutate(*arguments, **options):
                    if change == "output":
                        self.log.append("  Changed while checking catalog")
                        self._screen()
                    else:
                        self.title = OTHER
                    return {SESSION}
                self.loaded.side_effect = mutate
                self.assertFalse(codex_status.revalidate(proof))

    def test_invalid_arguments_never_get_input(self):
        for pid, pane, timeout in ((0, "%987", 5), (456, "current", 5), (456, "%987", float("nan")),
                                   (456, "%987", 0), (True, "%987", 5)):
            with self.subTest(pid=pid, pane=pane, timeout=timeout):
                with self.assertRaises(codex_status.StatusRefused):
                    codex_status.probe(pid, pane, timeout)
                self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
