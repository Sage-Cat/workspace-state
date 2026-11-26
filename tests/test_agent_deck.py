from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from workspace_state import agent_deck


NATIVE = "01234567-89ab-4cde-8fab-0123456789ab"
DECK = Path("/usr/bin/agent-deck")


def reply(payload):
    return subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")


def profiles(*names):
    return reply({"success": True, "total": len(names), "profiles": [{"name": n} for n in names]})


def cli_row(**overrides):
    return {
        "profile": "default", "id": "c0958511-1795673077", "group": "projects", "account": "",
        "tool": "codex", "title": "Local work", "codex_session_id": NATIVE, "status": "running",
        "status_source": "live", "tmux_session": "agentdeck-local", "archived": False,
        **overrides,
    }


def saved_row(**overrides):
    return {
        "profile": "default", "id": "c0958511-1795673077", "group": "projects", "account": "",
        "tool": "codex", "title": "Local work", "native_id": NATIVE, "open": True, "archived": False,
        **overrides,
    }


class AgentDeckInventoryTests(unittest.TestCase):
    def test_complete_capture_keeps_all_profiles_and_archives(self):
        default = cli_row(transcript_path="/private/transcript", token="must not be copied")
        other = cli_row(profile="example", tool="other harness", status="stopped",
                        archived=True, status_source="cached")
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("example", "default"), reply([default]), reply([other]),
                  profiles("default", "example"),
              ]) as run):
            payload = agent_deck.inventory()
        self.assertEqual(payload, {"version": 1, "sessions": [
            saved_row(), saved_row(profile="example", tool="other harness", native_id=None,
                                   archived=True, open=False),
        ]})
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            [str(DECK), "profile", "list", "--json"],
            [str(DECK), "-p", "default", "list", "--json", "--include-superseded"],
            [str(DECK), "-p", "example", "list", "--json", "--include-superseded"],
            [str(DECK), "profile", "list", "--json"],
        ])

    def test_capture_rejects_incomplete_or_mixed_inventory(self):
        cases = [
            ("profiles changed", [profiles("default"), reply([]), profiles("other")]),
            ("outside the requested profile", [profiles("default"), reply([cli_row(profile="other")])]),
            ("status could not be observed", [profiles("default"), reply([cli_row(status_source="stale")])]),
            ("duplicate Agent Deck", [profiles("default"), reply([cli_row(), cli_row()]), profiles("default")]),
            ("invalid Agent Deck native", [profiles("default"), reply([cli_row(codex_session_id="wrong")])]),
            ("incomplete Agent Deck profile list", [reply({"success": True, "total": 2, "profiles": []})]),
        ]
        for message, replies in cases:
            with (self.subTest(message=message),
                  patch.object(agent_deck, "_executable", return_value=DECK),
                  patch.object(agent_deck.subprocess, "run", side_effect=replies),
                  self.assertRaisesRegex(agent_deck.AgentDeckError, message)):
                agent_deck.inventory()

    def test_capture_shares_one_deadline_between_commands(self):
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck.time, "monotonic", side_effect=[100.0, 100.0, 104.0, 105.0]),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("default"), reply([]),
              ]) as run):
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "timed out"):
                agent_deck.inventory(timeout=5)
        self.assertEqual([c.kwargs["timeout"] for c in run.call_args_list], [5.0, 1.0])

    def test_unavailable_binary_keeps_previously_configured_ownership(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config, data, legacy = [root / name for name in ("config", "data", "legacy")]
            with (patch.object(agent_deck, "_roots", return_value=(config, data, legacy)),
                  patch.object(agent_deck, "_executable", return_value=None)):
                self.assertFalse(agent_deck.enabled())
                (data / "profiles").mkdir(parents=True)
                self.assertTrue(agent_deck.enabled())
                with self.assertRaisesRegex(agent_deck.AgentDeckError, "unavailable"):
                    agent_deck.inventory()

    def test_invalid_checkpoint_records_do_not_pass_validation(self):
        for row in (saved_row(id="--help"), saved_row(profile="../another"),
                    saved_row(archived=True), saved_row(open=1), saved_row(group="a/../b")):
            with self.subTest(row=row), self.assertRaises(ValueError):
                agent_deck.validate({"version": 1, "sessions": [row]})


class AgentDeckStartTests(unittest.TestCase):
    def test_exact_start_uses_only_guarded_cli_and_ignores_foreign_session_overrides(self):
        row = saved_row()
        result = reply({"success": True, "id": row["id"], "native_id": NATIVE, "tmux": "local-session"})
        inherited = {
            "HOME": "/example", "XDG_RUNTIME_DIR": "/example/runtime", "PATH": "/usr/bin",
            "AGENTDECK_PROFILE": "other", "AGENTDECK_ACCOUNT": "something-else", "TMUX": "foreign",
            "CLAUDE_CONFIG_DIR": "/elsewhere", "CODEX_HOME": "/elsewhere", "OPENAI_API_KEY": "unused",
        }
        with (patch.dict(os.environ, inherited, clear=True),
              patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck.subprocess, "run", return_value=result) as run):
            agent_deck.start_exact(row, timeout=5)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], [
            str(DECK), "-p", "default", "session", "start", row["id"],
            "--expected-native-id", NATIVE, "--json",
        ])
        self.assertEqual(run.call_args.kwargs["env"], {
            "HOME": "/example", "XDG_RUNTIME_DIR": "/example/runtime", "PATH": "/usr/bin",
            "AGENTDECK_SKIP_UPDATE_CHECK": "1",
        })
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)

    def test_exact_start_rejects_failures_without_an_unguarded_retry(self):
        row = saved_row()
        good = {"success": True, "id": row["id"], "native_id": NATIVE, "tmux": "local-session"}
        bad = [
            reply(dict(good, id="different")),
            reply(dict(good, native_id="different")),
            reply(dict(good, status="queued")),
            reply(dict(good, success=False)),
            reply(dict(good, tmux="")),
            reply(dict(good, deferred=True)),
            subprocess.CompletedProcess([], 1, b"", b"unknown flag --expected-native-id"),
            subprocess.CompletedProcess([], 0, b"no json", b""),
        ]
        for response in bad:
            with (self.subTest(response=response),
                  patch.object(agent_deck, "_executable", return_value=DECK),
                  patch.object(agent_deck.subprocess, "run", return_value=response) as run,
                  self.assertRaises(agent_deck.AgentDeckError)):
                agent_deck.start_exact(row, timeout=5)
            self.assertEqual(run.call_count, 1)


class AgentDeckDashboardTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.process = self.root / "123"
        self.process.mkdir()
        self.binary = self.root / "agent-deck"
        self.binary.touch()
        (self.process / "exe").symlink_to(self.binary)
        (self.process / "fd").mkdir()

    def command(self, *args):
        (self.process / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in args) + b"\0")
        with patch.object(agent_deck, "_executable", return_value=self.binary):
            return agent_deck.dashboard(123, proc_root=self.root)

    def test_scoped_dashboard_round_trip(self):
        recipe = self.command(self.binary, "--profile=work", "-g", "team/project")
        self.assertEqual(recipe, {"profile": "work", "group": "team/project"})
        with patch.object(agent_deck, "_executable", return_value=self.binary):
            self.assertEqual(agent_deck.dashboard_command(recipe), [
                "env", "AGENT_DECK_ALLOW_OUTER_TMUX=1", str(self.binary), "-p", "work", "-g", "team/project",
            ])

    def test_implicit_profile_requires_open_store_evidence(self):
        data = self.root / "data"
        with patch.object(agent_deck, "_roots", return_value=(self.root, data, self.root / "legacy")):
            self.assertIsNone(self.command(self.binary, "-g", "team"))
            state = data / "profiles/default/state.db"
            state.parent.mkdir(parents=True)
            state.touch()
            (self.process / "fd/3").symlink_to(state)
            self.assertEqual(self.command(self.binary, "-g", "team"), {"profile": "default", "group": "team"})

    def test_rejects_subcommands_and_unsafe_restore_flags(self):
        for args in (("session", "start"), ("-p", "work", "--run-repo-scripts"),
                     ("-p", "work", "-g", "team", "--group=changed"), ("-g", "../wrong")):
            with self.subTest(args=args):
                self.assertIsNone(self.command(self.binary, *args))
        with self.assertRaises(ValueError):
            agent_deck.dashboard_command({"profile": "work", "group": "", "args": ["--run-repo-scripts"]})

    def test_named_but_unrelated_executable_is_not_a_dashboard(self):
        foreign = self.root / "foreign/agent-deck"
        foreign.parent.mkdir()
        foreign.touch()
        (self.process / "exe").unlink()
        (self.process / "exe").symlink_to(foreign)
        self.assertIsNone(self.command(foreign, "-p", "work"))


if __name__ == "__main__":
    unittest.main()
