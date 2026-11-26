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


def panes(*rows):
    return subprocess.CompletedProcess([], 0, "\n".join(rows).encode(), b"")


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
        "tool": "codex", "title": "Local work", "native_id": NATIVE,
        "open": True, "stopped": False, "archived": False,
        **overrides,
    }


def process(root, pid, *, name="bash", parent=1, group=None, foreground=None, tty="/dev/pts/2"):
    folder = root / str(pid)
    folder.mkdir(exist_ok=True)
    fields = ["S", str(parent), str(group or pid), "123", "34818", str(foreground or pid)]
    fields.extend(["0"] * 13 + [str(1000 + pid)])
    (folder / "stat").write_text(f"{pid} ({name}) {' '.join(fields)}\n")
    (folder / "comm").write_text(f"{name}\n")
    (folder / "fd").mkdir(exist_ok=True)
    (folder / "fd/0").unlink(missing_ok=True)
    (folder / "fd/0").symlink_to(tty)
    return folder


def command_line(folder, *args):
    (folder / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in args) + b"\0")


class AgentDeckInventoryTests(unittest.TestCase):
    def test_complete_capture_keeps_all_profiles_and_archives(self):
        default = cli_row(transcript_path="/private/transcript", token="must not be copied")
        other = cli_row(profile="example", tool="other harness", status="stopped",
                        archived=True, status_source="cached")
        visible_routes = set()
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck, "_foreground_provider", return_value="codex"),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("example", "default"), reply([default]), reply([other]),
                  panes("%0|1001|/dev/pts/2|0|agentdeck-local"), profiles("default", "example"),
              ]) as run):
            payload = agent_deck.inventory(visible_routes=visible_routes)
        self.assertEqual(visible_routes, {"agentdeck-local"})
        self.assertEqual(payload, {"version": 1, "sessions": [
            saved_row(), saved_row(profile="example", tool="other harness", native_id=None,
                                   archived=True, open=False, stopped=True),
        ]})
        self.assertEqual([c.args[0] for c in run.call_args_list], [
            [str(DECK), "profile", "list", "--json"],
            [str(DECK), "-p", "default", "list", "--json", "--include-superseded"],
            [str(DECK), "-p", "example", "list", "--json", "--include-superseded"],
            ["tmux", "-L", "default", "list-panes", "-a", "-F",
             "#{pane_id}|#{pane_pid}|#{pane_tty}|#{pane_dead}|#{session_name}"],
            [str(DECK), "profile", "list", "--json"],
        ])

    def test_capture_rejects_incomplete_or_mixed_inventory(self):
        cases = [
            ("profiles changed", [profiles("default"), reply([]), profiles("other")]),
            ("outside the requested profile", [profiles("default"), reply([cli_row(profile="other")])]),
            ("invalid Agent Deck tmux route", [profiles("default"), reply([cli_row(tmux_socket_name="--bad")])]),
            ("duplicate Agent Deck", [profiles("default"), reply([cli_row(tmux_session="")] * 2), profiles("default")]),
            ("invalid Agent Deck native", [profiles("default"), reply([cli_row(codex_session_id="wrong")])]),
            ("incomplete Agent Deck profile list", [reply({"success": True, "total": 2, "profiles": []})]),
        ]
        for message, replies in cases:
            with (self.subTest(message=message),
                  patch.object(agent_deck, "_executable", return_value=DECK),
                  patch.object(agent_deck.subprocess, "run", side_effect=replies),
                  self.assertRaisesRegex(agent_deck.AgentDeckError, message)):
                agent_deck.inventory()

    def test_live_status_alone_does_not_mark_a_shell_or_stale_route_open(self):
        rows = [cli_row(), cli_row(id="stopped", tmux_session="absent", status="stopped", status_source="cached"),
                cli_row(id="stale", tmux_session="absent", status="stopped", status_source="stale")]
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck, "_foreground_provider", return_value=None),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("default"), reply(rows), panes("%0|1001|/dev/pts/2|0|agentdeck-local"),
                  profiles("default"),
              ]) as run):
            visible_routes = set()
            state = agent_deck.inventory(visible_routes=visible_routes)
        self.assertFalse(visible_routes)
        self.assertEqual(run.call_count, 4)
        self.assertEqual([(row["open"], row["stopped"]) for row in state["sessions"]],
                         [(False, False), (False, True), (False, False)])

    def test_named_socket_and_default_routes_are_observed_independently(self):
        rows = [cli_row(id="default"), cli_row(id="named", tmux_socket_name="deck-workers", status_source="stale")]
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck, "_foreground_provider", return_value="codex"),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("default"), reply(rows), panes("%0|2001|/dev/pts/3|0|agentdeck-local"),
                  subprocess.CompletedProcess([], 1, b"", b"no server running on /tmp/test/default\n"),
                  profiles("default"),
              ]) as run):
            visible_routes = set()
            state = agent_deck.inventory(visible_routes=visible_routes)
        self.assertFalse(visible_routes)
        self.assertEqual([row["open"] for row in state["sessions"]], [False, True])
        self.assertEqual([call.args[0][:3] for call in run.call_args_list[2:4]],
                         [["tmux", "-L", "deck-workers"], ["tmux", "-L", "default"]])

    def test_ambiguous_route_does_not_publish_any_partial_observation(self):
        rows = [cli_row(), cli_row(id="another")]
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck, "_foreground_provider", return_value="codex"),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("default"), reply(rows), panes("%0|1001|/dev/pts/2|0|agentdeck-local"),
              ])):
            visible_routes = {"existing"}
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "ambiguous.*tmux route"):
                agent_deck.inventory(visible_routes=visible_routes)
        self.assertEqual(visible_routes, {"existing"})

    def test_matching_native_foreground_and_ancestry_are_required(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            process(root, 123, foreground=400)
            primary = process(root, 400, name="codex", parent=123)
            child = process(root, 401, name="claude", parent=400, group=400, foreground=400)
            command_line(primary, "/home/example/.local/bin/codex", "resume", NATIVE)
            command_line(child, "/home/example/.local/bin/claude", "--print", "background")
            pane = agent_deck._Pane(123, "/dev/pts/2")
            self.assertEqual(agent_deck._foreground_provider(pane, root), "codex")
            command_line(child, "claude", "--resume", NATIVE)
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "ambiguous.*foreground"):
                agent_deck._foreground_provider(pane, root)
            process(root, 401, name="claude", parent=400, group=400, foreground=400, tty="/dev/pts/3")
            self.assertEqual(agent_deck._foreground_provider(pane, root), "codex")
            command_line(primary, "codex", "exec", "background")
            self.assertIsNone(agent_deck._foreground_provider(pane, root))
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "pane process changed"):
                agent_deck._foreground_provider(agent_deck._Pane(123, "/dev/pts/3"), root)

    def test_capture_shares_one_deadline_between_commands(self):
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck.time, "monotonic", side_effect=[100.0, 100.0, 104.0, 105.0]),
              patch.object(agent_deck.subprocess, "run", side_effect=[
                  profiles("default"), reply([]),
              ]) as run):
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "timed out"):
                agent_deck.inventory(timeout=5)
        self.assertEqual([c.kwargs["timeout"] for c in run.call_args_list], [5.0, 1.0])

    def test_live_client_with_conflicting_native_identity_refuses_capture(self):
        other = "22222222-2222-4222-8222-222222222222"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            pane = agent_deck._Pane(123, "/dev/pts/2")
            codex = process(root, 123, name="codex")
            command_line(codex, "codex", "resume", NATIVE)
            with patch.object(agent_deck, "_open_rollout_sessions", return_value={other}), \
                    self.assertRaisesRegex(agent_deck.AgentDeckError, "disagree on the native UUID"):
                agent_deck._foreground_provider(pane, root, binding=("codex", NATIVE))
            claude = process(root, 123, name="claude")
            command_line(claude, "claude", f"--resume={other}")
            with self.assertRaisesRegex(agent_deck.AgentDeckError, "disagree on the native UUID"):
                agent_deck._foreground_provider(pane, root, binding=("claude", NATIVE))

    def test_unavailable_binary_keeps_previously_configured_ownership(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            config, data, legacy = [root / name for name in ("config", "data", "legacy")]
            with (patch.object(agent_deck, "_roots", return_value=(config, data, legacy)),
                  patch.object(agent_deck, "_executable", return_value=None)):
                self.assertFalse(agent_deck.enabled())
                legacy.mkdir(parents=True)
                (legacy / "config.toml").touch()
                self.assertFalse(agent_deck.enabled())
                store = data / "profiles/work/state.db"
                store.parent.mkdir(parents=True)
                self.assertFalse(agent_deck.enabled())
                store.touch()
                self.assertTrue(agent_deck.enabled())
                with self.assertRaisesRegex(agent_deck.AgentDeckError, "unavailable"):
                    agent_deck.inventory()

    def test_invalid_checkpoint_records_do_not_pass_validation(self):
        for row in (saved_row(id="--help"), saved_row(profile="../another"),
                    saved_row(archived=True), saved_row(open=1), saved_row(group="a/../b"),
                    saved_row(stopped=True)):
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
            "--expected-native-id", NATIVE, "--expected-native-tool", "codex",
            "--expected-account", "", "--expected-group", "projects", "--json",
        ])
        self.assertEqual(run.call_args.kwargs["env"], {
            "HOME": "/example", "XDG_RUNTIME_DIR": "/example/runtime", "PATH": "/usr/bin",
            "AGENTDECK_SKIP_UPDATE_CHECK": "1",
        })
        self.assertGreater(run.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(run.call_args.kwargs["timeout"], 5)

    def test_claude_start_sends_exact_account_and_empty_group(self):
        row = saved_row(tool="claude", account="lpnu", group="")
        result = reply({"success": True, "id": row["id"], "native_id": NATIVE, "tmux": "local-session"})
        with (patch.object(agent_deck, "_executable", return_value=DECK),
              patch.object(agent_deck.subprocess, "run", return_value=result) as run):
            agent_deck.start_exact(row, timeout=5)
        self.assertEqual(run.call_args.args[0][-7:], [
            "--expected-native-tool", "claude", "--expected-account", "lpnu", "--expected-group", "", "--json",
        ])

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
        self.process = process(self.root, 123)
        self.binary = self.root / "agent-deck"
        self.binary.touch()
        self.binary.chmod(0o755)
        (self.process / "exe").symlink_to(self.binary)

    def command(self, *args):
        command_line(self.process, *args)
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

    def test_orphaned_old_image_matches_only_the_approved_executable_path_and_mode(self):
        old = self.root / "agent-deck (deleted)"
        old.touch()
        old.chmod(0o755)
        (self.process / "exe").unlink()
        (self.process / "exe").symlink_to(old)
        self.assertEqual(self.command(self.binary, "-p", "work"), {"profile": "work", "group": ""})
        old.chmod(0o777)
        self.assertIsNone(self.command(self.binary, "-p", "work"))
        old.chmod(0o755)
        foreign = self.root / "foreign"
        foreign.mkdir()
        old.rename(foreign / old.name)
        (self.process / "exe").unlink()
        (self.process / "exe").symlink_to(foreign / old.name)
        self.assertIsNone(self.command(self.binary, "-p", "work"))

    def test_dashboard_under_shell_requires_unique_foreground_and_same_tty(self):
        process(self.root, 123, foreground=400)
        (self.process / "exe").unlink()
        child = process(self.root, 400, parent=123, name="agent-deck")
        (child / "exe").symlink_to(self.binary)
        command_line(child, self.binary, "-p", "work", "-g", "Main")
        self.assertEqual(self.command("bash"), {"profile": "work", "group": "Main"})
        second = process(self.root, 401, parent=400, name="agent-deck", group=400, foreground=400)
        (second / "exe").symlink_to(self.binary)
        command_line(second, self.binary, "-p", "work", "-g", "Main")
        self.assertIsNone(self.command("bash"))
        process(self.root, 400, parent=123, name="agent-deck", tty="/dev/pts/3")
        process(self.root, 401, parent=400, name="agent-deck", group=400, foreground=400, tty="/dev/pts/3")
        self.assertIsNone(self.command("bash"))


if __name__ == "__main__":
    unittest.main()
