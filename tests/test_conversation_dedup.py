"""Tests for the one-client-per-conversation guard and its callers."""
from __future__ import annotations

from argparse import Namespace
from contextlib import ExitStack, redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import (
    capture, claude_resume, cli, codex_resume, conversation_guard as guard, operations, restore, storage,
)
from workspace_state.util import CommandError

SRC = str(Path(__file__).resolve().parent.parent / "src")
UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"
UUID_C = "33333333-3333-4333-8333-333333333333"


class RuntimeDirectoryCase(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.runtime = stack.enter_context(tempfile.TemporaryDirectory())
        stack.enter_context(patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.runtime}))

    def record_path(self, provider="codex", session_id=UUID_A):
        return guard.claim_path(provider, session_id)

    def write_record(self, value, provider="codex", session_id=UUID_A):
        path = self.record_path(provider, session_id)
        path.write_text(value if isinstance(value, str) else json.dumps(value))

    def read_record(self, provider="codex", session_id=UUID_A):
        text = self.record_path(provider, session_id).read_text()
        return json.loads(text) if text else None


class ClaimTests(RuntimeDirectoryCase):
    def test_first_claim_succeeds_and_records_this_process(self):
        self.assertTrue(guard.claim("codex", UUID_A))
        record = self.read_record()
        self.assertEqual(record["pid"], os.getpid())
        self.assertEqual(record["start_ticks"], guard._start_ticks(os.getpid()))
        self.assertEqual(record["boot_id"], guard._boot_id())

    def test_claim_file_name_is_lowercase_and_provider_qualified(self):
        self.assertTrue(guard.claim("claude", UUID_A.upper()))
        self.assertEqual(self.record_path("claude", UUID_A).name, f"claude-{UUID_A}.json")
        self.assertTrue(self.record_path("claude", UUID_A).is_file())

    def test_same_process_can_claim_again(self):
        self.assertTrue(guard.claim("codex", UUID_A))
        self.assertTrue(guard.claim("codex", UUID_A))

    def test_release_clears_own_record(self):
        guard.claim("codex", UUID_A)
        guard.release("codex", UUID_A)
        self.assertIsNone(self.read_record())
        self.assertTrue(guard.claim("codex", UUID_A))

    def test_release_keeps_record_of_another_live_process(self):
        parent = os.getppid()
        other = {"pid": parent, "start_ticks": guard._start_ticks(parent), "boot_id": guard._boot_id()}
        self.write_record(other)
        guard.release("codex", UUID_A)
        self.assertEqual(self.read_record(), other)

    def test_claim_is_refused_while_another_live_process_holds_it(self):
        parent = os.getppid()
        other = {"pid": parent, "start_ticks": guard._start_ticks(parent), "boot_id": guard._boot_id()}
        self.write_record(other)
        self.assertFalse(guard.claim("codex", UUID_A))
        self.assertEqual(self.read_record(), other)

    def test_stale_records_are_replaced(self):
        parent = os.getppid()
        ticks = guard._start_ticks(parent)
        boot = guard._boot_id()
        cases = {
            "wrong start ticks": {"pid": parent, "start_ticks": ticks + 1, "boot_id": boot},
            "wrong boot id": {"pid": parent, "start_ticks": ticks, "boot_id": "other-boot"},
            "dead pid": {"pid": 2 ** 22 + 12345, "start_ticks": 1, "boot_id": boot},
            "garbage json": "{not json",
            "non-object json": "[1, 2]",
            "missing ticks": {"pid": parent, "start_ticks": None, "boot_id": boot},
        }
        for label, value in cases.items():
            with self.subTest(label):
                self.write_record(value)
                self.assertTrue(guard.claim("codex", UUID_A))
                self.assertEqual(self.read_record()["pid"], os.getpid())

    def test_providers_and_uuids_are_independent(self):
        parent = os.getppid()
        self.write_record({"pid": parent, "start_ticks": guard._start_ticks(parent),
                           "boot_id": guard._boot_id()})
        self.assertTrue(guard.claim("claude", UUID_A))
        self.assertTrue(guard.claim("codex", UUID_B))

    def test_claim_survives_exec_until_the_new_program_exits(self):
        code = (
            "import os, sys\n"
            "from workspace_state import conversation_guard as g\n"
            f"assert g.claim('codex', '{UUID_A}')\n"
            "print('claimed', flush=True)\n"
            "os.execvp('sleep', ['sleep', '30'])\n"
        )
        env = {**os.environ, "PYTHONPATH": SRC}
        child = subprocess.Popen([sys.executable, "-c", code], env=env,
                                 stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.assertEqual(child.stdout.readline().strip(), "claimed")
        self.addCleanup(child.stdout.close)
        # The line is printed before exec; wait until the process image is sleep.
        deadline = time.monotonic() + 10
        while Path(f"/proc/{child.pid}/comm").read_text().strip() != "sleep":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        self.assertFalse(guard.claim("codex", UUID_A))
        child.kill()
        child.wait()
        self.assertTrue(guard.claim("codex", UUID_A))


class LiveLocationTests(unittest.TestCase):
    PANES = "\n".join([
        "%1\t100\tmain\t0\t0\t/synthetic/work",
        "%2\t200\tmain\t0\t1\t/synthetic/work",
        "%3\t300\tother\t1\t2\t/synthetic/work",
        "%4\t400\tmain\t2\t0\t/synthetic/work",
        "%5\t500\tthird\t0\t0\t/synthetic/work",
        "bad line",
    ])
    TABLE = (
        {100: [101], 200: [201], 300: [301], 400: [], 500: [501]},
        {100: "zsh", 101: "codex", 200: "zsh", 201: "claude", 300: "zsh", 301: "python3",
         400: "zsh", 500: "zsh", 501: "codex"},
    )

    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(guard, "_process_table", return_value=self.TABLE))
        self.identity = {
            100: ("codex", UUID_A.upper(), "resume-argv"),
            200: ("claude", UUID_B, "hook"),
            300: ("codex", UUID_C, "restore-wrapper"),
            500: ("codex", UUID_A, "rollout"),
        }
        self.calls = []

        def fake_identity(pid, cwd, pane_id, providers=("codex", "claude")):
            self.calls.append((pid, pane_id, set(providers)))
            found = self.identity.get(pid)
            return [found] if found else []

        stack.enter_context(patch.object(guard, "pane_identities", side_effect=fake_identity))
        self.runner = Mock(return_value=self.PANES)

    def test_keys_are_lowercase_and_locations_formatted(self):
        result = guard.live_locations(runner=self.runner)
        self.assertEqual(result, {
            f"codex:{UUID_A}": ["main:0.0", "third:0.0"],
            f"claude:{UUID_B}": ["main:0.1"],
            f"codex:{UUID_C}": ["other:1.2"],
        })
        self.runner.assert_called_once_with(["tmux", "list-panes", "-a", "-F", guard.PANE_FORMAT])

    def test_pane_without_provider_process_is_not_identified(self):
        guard.live_locations(runner=self.runner)
        self.assertNotIn(400, [call[0] for call in self.calls])
        self.assertEqual({call[0]: call[2] for call in self.calls},
                         {100: {"codex"}, 200: {"claude"}, 300: {"codex"}, 500: {"codex"}})

    def test_excluded_sessions_and_pane_are_skipped(self):
        result = guard.live_locations(exclude_sessions={"main", "third"}, runner=self.runner)
        self.assertEqual(result, {f"codex:{UUID_C}": ["other:1.2"]})
        result = guard.live_locations(exclude_pane="%1", runner=self.runner)
        self.assertEqual(result[f"codex:{UUID_A}"], ["third:0.0"])
        self.assertNotIn(100, [call[0] for call in self.calls[-4:]])

    def test_launchers_can_be_excluded(self):
        result = guard.live_locations(include_launchers=False, runner=self.runner)
        self.assertNotIn(f"codex:{UUID_C}", result)
        self.assertIn(f"codex:{UUID_A}", result)

    def test_resume_claim_counts_only_as_a_launcher(self):
        self.identity[200] = ("claude", UUID_B, "resume-claim")
        self.assertEqual(guard.live_locations(runner=self.runner)[f"claude:{UUID_B}"], ["main:0.1"])
        self.assertNotIn(f"claude:{UUID_B}", guard.live_locations(include_launchers=False,
                                                                  runner=self.runner))
        self.assertIsNone(guard.running_elsewhere("claude", UUID_B, runner=self.runner))
        self.assertNotIn(f"claude:{UUID_B}", guard.live_sessions(runner=self.runner))

    def test_runner_errors_give_empty_result(self):
        for error in (CommandError("no server"), FileNotFoundError("tmux")):
            with self.subTest(error=type(error).__name__):
                self.assertEqual(guard.live_locations(runner=Mock(side_effect=error)), {})

    def test_running_elsewhere_ignores_launchers_and_returns_first_location(self):
        self.assertEqual(guard.running_elsewhere("codex", UUID_A.upper(), runner=self.runner), "main:0.0")
        self.assertEqual(guard.running_elsewhere("codex", UUID_A, exclude_pane="%1", runner=self.runner),
                         "third:0.0")
        self.assertIsNone(guard.running_elsewhere("codex", UUID_C, runner=self.runner))
        self.assertIsNone(guard.running_elsewhere("claude", UUID_A, runner=self.runner))

    def test_live_sessions_maps_keys_to_session_names_without_launchers(self):
        self.assertEqual(guard.live_sessions(runner=self.runner), {
            f"codex:{UUID_A}": {"main", "third"},
            f"claude:{UUID_B}": {"main"},
        })

    def test_refuse_message_names_location_or_pending_launcher(self):
        self.assertIn("already runs in main:0.0", guard.refuse_message("codex", UUID_A, "main:0.0"))
        self.assertIn("is already being resumed", guard.refuse_message("claude", UUID_A, None))


class CodexTitleEvidenceTests(unittest.TestCase):
    def identity(self, found, title):
        with patch("workspace_state.capture.codex_for_pane", return_value=found), \
                patch("workspace_state.codex_title._pane_title", return_value=title):
            return guard._codex_identities(100, "/synthetic/work", "%7")

    def test_title_of_another_thread_keeps_both_threads_refuse_only(self):
        # The client moved on with /resume, or the title is stale: either way
        # neither thread may start elsewhere, and neither counts as verified.
        found = {"pid": 101, "session_id": UUID_A, "confidence": "command-line"}
        self.assertEqual(self.identity(found, ("%7", 100, UUID_B[:29] + "...")),
                         [(UUID_A, "title-conflict"), (UUID_B[:29] + "...", "native-title")])

    def test_matching_title_keeps_the_argv_identity(self):
        found = {"pid": 101, "session_id": UUID_A, "confidence": "command-line"}
        self.assertEqual(self.identity(found, ("%7", 100, UUID_A[:29] + "...")), [(UUID_A, "command-line")])

    def test_title_of_a_reused_pane_is_ignored(self):
        found = {"pid": 101, "session_id": UUID_A, "confidence": "command-line"}
        self.assertEqual(self.identity(found, ("%7", 999, UUID_B[:29] + "...")), [(UUID_A, "command-line")])

    def test_open_rollout_and_waiting_launcher_ignore_the_title(self):
        for evidence in ("open-rollout", "restore-wrapper"):
            with self.subTest(evidence=evidence):
                found = {"pid": 101, "session_id": UUID_A, "confidence": evidence}
                self.assertEqual(self.identity(found, ("%7", 100, UUID_B[:29] + "...")), [(UUID_A, evidence)])

    def test_unidentified_foreground_client_is_known_by_its_title_prefix(self):
        found = {"pid": 101, "session_id": None, "confidence": "unknown"}
        self.assertEqual(self.identity(found, ("%7", 100, UUID_A[:29] + "...")),
                         [(UUID_A[:29] + "...", "native-title")])
        self.assertEqual(self.identity(found, ("%7", 100, UUID_A)), [(UUID_A, "native-title")])
        self.assertEqual(self.identity(found, None), [])
        self.assertEqual(self.identity({"pid": 101, "session_id": None,
                                        "confidence": "conflicting-identities"},
                                       ("%7", 100, UUID_A[:29] + "...")), [])

    def test_title_evidence_refuses_but_never_verifies(self):
        panes = "%1\t100\tmain\t0\t0\t/synthetic/work\n%2\t200\twork\t0\t0\t/synthetic/work"
        found = {100: [("codex", UUID_A, "title-conflict"), ("codex", UUID_B[:29] + "...", "native-title")],
                 200: [("codex", UUID_C, "command-line")]}
        table = ({100: [101], 200: [201]}, {100: "zsh", 101: "codex", 200: "zsh", 201: "codex"})
        with patch.object(guard, "_process_table", return_value=table), \
                patch.object(guard, "pane_identities",
                             side_effect=lambda pid, *args, **kwargs: found[pid]):
            runner = Mock(return_value=panes)
            self.assertEqual(guard.running_elsewhere("codex", UUID_A, runner=runner), "main:0.0")
            self.assertEqual(guard.running_elsewhere("codex", UUID_B, runner=runner), "main:0.0")
            self.assertEqual(guard.live_sessions(runner=runner), {f"codex:{UUID_C}": {"work"}})

    def test_title_only_evidence_keeps_a_conversation_missing(self):
        key = f"codex:{UUID_A}"
        with patch.object(cli, "_mapped_missing_conversation_ids", return_value={key}), \
                patch.object(cli, "_actual_session_name", return_value="main"), \
                patch.object(guard, "live_locations",
                             side_effect=lambda **kwargs: {} if kwargs.get("verified_only")
                             else {f"codex:{UUID_A[:29]}...": ["other:1.0"]}):
            self.assertEqual(cli._missing_conversations([({"name": "main"}, "main")], None), {key})

    def test_matching_includes_title_prefixes_of_the_same_provider_only(self):
        index = {f"codex:{UUID_A}": ["main:1.0"], f"codex:{UUID_A[:29]}...": ["work:2.0"],
                 f"claude:{UUID_A[:29]}...": ["notes:0.0"], f"codex:{UUID_B[:29]}...": ["x:0.0"]}
        self.assertEqual(sorted(guard.matching(index, "codex", UUID_A.upper())), ["main:1.0", "work:2.0"])
        self.assertEqual(guard.matching(index, "claude", UUID_B), [])

    def test_running_elsewhere_finds_a_client_by_title_prefix(self):
        with patch.object(guard, "live_locations", return_value={f"codex:{UUID_A[:29]}...": ["work:2.0"]}):
            self.assertEqual(guard.running_elsewhere("codex", UUID_A), "work:2.0")
            self.assertIsNone(guard.running_elsewhere("codex", UUID_B))


class CodexLauncherGuardTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"TMUX_PANE": "%987"}))
        self.claim = stack.enter_context(patch.object(guard, "claim", return_value=True))
        self.elsewhere = stack.enter_context(patch.object(guard, "running_elsewhere", return_value=None))
        self.release = stack.enter_context(patch.object(guard, "release"))
        self.resume = stack.enter_context(patch.object(codex_resume, "_resume", return_value=0))
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(io.StringIO()))

    def test_refused_claim_does_not_start_or_release(self):
        self.claim.return_value = False
        self.assertEqual(codex_resume.resume(UUID_A), 0)
        self.resume.assert_not_called()
        self.release.assert_not_called()
        self.elsewhere.assert_not_called()
        self.assertIn("is already being resumed", self.output.getvalue())

    def test_live_client_elsewhere_refuses_and_releases(self):
        self.elsewhere.return_value = "main:0.1"
        self.assertEqual(codex_resume.resume(UUID_A), 0)
        self.resume.assert_not_called()
        self.release.assert_called_once_with("codex", UUID_A)
        self.assertIn("already runs in main:0.1", self.output.getvalue())
        self.elsewhere.assert_called_once_with("codex", UUID_A, exclude_pane="%987")

    def test_normal_path_resumes_then_releases(self):
        order = []
        self.resume.side_effect = lambda value: order.append("resume") or 0
        self.release.side_effect = lambda *args: order.append("release")
        self.assertEqual(codex_resume.resume(UUID_A), 0)
        self.assertEqual(order, ["resume", "release"])
        self.claim.assert_called_once_with("codex", UUID_A)

    def test_release_happens_when_resume_raises(self):
        self.resume.side_effect = RuntimeError("boom")
        with self.assertRaises(RuntimeError):
            codex_resume.resume(UUID_A)
        self.release.assert_called_once()

    def test_invalid_uuid_returns_2_without_claim(self):
        self.assertEqual(codex_resume.resume("not-a-uuid"), 2)
        self.claim.assert_not_called()
        self.resume.assert_not_called()

    def test_claim_oserror_returns_2(self):
        self.claim.side_effect = OSError("read-only")
        self.assertEqual(codex_resume.resume(UUID_A), 2)
        self.resume.assert_not_called()
        self.release.assert_not_called()


class ClaudeLauncherGuardTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.dict(os.environ, {"TMUX_PANE": "%987"}))
        stack.enter_context(patch.object(operations, "current", return_value=None))
        self.claim = stack.enter_context(patch.object(guard, "claim", return_value=True))
        self.elsewhere = stack.enter_context(patch.object(guard, "running_elsewhere", return_value=None))
        self.release = stack.enter_context(patch.object(guard, "release"))
        self.execvp = stack.enter_context(patch.object(claude_resume.os, "execvp"))
        self.output = io.StringIO()
        stack.enter_context(redirect_stdout(self.output))
        stack.enter_context(redirect_stderr(io.StringIO()))

    def test_refused_claim_neither_execs_nor_releases(self):
        self.claim.return_value = False
        self.assertEqual(claude_resume.main([UUID_A]), 0)
        self.execvp.assert_not_called()
        self.release.assert_not_called()
        self.assertIn("is already being resumed", self.output.getvalue())

    def test_live_client_elsewhere_refuses_and_releases(self):
        self.elsewhere.return_value = "work:1.0"
        self.assertEqual(claude_resume.main([UUID_A]), 0)
        self.execvp.assert_not_called()
        self.release.assert_called_once_with("claude", UUID_A)
        self.assertIn("already runs in work:1.0", self.output.getvalue())
        self.elsewhere.assert_called_once_with("claude", UUID_A, exclude_pane="%987")

    def test_exec_happens_while_the_claim_is_still_held(self):
        def execute(*args):
            self.release.assert_not_called()
            self.claim.assert_called_once_with("claude", UUID_A)

        self.execvp.side_effect = execute
        claude_resume.main([UUID_A])
        self.execvp.assert_called_once_with("claude", ["claude", "--resume", UUID_A])

    def test_failed_exec_releases_the_claim(self):
        self.execvp.side_effect = FileNotFoundError("claude")
        self.assertEqual(claude_resume.main([UUID_A]), 2)
        self.release.assert_called_once_with("claude", UUID_A)

    def test_invalid_uuid_is_rejected_before_claiming(self):
        self.assertEqual(claude_resume.main(["nope"]), 2)
        self.claim.assert_not_called()


def codex_pane(index, session_id):
    return {"index": index, "cwd": "/synthetic/work", "command": "codex", "codex": {"session_id": session_id}}


def saved_session(name="main", panes=None):
    return {"name": name, "windows": [{
        "index": 0, "name": "work", "layout": "synthetic-layout",
        "panes": panes or [codex_pane(0, UUID_A), codex_pane(1, UUID_B)],
    }]}


class RestoreGuardTests(unittest.TestCase):
    def test_without_running_elsewhere_strips_only_matching_pane(self):
        session = saved_session()
        session["windows"][0]["panes"][1] = {
            "index": 1, "cwd": "/synthetic/work", "command": "claude",
            "conversation": {"provider": "claude", "session_id": UUID_B, "persisted": True},
        }
        original = copy.deepcopy(session)
        running = {f"codex:{UUID_A}": ["other:2.1"]}
        stripped, actions = restore._without_running_elsewhere(session, running)
        self.assertEqual(session, original)
        panes = stripped["windows"][0]["panes"]
        self.assertIsNone(panes[0]["codex"])
        self.assertNotIn("conversation", panes[0])
        self.assertEqual(panes[1], session["windows"][0]["panes"][1])
        self.assertEqual(len(actions), 1)
        self.assertIn("already runs in other:2.1", actions[0])
        self.assertIn(UUID_A, actions[0])

    def test_claude_conversation_is_stripped_when_live(self):
        session = saved_session(panes=[{
            "index": 0, "cwd": "/synthetic/work",
            "conversation": {"provider": "claude", "session_id": UUID_B, "persisted": True}}])
        stripped, actions = restore._without_running_elsewhere(session, {f"claude:{UUID_B}": ["x:0.0"]})
        self.assertNotIn("conversation", stripped["windows"][0]["panes"][0])
        self.assertIsNone(stripped["windows"][0]["panes"][0]["codex"])
        self.assertEqual(len(actions), 1)

    def test_nothing_running_leaves_session_equal(self):
        session = saved_session()
        stripped, actions = restore._without_running_elsewhere(session, {})
        self.assertEqual(stripped, session)
        self.assertEqual(actions, [])

    def run_recreate(self, *, exists, running, legacy=None, same=None):
        commands = []
        index_calls = []

        def execute(command):
            commands.append(command)
            verb = command[1]
            if verb == "show-options":
                return "off\n" if command[-1] == "automatic-rename" else ""
            if verb == "display-message":
                return {
                    "#{window_id}": "@10\n", "#{window_index}": "0\n", "#{pane_id}": "%10\n",
                    "#{window_id}\n#{window_name}": "@10\nwork\n",
                }.get(command[-1], "%11\n")
            if verb == "split-window":
                return "%11\n"
            return ""

        foreign = {0: {"id": "@1", "name": "other", "panes": {
            0: {"id": "%1", "pid": 100, "cwd": "/elsewhere", "command": "zsh"}}}}
        with ExitStack() as stack:
            live_names = ({"main"} if exists else set()) | ({legacy} if legacy else set())
            stack.enter_context(patch.object(restore, "_tmux_exists",
                                             side_effect=lambda name: name in live_names))
            stack.enter_context(patch.object(restore, "_tagged_restore_session", return_value=legacy))
            stack.enter_context(patch.object(restore, "_tmux_state", return_value=foreign))
            stack.enter_context(patch.object(restore, "_live_conversation_ids", return_value={}))
            stack.enter_context(patch.object(restore, "_live_codex_ids", return_value={}))
            stack.enter_context(patch.object(restore, "codex_for_pane", return_value=None))
            if same is not None:
                stack.enter_context(patch.object(restore, "_same_tmux_session", side_effect=same))
                stack.enter_context(patch.object(restore, "_reconcile_tmux", return_value=["reconciled"]))

            def index(**kwargs):
                index_calls.append(kwargs)
                return running
            stack.enter_context(patch.object(restore.conversation_guard, "live_locations",
                                             side_effect=index))
            stack.enter_context(patch.object(restore, "run", side_effect=execute))
            stack.enter_context(patch.object(operations, "current", return_value=None))
            try:
                outcome = restore.recreate_tmux(saved_session())
            except CommandError as error:
                outcome = error
        self.index_calls = index_calls
        return outcome, commands

    def test_foreign_same_name_session_gets_a_copy_without_duplicate_clients(self):
        outcome, commands = self.run_recreate(
            exists=True, running={f"codex:{UUID_A}": ["other:3.0"]})
        self.assertEqual(outcome[0], "main-wsctl")
        creation = next(command for command in commands if command[1] == "new-session")
        self.assertEqual(creation[creation.index("-s") + 1], "main-wsctl")
        joined = "\n".join(" ".join(map(str, command)) for command in commands)
        self.assertNotIn(f"wsctl-codex-resume {UUID_A}", joined)
        self.assertIn(f"wsctl-codex-resume {UUID_B}", joined)
        # The copy is a new target: no live session is excluded from its index.
        self.assertEqual(self.index_calls[-1], {"exclude_sessions": {"main-wsctl"}})

    def test_adopted_legacy_copy_is_the_only_excluded_session(self):
        outcome, _commands = self.run_recreate(
            exists=False, running={}, legacy="main-wsctl", same=lambda *args, **kwargs: True)
        self.assertEqual(outcome, ("main-wsctl", ["reconciled"]))
        self.assertEqual(self.index_calls, [{"exclude_sessions": {"main-wsctl"}}])

    def test_unadopted_legacy_copy_counts_as_another_session(self):
        # The live main is the saved session; a legacy copy that is not adopted
        # keeps its conversations visible to the index.
        outcome, _commands = self.run_recreate(
            exists=True, running={}, legacy="main-wsctl", same=lambda *args, **kwargs: True)
        self.assertEqual(outcome, ("main", ["reconciled"]))
        self.assertEqual(self.index_calls, [{"exclude_sessions": {"main"}}])

    def test_fresh_restore_starts_plain_shell_for_conversation_live_elsewhere(self):
        outcome, commands = self.run_recreate(
            exists=False, running={f"codex:{UUID_A}": ["other:3.0"]})
        self.assertEqual(outcome[0], "main")
        self.assertTrue(any("already runs in other:3.0" in action for action in outcome[1]))
        creating = [" ".join(map(str, command)) for command in commands
                    if command[1] in {"new-session", "new-window", "split-window"}]
        self.assertTrue(creating)
        joined = "\n".join(creating)
        self.assertNotIn(f"wsctl-codex-resume {UUID_A}", joined)
        self.assertIn(f"wsctl-codex-resume {UUID_B}", joined)

    def test_partial_adoption_appends_only_conversations_live_nowhere(self):
        saved = saved_session(panes=[codex_pane(0, UUID_A), codex_pane(1, UUID_B), codex_pane(2, UUID_C)])
        # The user's newer layout moved A; B runs in another session; C is gone.
        live = {4: {"id": "@4", "name": "moved", "panes": {
            0: {"id": "%4", "pid": 104, "cwd": "/synthetic/work", "command": "codex"}}}}
        commands = []

        def execute(command):
            commands.append(command)
            if command[1] == "new-window":
                return "@20\n"
            if command[1] == "display-message":
                return "%20\n"
            if command[1] == "show-options":
                return "off\n" if command[-1] == "automatic-rename" else ""
            return ""

        with ExitStack() as stack:
            stack.enter_context(patch.object(restore, "_tmux_exists", return_value=True))
            stack.enter_context(patch.object(restore, "_tagged_restore_session", return_value=None))
            stack.enter_context(patch.object(restore, "_restore_fingerprint", return_value=None))
            stack.enter_context(patch.object(restore, "_tmux_state", return_value=live))
            stack.enter_context(patch.object(restore, "_live_conversation_ids",
                                             return_value={(4, 0): f"codex:{UUID_A}"}))
            stack.enter_context(patch.object(restore.conversation_guard, "live_locations",
                                             return_value={f"codex:{UUID_B}": ["other:3.0"]}))
            stack.enter_context(patch.object(restore, "read_window_names",
                                             return_value={"name": "work", "automatic_rename": False}))
            stack.enter_context(patch.object(restore, "run", side_effect=execute))
            stack.enter_context(patch.object(operations, "current", return_value=None))
            name, actions = restore.recreate_tmux(saved)
        self.assertEqual(name, "main")
        self.assertTrue(any("already runs in other:3.0" in action for action in actions))
        self.assertTrue(any("preserve its current layout" in action for action in actions))
        joined = "\n".join(" ".join(map(str, command)) for command in commands)
        self.assertIn(f"wsctl-codex-resume {UUID_C}", joined)
        self.assertNotIn(f"wsctl-codex-resume {UUID_A}", joined)
        self.assertNotIn(f"wsctl-codex-resume {UUID_B}", joined)
        self.assertNotIn("new-session", [command[1] for command in commands])

    def test_fresh_restore_without_live_conversations_resumes_all(self):
        _outcome, commands = self.run_recreate(exists=False, running={})
        joined = "\n".join(" ".join(map(str, command)) for command in commands)
        self.assertIn(f"wsctl-codex-resume {UUID_A}", joined)
        self.assertIn(f"wsctl-codex-resume {UUID_B}", joined)


class CliGuardTests(unittest.TestCase):
    KEY = f"codex:{UUID_A}"

    def missing(self, *, actual, live):
        pairs = [({"name": "main"}, "main")]
        with patch.object(cli, "_mapped_missing_conversation_ids", return_value={self.KEY}), \
                patch.object(cli, "_actual_session_name", return_value=actual), \
                patch.object(guard, "live_sessions", return_value=live):
            return cli._missing_conversations(pairs, None)

    def test_conversation_live_in_another_session_is_not_missing(self):
        self.assertEqual(self.missing(actual="main", live={self.KEY: {"other"}}), set())

    def test_conversation_live_only_in_own_session_stays_missing(self):
        self.assertEqual(self.missing(actual="main", live={self.KEY: {"main"}}), {self.KEY})

    def test_not_live_anywhere_stays_missing(self):
        self.assertEqual(self.missing(actual="main", live={}), {self.KEY})

    def test_unmapped_session_keeps_conversation_missing(self):
        self.assertEqual(self.missing(actual=None, live={self.KEY: {"other"}}), {self.KEY})

    def test_one_failed_session_does_not_stop_the_others(self):
        sessions = [
            {"name": name, "launch_terminal": False,
             "windows": [{"index": 0, "panes": [codex_pane(0, uuid)]}]}
            for name, uuid in (("first", UUID_A), ("second", UUID_B))
        ]
        attempted = []

        def recreate(item, **kwargs):
            attempted.append(item["name"])
            if item["name"] == "first":
                raise CommandError("tmux session first belongs to a different live session")
            return item["name"], []

        args = Namespace(workspace=None, session=None, select=False, dry_run=False, no_place=True,
                         repair_processes=False, adopt_restored=False, verify_codex=False,
                         wait=0, login_status=False)
        with patch.object(cli, "_marker_context", return_value=None), \
                patch.object(cli, "_filtered_terminal_items", return_value=sessions), \
                patch.object(cli, "_live_terminal_clients", return_value={}), \
                patch.object(cli, "recreate_tmux", side_effect=recreate), \
                patch.object(cli, "update_stage"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError) as raised:
                cli._restore_terminals({}, args)
        self.assertEqual(attempted, ["first", "second"])
        self.assertIn("belongs to a different live session", str(raised.exception))

    def test_failed_session_is_neither_published_nor_verified(self):
        sessions = [
            {"name": name, "launch_terminal": False,
             "windows": [{"index": 0, "panes": [codex_pane(0, uuid)]}]}
            for name, uuid in (("first", UUID_A), ("second", UUID_B))
        ]

        def recreate(item, **kwargs):
            if item["name"] == "first":
                raise CommandError("tmux session first belongs to a different live session")
            return item["name"], []

        published, verified = [], []

        def missing(pairs, context):
            verified.append([session["name"] for session, _name in pairs])
            return set()

        from workspace_state import terminal_restore_map
        args = Namespace(workspace=None, session=None, select=False, dry_run=False, no_place=True,
                         repair_processes=False, adopt_restored=False, verify_codex=True,
                         wait=0, login_status=True)
        with patch.object(cli, "_marker_context", return_value=object()), \
                patch.object(cli, "_startup_directory", return_value=Path("/synthetic/startup")), \
                patch.object(cli, "status_path", return_value=Path("/synthetic/status.json")), \
                patch.object(terminal_restore_map, "publish",
                             side_effect=lambda path, items, names, context, status: published.append(
                                 [item["name"] for item in items])), \
                patch.object(cli, "_filtered_terminal_items", return_value=sessions), \
                patch.object(cli, "_live_terminal_clients", return_value={}), \
                patch.object(cli, "recreate_tmux", side_effect=recreate), \
                patch.object(cli, "_missing_conversations", side_effect=missing), \
                patch.object(cli, "_unidentified_codex_panes", return_value=[]), \
                patch.object(cli, "update_stage"), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(RuntimeError):
                cli._restore_terminals({}, args)
        self.assertEqual(published, [["second"]])
        self.assertTrue(verified)
        self.assertTrue(all(names == ["second"] for names in verified))


class CaptureDedupTests(unittest.TestCase):
    def sessions(self, *panes):
        return {"main": {"name": "main", "windows": {
            0: {"index": 0, "panes": [dict(pane, index=index) for index, pane in enumerate(panes)]}}}}

    def with_ticks(self, ticks):
        def identity(pid, _root):
            return Mock(start_ticks=ticks[pid]) if pid in ticks else None
        return patch.object(capture, "_process_identity", side_effect=identity)

    def test_later_client_of_same_codex_uuid_loses_identity(self):
        sessions = self.sessions(
            {"codex": {"session_id": UUID_A.upper(), "pid": 11}},
            {"codex": {"session_id": UUID_A, "pid": 12}},
        )
        with self.with_ticks({11: 900, 12: 100}):
            warnings = capture._keep_one_pane_per_conversation(sessions)
        first, second = sessions["main"]["windows"][0]["panes"]
        self.assertIsNone(first["codex"])
        self.assertEqual(second["codex"]["session_id"], UUID_A)
        self.assertEqual(len(warnings), 1)
        self.assertIn("main:0.1", warnings[0])
        self.assertIn("main:0.0", warnings[0])

    def test_claude_duplicates_are_deduplicated(self):
        def pane(pid):
            return {"conversation": {"provider": "claude", "session_id": UUID_B,
                                     "persisted": True, "pid": pid}}
        sessions = self.sessions(pane(21), pane(22))
        with self.with_ticks({21: 5, 22: 50}):
            warnings = capture._keep_one_pane_per_conversation(sessions)
        first, second = sessions["main"]["windows"][0]["panes"]
        self.assertIn("conversation", first)
        self.assertNotIn("conversation", second)
        self.assertIsNone(second["codex"])
        self.assertEqual(len(warnings), 1)

    def test_unknown_process_sorts_last_and_ties_use_location(self):
        sessions = self.sessions(
            {"codex": {"session_id": UUID_A, "pid": 99}},
            {"codex": {"session_id": UUID_A, "pid": 12}},
        )
        with self.with_ticks({12: 100}):
            capture._keep_one_pane_per_conversation(sessions)
        first, second = sessions["main"]["windows"][0]["panes"]
        self.assertIsNone(first["codex"])
        self.assertIsNotNone(second["codex"])
        sessions = self.sessions(
            {"codex": {"session_id": UUID_A, "pid": 1}},
            {"codex": {"session_id": UUID_A, "pid": 2}},
        )
        with self.with_ticks({1: 7, 2: 7}):
            capture._keep_one_pane_per_conversation(sessions)
        first, second = sessions["main"]["windows"][0]["panes"]
        self.assertIsNotNone(first["codex"])
        self.assertIsNone(second["codex"])

    def test_unique_and_unresolved_identities_are_untouched(self):
        sessions = self.sessions(
            {"codex": {"session_id": UUID_A, "pid": 1}},
            {"codex": {"session_id": UUID_B, "pid": 2}},
            {"codex": {"session_id": None, "confidence": "unknown"}},
            {"codex": {"session_id": None, "confidence": "unknown"}},
            {"command": "zsh"},
        )
        original = copy.deepcopy(sessions)
        with self.with_ticks({1: 1, 2: 2}):
            warnings = capture._keep_one_pane_per_conversation(sessions)
        self.assertEqual(warnings, [])
        self.assertEqual(sessions, original)

    def test_same_uuid_for_different_providers_is_not_a_duplicate(self):
        sessions = self.sessions(
            {"codex": {"session_id": UUID_A, "pid": 1}},
            {"conversation": {"provider": "claude", "session_id": UUID_A, "persisted": True, "pid": 2}},
        )
        with self.with_ticks({1: 1, 2: 2}):
            self.assertEqual(capture._keep_one_pane_per_conversation(sessions), [])


class SaveDropsWarningsTests(unittest.TestCase):
    def test_capture_warnings_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {"XDG_DATA_HOME": directory, "XDG_RUNTIME_DIR": directory}):
            snapshot = {"sessions": [], "terminals": [],
                        "capture_warnings": {"conversations": ["synthetic warning"]}}
            path = storage.save(snapshot)
            saved = json.loads(path.read_text())
        self.assertNotIn("capture_warnings", saved)


if __name__ == "__main__":
    unittest.main()
