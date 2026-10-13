import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from argparse import Namespace

from workspace_state import cli, codex_launch_scope as scope, codex_resume, login_status, operations
from workspace_state.util import atomic_json


class LegacyCodexScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.temp.name})
        env.start(); self.addCleanup(env.stop)
        operations.bind(None); self.addCleanup(operations.bind, None)
        login_status.initialize('ab' * 8)
        self.context = operations.current()
        (login_status.runtime_root() / 'login-generation').write_text(self.context.login_generation)
        self.option = scope.BASE_MAPPING; self.calls = []
        mocked = patch.object(scope, '_tmux', side_effect=self.tmux)
        mocked.start(); self.addCleanup(mocked.stop)
        self.pid = os.getpid(); self.stamp = codex_resume._process_stamp(self.pid)

    def tmux(self, *args):
        self.calls.append(args)
        if args[0] == 'show-option': return self.option
        self.assertEqual(args[:3], ('set-option', '-g', '@resurrect-processes'))
        self.option = args[3]; return ''

    def begin(self):
        scope.begin(self.context, self.pid, self.stamp)

    def child(self):
        child = subprocess.Popen(['/usr/bin/python3', '-c', 'import time;time.sleep(10)'])
        def stop():
            if child.poll() is None: child.terminate()
            child.wait(timeout=2)
        self.addCleanup(stop); return child

    def test_legacy_command_keeps_immutable_context_without_exporting_shell_environment(self):
        self.begin()
        arg = self.option.split(' ', 1)[1].removesuffix(' *"')
        self.assertEqual(codex_resume.decode_startup_context(arg), self.context)
        self.assertNotIn(operations.CONTEXT_ENV, os.environ)
        self.assertEqual(scope._read_scope()['owner_pid'], self.pid)

    def test_nested_restore_hook_reuses_root_mapping_owner(self):
        self.begin(); child = self.child()
        scope.begin(self.context, child.pid, codex_resume._process_stamp(child.pid))
        self.assertEqual(scope._read_scope()['owner_pid'], self.pid)
        self.assertEqual(sum(c[0] == 'set-option' for c in self.calls), 1)

    def test_concurrent_restore_cannot_replace_live_scope(self):
        child = self.child()
        scope.begin(self.context, child.pid, codex_resume._process_stamp(child.pid))
        old = self.option
        with self.assertRaisesRegex(RuntimeError, 'Another tmux restore'):
            self.begin()
        self.assertEqual(self.option, old)
        self.assertEqual(scope._read_scope()['owner_pid'], child.pid)

    def test_owned_cleanup_restores_mapping_after_abort_without_publishing_success(self):
        self.begin()
        atomic_json(login_status.runtime_root() / 'startup-abort.json', dict(schema_version=1,
            mode='startup',session_id=self.context.login_generation,operation_id=self.context.operation_id,
            operation_context=self.context.to_dict()))
        before = login_status.status_path().read_bytes()
        with self.assertRaises(operations.StartupAborted): self.context.check()
        self.assertTrue(scope.end(self.pid))
        self.assertEqual(self.option, scope.BASE_MAPPING)
        self.assertFalse(scope.scope_path().exists())
        self.assertEqual(login_status.status_path().read_bytes(), before)

    def test_cleanup_preserves_user_option_change(self):
        self.begin(); changed = r'\"vim->vim *\"'; self.option = changed
        self.assertTrue(scope.end(self.pid)); self.assertEqual(self.option, changed)

    def test_stale_cleanup_cannot_change_newer_owner(self):
        child = self.child()
        scope.begin(self.context, child.pid, codex_resume._process_stamp(child.pid))
        before = scope.scope_path().read_bytes(); mapping = self.option
        self.assertFalse(scope.end(self.pid))
        self.assertEqual(scope.scope_path().read_bytes(), before)
        self.assertEqual(self.option, mapping)

    def test_dead_owner_releases_only_its_mapping_before_manual_restore(self):
        self.begin(); record = scope._read_scope(); record['owner_pid'] = 99999999
        atomic_json(scope.scope_path(), record)
        scope.begin(None, self.pid, self.stamp)
        self.assertEqual(self.option, scope.BASE_MAPPING); self.assertFalse(scope.scope_path().exists())

    def test_changed_nonmanaged_mapping_is_not_replaced(self):
        changed = r'\"vim->vim *\"'; self.option = changed
        with self.assertRaisesRegex(RuntimeError, 'current option preserved'): self.begin()
        self.assertEqual(self.option, changed); self.assertFalse(scope.scope_path().exists())

    def test_pre_restore_hook_binds_only_current_running_login(self):
        operations.bind(None)
        with patch.object(scope, 'begin') as begin:
            cli.cmd_tmux_begin(Namespace(owner_pid=self.pid))
        self.assertEqual(begin.call_args.args, (self.context, self.pid, self.stamp))
        document = json.loads(login_status.status_path().read_text()); document['operation_state'] = 'cancelled'
        atomic_json(login_status.status_path(), document)
        with patch.object(scope, 'begin') as begin:
            cli.cmd_tmux_begin(Namespace(owner_pid=self.pid))
        self.assertIsNone(begin.call_args.args[0])

    def test_cli_owned_cleanup_does_not_adopt_later_status_authority(self):
        with patch.object(scope, 'end', return_value=False) as end, \
             patch.object(operations, 'context_from_status') as adopt:
            self.assertEqual(cli.main(['tmux','end']), 0)
        adopt.assert_not_called(); end.assert_called_once_with()

    def test_legacy_unquoted_restore_list_does_not_expand_star_into_files(self):
        self.begin()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "unwanted-file").touch()
            script = 'restore_list() { printf "%s\\n" "$1"; }; eval set $(restore_list "$1"); printf "%s\\n" "$@"'
            result = subprocess.check_output(["bash", "-c", script, "legacy", self.option], cwd=directory, text=True)
        self.assertEqual(result.strip(), self.option.strip('"'))
        self.assertNotIn("unwanted-file", result)
