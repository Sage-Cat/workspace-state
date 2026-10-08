from __future__ import annotations

from contextlib import ExitStack
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from workspace_state import codex_resume, codex_title


SESSION = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
COLLISION = "11111111-1111-4111-8111-11111aaaaaaa"
REALTIME = 1_700_000_000_000_000_000
BOOTTIME = 10_000_000_000
EARLIEST_START = REALTIME - BOOTTIME + 1_000_000_000
LATEST_START_MS = (EARLIEST_START + 10_000_000) // 1_000_000


class CodexTitleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        temporary = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.home = temporary / "codex-home"
        self.home.mkdir()
        self.cwd = temporary / "work"
        self.cwd.mkdir()
        self.proc_root = temporary / "proc"
        self.proc_root.mkdir()
        self._process(123, comm="zsh", command=b"zsh\0", group=123, ticks=50)
        self._process(456)
        self._config('[tui]\nterminal_title = ["thread-id"]\n')
        self._database({SESSION: LATEST_START_MS + 100, OTHER: LATEST_START_MS + 200})
        self.title = SESSION[:29] + "..."
        self.pane_pid = 123
        self.row = 0
        self.in_mode = 0
        self.screen = "› Ask Codex to do anything\n\n  GPT-6-Astra default · /work…"
        self.stack.enter_context(patch.object(codex_resume, "PROC_ROOT", self.proc_root))
        self.system_config = temporary / "system-config.toml"
        self.stack.enter_context(patch.object(codex_title, "_SYSTEM_CONFIG", self.system_config))
        self.tmux = self.stack.enter_context(patch.object(
            codex_title.subprocess, "run", side_effect=self._metadata,
        ))
        self.capture = self.stack.enter_context(patch.object(
            codex_resume, "pane_text", side_effect=lambda _pane: self.screen,
        ))
        self.loaded = self.stack.enter_context(patch.object(
            codex_title, "loaded_thread_ids", return_value={SESSION},
        ))
        self.clock = self.stack.enter_context(patch.object(
            codex_title.time, "clock_gettime_ns", return_value=BOOTTIME,
        ))
        self.stack.enter_context(patch.object(codex_title.time, "time_ns", return_value=REALTIME))
        self.frequency = self.stack.enter_context(patch.object(codex_title.os, "sysconf", return_value=100))

    def _process(self, pid, *, comm="codex", command=b"codex\0", parent=123,
                 group=456, foreground=456, tty="/dev/pts/7", ticks=100):
        process = self.proc_root / str(pid)
        process.mkdir(exist_ok=True)
        fields = ["S", str(parent), str(group), "123", "34823", str(foreground),
                  *(["0"] * 13), str(ticks)]
        (process / "stat").write_text(f"{pid} ({comm}) {' '.join(fields)}\n")
        (process / "comm").write_text(comm + "\n")
        (process / "cmdline").write_bytes(command)
        (process / "environ").write_bytes(os.fsencode(f"CODEX_HOME={self.home}") + b"\0")
        (process / "fd").mkdir(exist_ok=True)
        for descriptor, target in ((process / "fd" / "0", Path(tty)), (process / "cwd", self.cwd)):
            if descriptor.is_symlink():
                descriptor.unlink()
            descriptor.symlink_to(target)

    def _config(self, text, *, mtime=EARLIEST_START - 1_000_000_000):
        config = self.home / "config.toml"
        config.write_text(text)
        os.utime(config, ns=(mtime, mtime))

    def _database(self, rows):
        with sqlite3.connect(self.home / "state_5.sqlite") as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT PRIMARY KEY, created_at_ms INTEGER)")
            connection.execute("DELETE FROM threads")
            connection.executemany("INSERT INTO threads VALUES (?, ?)", rows.items())

    def _metadata(self, command, **options):
        self.assertEqual(command[:5], ["tmux", "display-message", "-p", "-t", "%987"])
        self.assertEqual(options["timeout"], 1)
        if command[-1] == "#{pane_id}\t#{pane_pid}\t#{pane_title}":
            output = f"%987\t{self.pane_pid}\t{self.title}\n"
        else:
            output = f"/dev/pts/7\t{self.row}\t2\t{self.in_mode}\t{self.pane_pid}\n"
        return subprocess.CompletedProcess(command, 0, output, "")

    def _proof(self):
        return codex_title.native_title_proof(456, "%987", self.home)

    def test_full_and_exact_native_truncated_titles_prove_the_fresh_uuid(self):
        for title in (SESSION, SESSION[:29] + "..."):
            with self.subTest(title=title):
                self.title = title
                self.assertEqual(self._proof(), codex_title.NativeTitleProof(SESSION, 456, "%987", 123, 100, title))
        self.loaded.assert_called_with(self.home)

    def test_stale_prior_client_title_is_rejected(self):
        self._database({SESSION: LATEST_START_MS - 1})
        self.assertIsNone(self._proof())

    def test_creation_timestamp_never_selects_between_colliding_prefixes(self):
        self._database({SESSION: LATEST_START_MS + 100, COLLISION: LATEST_START_MS - 1})
        self.loaded.return_value = {SESSION, COLLISION}
        self.assertIsNone(self._proof())

    def test_new_prefix_collision_during_proof_is_rejected(self):
        self.loaded.side_effect = [{SESSION}, {SESSION, COLLISION}]
        self.assertIsNone(self._proof())

    def test_full_uuid_does_not_ambiguously_match_another_uuid_prefix(self):
        self.title = SESSION
        self.loaded.return_value = {SESSION, COLLISION}
        self.assertIsNotNone(self._proof())

    def test_new_title_selects_the_new_thread_while_both_threads_remain_loaded(self):
        self.loaded.return_value = {SESSION, OTHER}
        self.assertEqual(self._proof().session_id, SESSION)
        self.title = OTHER[:29] + "..."
        self.assertEqual(self._proof().session_id, OTHER)

    def test_malformed_and_non_native_prefixes_are_rejected(self):
        titles = (SESSION[:28] + "...", SESSION[:30] + "...", SESSION[:29] + "…",
                  SESSION[:29], '"' + SESSION + '"', SESSION + " ", "codex " + SESSION,
                  SESSION[:8] + "_" + SESSION[9:], "not-a-uuid", SESSION + "\n")
        for title in titles:
            with self.subTest(title=title):
                self.title = title
                self.assertIsNone(self._proof())
        self.loaded.assert_not_called()

    def test_title_requires_a_complete_uuid_in_the_loaded_catalog(self):
        for loaded in (None, set(), {OTHER}, {SESSION[:29] + "..."}):
            with self.subTest(loaded=loaded):
                self.loaded.return_value = loaded
                self.assertIsNone(self._proof())

    def test_catalog_removal_during_proof_is_rejected(self):
        self.loaded.side_effect = [{SESSION}, set()]
        self.assertIsNone(self._proof())

    def test_missing_or_nonexclusive_explicit_mode_is_rejected(self):
        configs = ('[tui]\n', '[tui]\nterminal_title = ["model"]\n',
                   '[tui]\nterminal_title = ["thread-id", "model"]\n',
                   '[tui]\nterminal_title = "thread-id"\n',
                   'profile = "work"\n[tui]\nterminal_title = ["thread-id"]\n',
                   '[tui]\nterminal_title = ["thread-id"]\n[profiles.work.tui]\nterminal_title = ["thread-id"]\n',
                   '[tui]\nterminal_title = [')
        for config in configs:
            with self.subTest(config=config):
                self._config(config)
                self.assertIsNone(self._proof())
        self.loaded.assert_not_called()

    def test_profile_and_config_argv_overrides_are_rejected(self):
        commands = (b"codex\0-p\0work\0", b"codex\0--profile=work\0",
                    b"codex\0-c\0tui.terminal_title=['thread-id']\0",
                    b"codex\0--config=tui.terminal_title=['thread-id']\0",
                    b"codex\0-C\0/other-workspace\0", b"codex\0--cd=/other-workspace\0")
        for command in commands:
            with self.subTest(command=command):
                self._process(456, command=command)
                self.assertIsNone(self._proof())
        self.loaded.assert_not_called()

    def test_project_or_managed_configuration_is_unsupported(self):
        project = self.cwd / ".codex"
        project.mkdir()
        for config in (project / "config.toml", self.home / "managed_config.toml", self.system_config):
            with self.subTest(config=config):
                config.write_text('[tui]\nterminal_title = ["thread-id"]\n')
                self.assertIsNone(self._proof())
                config.unlink()

    def test_unrelated_config_edit_after_launch_preserves_native_title_proof(self):
        self._config('model_reasoning_effort = "high"\n[tui]\nterminal_title = ["thread-id"]\n',
                     mtime=EARLIEST_START + 1)
        self.assertIsNotNone(self._proof())

    def test_unrelated_project_settings_do_not_override_native_titles(self):
        project = self.cwd / ".codex"
        project.mkdir()
        (project / "config.toml").write_text('model_reasoning_effort = "high"\n')
        self.assertIsNotNone(self._proof())

    def test_project_profile_and_malformed_configuration_refuse_proof(self):
        project = self.cwd / ".codex"
        project.mkdir()
        for text in ('profile = "work"\n', '[profiles.work.tui]\nterminal_title = ["thread-id"]\n',
                     '[tui]\nterminal_title = ['):
            with self.subTest(text=text):
                (project / "config.toml").write_text(text)
                self.assertIsNone(self._proof())

    def test_project_title_override_added_during_proof_is_rejected(self):
        project = self.cwd / ".codex"
        project.mkdir()
        def change(_root):
            (project / "config.toml").write_text('[tui]\nterminal_title = ["model"]\n')
            return {SESSION}
        self.loaded.side_effect = change
        self.assertIsNone(self._proof())

    def test_process_configuration_home_and_profile_must_be_unambiguous(self):
        for environment in (b"", b"CODEX_HOME=/other\0", b"CODEX_HOME=relative\0",
                            os.fsencode(f"CODEX_HOME={self.home}") + b"\0CODEX_PROFILE=work\0"):
            with self.subTest(environment=environment):
                (self.proc_root / "456" / "environ").write_bytes(environment)
                self.assertIsNone(self._proof())

    def test_default_home_configuration_location_can_be_proved(self):
        standard = self.home.with_name(".codex")
        self.home.rename(standard)
        self.home = standard
        (self.proc_root / "456" / "environ").write_bytes(os.fsencode(f"HOME={standard.parent}") + b"\0")
        self.assertIsNotNone(self._proof())

    def test_title_change_during_proof_is_rejected(self):
        def switch(_root):
            self.title = OTHER[:29] + "..."
            return {SESSION, OTHER}

        self.loaded.side_effect = switch
        self.assertIsNone(self._proof())

    def test_pane_identity_change_during_proof_is_rejected(self):
        def replace(_root):
            self.pane_pid = 999
            self._process(999, comm="zsh", command=b"zsh\0", group=123, ticks=50)
            self._process(456, parent=999)
            return {SESSION}

        self.loaded.side_effect = replace
        self.assertIsNone(self._proof())

    def test_pid_replacement_or_shell_takeover_during_proof_is_rejected(self):
        for changes in ({"ticks": 200}, {"comm": "zsh", "command": b"zsh\0"}, {"group": 888}):
            with self.subTest(changes=changes):
                self._process(456)

                def replace(_root, changes=changes):
                    self._process(456, **changes)
                    return {SESSION}

                self.loaded.side_effect = replace
                self.assertIsNone(self._proof())

    def test_unowned_background_and_noninteractive_processes_are_rejected(self):
        for changes in ({"parent": 999}, {"group": 888}, {"tty": "/dev/pts/8"},
                        {"comm": "zsh", "command": b"zsh\0"}, {"command": b"codex\0app-server\0"}):
            with self.subTest(changes=changes):
                self._process(456, **changes)
                self.assertIsNone(self._proof())
        self.loaded.assert_not_called()

    def test_loading_or_signin_does_not_supply_a_ready_composer(self):
        ready = self.screen
        for status in ("model:       loading", "Resuming session", "Sign in to Codex"):
            with self.subTest(status=status):
                self.row = 1
                self.screen = status + "\n" + ready
                self.assertIsNone(self._proof())
        self.loaded.assert_not_called()

    def test_loading_screen_after_the_catalog_probe_is_rejected(self):
        def loading(_root):
            self.row = 1
            self.screen = "model:       loading\n" + self.screen
            return {SESSION}

        self.loaded.side_effect = loading
        self.assertIsNone(self._proof())

    def test_missing_timestamp_row_schema_and_database_are_unsupported(self):
        for created in (None, "unknown", -1, LATEST_START_MS + 0.25):
            with self.subTest(created=created):
                self._database({SESSION: created})
                self.assertIsNone(self._proof())
        self._database({OTHER: LATEST_START_MS + 100})
        self.assertIsNone(self._proof())
        database = self.home / "state_5.sqlite"
        database.unlink()
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, created_at INTEGER)")
            connection.execute("INSERT INTO threads VALUES (?, ?)", (SESSION, LATEST_START_MS + 100))
        self.assertIsNone(self._proof())
        database.unlink()
        self.assertIsNone(self._proof())
        self.assertFalse(database.exists())

    def test_config_change_during_proof_is_rejected(self):
        def reconfigure(_root):
            self._config('[tui]\nterminal_title = ["model"]\n')
            return {SESSION}

        self.loaded.side_effect = reconfigure
        self.assertIsNone(self._proof())

    def test_creation_timestamp_change_during_proof_is_rejected(self):
        calls = 0

        def replace(_root):
            nonlocal calls
            calls += 1
            if calls == 2:
                self._database({SESSION: LATEST_START_MS - 1})
            return {SESSION}

        self.loaded.side_effect = replace
        self.assertIsNone(self._proof())

    def test_start_time_uses_boottime_and_the_upper_edge_of_the_kernel_tick(self):
        self.assertEqual(codex_title._start_bounds(100), (EARLIEST_START, LATEST_START_MS))
        self._database({SESSION: LATEST_START_MS})
        self.assertIsNotNone(self._proof())
        self.frequency.assert_called_with("SC_CLK_TCK")

    def test_unavailable_or_inconsistent_clock_evidence_is_rejected(self):
        self.frequency.side_effect = OSError("unavailable")
        self.assertIsNone(self._proof())
        self.frequency.side_effect = None
        self.clock.side_effect = [BOOTTIME, BOOTTIME - 1]
        self.assertIsNone(self._proof())
        self.clock.side_effect = [BOOTTIME, BOOTTIME + 50_000_001]
        self.assertIsNone(self._proof())

    def test_bad_target_and_tmux_failure_do_not_probe_daemon_or_database(self):
        self.assertIsNone(codex_title.native_title_proof(456, "%other", self.home))
        for result in (subprocess.CompletedProcess([], 1, "", ""),
                       subprocess.CompletedProcess([], 0, f"%other\t123\t{SESSION}\n", "")):
            with self.subTest(result=result):
                self.tmux.side_effect = None
                self.tmux.return_value = result
                self.assertIsNone(self._proof())
        self.tmux.side_effect = subprocess.TimeoutExpired("tmux", 1)
        self.assertIsNone(self._proof())
        self.loaded.assert_not_called()


if __name__ == "__main__":
    unittest.main()
