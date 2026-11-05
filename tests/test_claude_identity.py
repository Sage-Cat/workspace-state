from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from workspace_state import claude_identity, conversation_guard


FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


class ClaudeIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.proc.mkdir()
        self.runtime = self.root / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.environment = patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.runtime)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.pane_id = "%3"
        self._process(456, "zsh", b"zsh\0", foreground=789)
        self._process(789, "claude", b"claude\0", parent=456, group=789)
        self._process(900, "sh", b"sh\0", parent=789, group=789)
        self._process(901, "python3", b"python3\0hook\0", parent=900, group=789)
        self.transcript = self.root / f"{FIRST}.jsonl"
        self.transcript.write_text('{"type":"user","sessionId":"' + FIRST + '"}\n', encoding="utf-8")
        self.transcript.chmod(0o600)

    def _process(self, pid, comm, command, *, parent=0, group=None,
                 foreground=None, ticks=10, tty="/dev/pts/1", tty_number=1):
        process = self.proc / str(pid)
        process.mkdir(exist_ok=True)
        fields = ["S", str(parent), str(group or pid), "456", str(tty_number),
                  str(foreground or group or pid), *(["0"] * 13), str(ticks)]
        (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (process / "comm").write_text(comm + "\n")
        (process / "cmdline").write_bytes(command)
        (process / "fd").mkdir(exist_ok=True)
        (process / "fd" / "0").symlink_to(tty)

    def _payload(self, identity=FIRST, event="SessionStart", transcript=None):
        return {"hook_event_name": event, "session_id": identity,
                "transcript_path": str(self.transcript if transcript is None else transcript),
                "cwd": "/work", "source": "startup"}

    def _start(self, identity=FIRST, transcript=None, *, event_tick=None):
        if event_tick is not None:
            self._process_restamp(901, event_tick)
        return claude_identity.record_session_start(
            self._payload(identity, transcript=transcript), proc_root=self.proc,
            hook_pid=901, environ={"TMUX_PANE": self.pane_id})

    def _capture(self):
        return claude_identity.claude_for_pane(456, "/work", proc_root=self.proc,
                                               pane_id=self.pane_id)

    def test_new_session_hook_binds_exact_live_foreground_client(self):
        self.assertTrue(self._start())
        proof = self._capture()
        self.assertEqual(proof["session_id"], FIRST)
        self.assertEqual(proof["confidence"], "session-start-hook")
        self.assertEqual(proof["pid"], 789)
        self.assertTrue(proof["persisted"])
        receipt = claude_identity._receipt_path(self.pane_id)
        self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
        self.assertEqual(receipt.parent.stat().st_mode & 0o777, 0o700)

    def test_empty_transcript_is_not_yet_restorable(self):
        self.transcript.write_text("", encoding="utf-8")
        self.assertTrue(self._start())
        proof = self._capture()
        self.assertEqual(proof["session_id"], FIRST)
        self.assertFalse(proof["persisted"])

    def test_missing_hook_receipt_does_not_guess_from_bare_process(self):
        result = self._capture()
        self.assertIsNone(result["session_id"])
        self.assertEqual(result["confidence"], "missing-hook-receipt")

    def test_clear_replaces_old_identity_even_when_resume_argv_is_stale(self):
        (self.proc / "789" / "cmdline").write_bytes(f"claude\0--resume\0{FIRST}\0".encode())
        self.assertTrue(self._start())
        other = self.root / f"{SECOND}.jsonl"
        other.write_text("")
        other.chmod(0o600)
        self.assertTrue(self._start(SECOND, transcript=other, event_tick=11))
        self.assertEqual(self._capture()["session_id"], SECOND)

    def test_late_old_start_cannot_replace_newer_session(self):
        other = self.root / f"{SECOND}.jsonl"
        other.write_text('{"type":"user","sessionId":"' + SECOND + '"}\n')
        other.chmod(0o600)
        self.assertTrue(self._start(SECOND, transcript=other, event_tick=20))
        self.assertFalse(self._start(FIRST, event_tick=10))
        self.assertEqual(self._capture()["session_id"], SECOND)

    def test_late_start_cannot_revive_an_ended_session(self):
        self.assertTrue(self._start(event_tick=20))
        self._process_restamp(901, 21)
        self.assertTrue(claude_identity.record_session_end(
            self._payload(FIRST, "SessionEnd"), proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))
        self.assertFalse(self._start(event_tick=10))
        self.assertIsNone(self._capture()["session_id"])

    def test_late_end_cannot_erase_newer_start_for_same_session(self):
        self.assertTrue(self._start(event_tick=20))
        self._process_restamp(901, 30)
        self.assertTrue(self._start(event_tick=30))
        self._process_restamp(901, 25)
        self.assertFalse(claude_identity.record_session_end(
            self._payload(FIRST, "SessionEnd"), proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))
        self.assertEqual(self._capture()["session_id"], FIRST)

    def test_equal_tick_different_sessions_fail_closed(self):
        self.assertTrue(self._start(event_tick=20))
        self.assertFalse(self._start(SECOND, event_tick=20))
        self.assertIsNone(self._capture()["session_id"])

    def test_session_end_invalidates_only_its_own_receipt(self):
        self._start()
        self.assertFalse(claude_identity.record_session_end(
            self._payload(SECOND, "SessionEnd"), proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))
        self.assertEqual(self._capture()["session_id"], FIRST)
        self.assertTrue(claude_identity.record_session_end(
            self._payload(FIRST, "SessionEnd"), proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))
        self.assertIsNone(self._capture()["session_id"])

    def test_stale_receipt_after_pid_reuse_is_unresolved(self):
        self._start()
        self._process_restamp(789, 11)
        self.assertIsNone(self._capture()["session_id"])

    def _process_restamp(self, pid, ticks):
        path = self.proc / str(pid) / "stat"
        raw = path.read_text()
        fields = raw.split()
        fields[-1] = str(ticks)
        path.write_text(" ".join(fields))

    def test_closed_client_is_not_recovered_from_receipt(self):
        self._start()
        for item in (self.proc / "789").iterdir():
            if item.is_file() or item.is_symlink():
                item.unlink()
        self.assertIsNone(self._capture())

    def test_background_client_cannot_supply_an_identity(self):
        self._start()
        self._process_restate(789, group=800)
        self.assertIsNone(self._capture())

    def _process_restate(self, pid, group):
        path = self.proc / str(pid) / "stat"
        raw = path.read_text()
        fields = raw.split()
        fields[4] = str(group)
        path.write_text(" ".join(fields))

    def test_hook_identity_without_durable_transcript_is_reported(self):
        absent = self.root / f"{FIRST}.jsonl"
        absent.unlink()
        self.assertTrue(self._start(transcript=absent))
        proof = self._capture()
        self.assertEqual(proof["session_id"], FIRST)
        self.assertFalse(proof["persisted"])

    def test_hook_rejects_subagents_and_invalid_uuid(self):
        payload = self._payload()
        payload["agent_type"] = "worker"
        self.assertFalse(claude_identity.record_session_start(
            payload, proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))
        payload = self._payload("not-a-uuid")
        self.assertFalse(claude_identity.record_session_start(
            payload, proc_root=self.proc, hook_pid=901,
            environ={"TMUX_PANE": self.pane_id}))

    def test_noninteractive_process_is_not_captured(self):
        (self.proc / "789" / "cmdline").write_bytes(b"claude\0-p\0hello\0")
        self.assertFalse(self._start())
        self.assertIsNone(self._capture())

    def test_multiple_foreground_clients_are_ambiguous(self):
        self._start()
        self._process(790, "claude", b"claude\0", parent=456, group=789)
        self.assertIsNone(self._capture()["session_id"])

    def test_readiness_requires_exact_live_pane_and_persisted_transcript(self):
        self._start()
        tmux_result = SimpleNamespace(returncode=0, stdout="%3\t456\t/work\n")
        with patch.object(claude_identity.subprocess, "run", return_value=tmux_result):
            self.assertTrue(claude_identity.resumed_session(
                789, FIRST, "%3", proc_root=self.proc))
            self.assertFalse(claude_identity.resumed_session(
                789, SECOND, "%3", proc_root=self.proc))
        self.transcript.unlink()
        with patch.object(claude_identity.subprocess, "run", return_value=tmux_result):
            self.assertFalse(claude_identity.resumed_session(
                789, FIRST, "%3", proc_root=self.proc))


    BOOT = "boot-aaaa"

    def _resume_client(self, argv=None, *, claim=True, boot=None, mode=0o600, **overrides):
        command = argv or f"claude\0--resume\0{FIRST}\0".encode()
        (self.proc / "789" / "cmdline").write_bytes(command)
        boot_file = self.proc / "sys/kernel/random/boot_id"
        boot_file.parent.mkdir(parents=True, exist_ok=True)
        boot_file.write_text(self.BOOT + "\n")
        if claim:
            record = {"pid": 789, "start_ticks": 10, "boot_id": boot or self.BOOT, **overrides}
            path = conversation_guard.claim_path("claude", FIRST)
            path.write_text(json.dumps(record))
            path.chmod(mode)

    def test_exact_resume_argv_with_matching_claim_is_accepted(self):
        self._resume_client()
        self.assertEqual(self._capture(), {
            "pid": 789, "session_id": FIRST, "confidence": "resume-claim",
            "persisted": True, "start_ticks": "10"})

    def test_resume_claim_must_match_pid_ticks_and_boot(self):
        for override in ({"pid": 790}, {"start_ticks": 11}, {"boot_id": "other"},
                         {"extra": 1}):
            with self.subTest(override=override):
                self._resume_client(**override)
                self.assertEqual(self._capture()["confidence"], "missing-hook-receipt")
        self._resume_client()
        (self.proc / "sys/kernel/random/boot_id").unlink()
        self.assertEqual(self._capture()["confidence"], "missing-hook-receipt")

    def test_resume_claim_needs_a_claim_record(self):
        self._resume_client(claim=False)
        self.assertEqual(self._capture()["confidence"], "missing-hook-receipt")

    def test_resume_claim_requires_the_exact_argv(self):
        for command in (f"claude\0--resume\0{FIRST}\0--model\0x\0".encode(),
                        b"claude\0--continue\0",
                        f"claude\0--resume\0{FIRST[:-1]}\0".encode(),
                        f"claude\0-r\0{FIRST}\0".encode(),
                        f"other\0--resume\0{FIRST}\0".encode()):
            with self.subTest(command=command):
                self._resume_client(command)
                result = self._capture()
                self.assertTrue(result is None or result["confidence"] != "resume-claim")

    def test_unsafe_claim_file_is_not_accepted(self):
        self._resume_client(mode=0o640)
        self.assertEqual(self._capture()["confidence"], "missing-hook-receipt")
        self._resume_client()
        path = conversation_guard.claim_path("claude", FIRST)
        target = self.root / "target.json"
        path.rename(target)
        path.symlink_to(target)
        self.assertEqual(self._capture()["confidence"], "missing-hook-receipt")

    def test_receipt_for_another_session_wins_over_resume_claim(self):
        other = self.root / f"{SECOND}.jsonl"
        other.write_text('{"sessionId":"' + SECOND + '"}\n')
        other.chmod(0o600)
        self._resume_client()
        self.assertTrue(self._start(SECOND, transcript=other))
        proof = self._capture()
        self.assertEqual(proof["session_id"], SECOND)
        self.assertEqual(proof["confidence"], "session-start-hook")

    def test_hook_result_includes_start_ticks(self):
        self.assertTrue(self._start())
        self.assertEqual(self._capture()["start_ticks"], "10")

    def test_resume_claim_never_proves_readiness(self):
        self._resume_client()
        tmux_result = SimpleNamespace(returncode=0, stdout="%3\t456\t/work\n")
        with patch.object(claude_identity.subprocess, "run", return_value=tmux_result):
            self.assertFalse(claude_identity.resumed_session(
                789, FIRST, "%3", proc_root=self.proc))


if __name__ == "__main__":
    unittest.main()
