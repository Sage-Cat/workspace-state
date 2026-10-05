from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import json

from workspace_state import cli, operations, restore, terminal_restore_map


def session():
    return {'name':'work','windows':[{'index':1,'name':'one','panes':[{'index':1,'cwd':'/synthetic','codex':{'session_id':'thread-a'}}]}, {'index':2,'name':'two','panes':[{'index':1,'cwd':'/synthetic','codex':{'session_id':'thread-b'}}]}]}


def state():
    return {4:{'id':'@4','name':'user-one','panes':{1:{'id':'%4','pid':104,'cwd':'/synthetic','command':'codex'}}},7:{'id':'@7','name':'user-two','panes':{1:{'id':'%7','pid':107,'cwd':'/synthetic','command':'codex'}}}}


class ExistingConversationCollisionTests(unittest.TestCase):
    def test_shifted_exact_conversations_are_reused_without_layout_or_process_changes(self):
        with patch.object(restore,'_tmux_exists',return_value=True), patch.object(restore,'_tmux_state',return_value=state()), patch.object(restore,'_tagged_restore_session',return_value=None), patch.object(restore,'_restore_fingerprint',return_value=None), patch.object(restore,'_live_codex_ids',return_value={(4,1):'thread-a',(7,1):'thread-b'}), patch.object(restore,'_available_tmux_name',return_value='work-wsctl'), patch.object(restore,'run',side_effect=AssertionError('unexpected new session mutation')) as run, patch.object(restore,'_reconcile_tmux') as reconcile:
            name, actions=restore.recreate_tmux(session())
        self.assertEqual(name,'work')
        self.assertTrue(any('preserve its current layout' in a for a in actions))
        run.assert_not_called();reconcile.assert_not_called()

    def test_partial_or_ambiguous_live_conversations_do_not_launch_duplicates(self):
        for ids in ({(4,1):'thread-a'},{(4,1):'thread-a',(5,1):'thread-a',(7,1):'thread-b'}):
            with self.subTest(ids=ids), patch.object(restore,'_tmux_exists',return_value=True), patch.object(restore,'_tmux_state',return_value=state()), patch.object(restore,'_tagged_restore_session',return_value=None), patch.object(restore,'_restore_fingerprint',return_value=None), patch.object(restore,'_live_codex_ids',return_value=ids), patch.object(restore,'_available_tmux_name',return_value='work-wsctl'), patch.object(restore,'run',side_effect=AssertionError('unexpected new session mutation')) as run:
                with self.assertRaisesRegex(restore.CommandError,'already active'):
                    restore.recreate_tmux(session())
                run.assert_not_called()

    def test_exact_uuid_in_an_unready_pane_is_not_ready(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a'},
        ) as live:
            missing = restore.missing_codex_ids(session(), 'work-wsctl',
                                               {'thread-a': '%4', 'thread-b': '%7'})
        self.assertEqual(missing, {'thread-b'})
        live.assert_called_once_with(state(), require_ready=True)

    def test_saved_indices_do_not_override_exact_pane_bindings(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a', (7, 1): 'thread-b'},
        ):
            self.assertEqual(restore.missing_codex_ids(
                session(), 'work-wsctl', {'thread-a': '%4', 'thread-b': '%7'},
            ), set())

    def test_bound_uuid_cannot_follow_replacement_pane(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-b', (7, 1): 'thread-a'},
        ):
            self.assertEqual(restore.missing_codex_ids(
                session(), 'work-wsctl', {'thread-a': '%4', 'thread-b': '%7'},
            ), {'thread-a', 'thread-b'})

    def test_initially_unbound_directory_wrapper_can_become_ready(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a', (7, 1): 'thread-b'},
        ):
            self.assertEqual(restore.missing_codex_ids(session(), 'work-wsctl', {}), set())

    def test_ambiguous_unbound_uuid_remains_missing(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a', (7, 1): 'thread-a'},
        ):
            self.assertEqual(restore.missing_codex_ids(session(), 'work-wsctl', {}),
                             {'thread-a', 'thread-b'})


class ScopedTerminalMapTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / terminal_restore_map.FILE_NAME
        self.status = self.root / 'status.json'
        self.context = operations.OperationContext.create('synthetic-login', 'startup')
        self.status.write_text(json.dumps({
            'mode': 'startup', 'session_id': self.context.login_generation,
            'operation_id': self.context.operation_id,
            'operation_context': self.context.to_dict(), 'operation_state': 'running',
        }))
        self.runtime = {'server_pid': '100', 'server_start_tick': '500', 'session_id': '$4'}
        self.runtime_patch = patch.object(terminal_restore_map, 'tmux_runtime_identity',
                                         return_value=self.runtime)
        self.runtime_mock = self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        with patch.object(terminal_restore_map, 'codex_pane_bindings',
                          return_value={'thread-a': '%4', 'thread-b': '%7'}):
            terminal_restore_map.publish(self.path, [session()], {'work': 'work-wsctl'},
                                         self.context, self.status)

    def test_valid_map_binds_actual_session_and_immutable_panes(self):
        entry = terminal_restore_map.read(self.path, session(), self.context)
        self.assertEqual(entry['actual_name'], 'work-wsctl')
        self.assertEqual(entry['codex_panes'], {'thread-a': '%4', 'thread-b': '%7'})
        with patch.object(cli, '_startup_directory', return_value=self.root), patch.object(
            cli, 'missing_codex_ids', return_value=set(),
        ) as missing:
            self.assertEqual(cli._mapped_missing_codex_ids(session(), self.context, 'work'), set())
        missing.assert_called_once_with(session(), 'work-wsctl', entry['codex_panes'])

    def test_new_operation_recipe_server_or_session_invalidates_map(self):
        other = operations.OperationContext.create('synthetic-login', 'startup')
        self.assertIsNone(terminal_restore_map.read(self.path, session(), other))
        changed = deepcopy(session())
        changed['windows'][0]['panes'][0]['codex']['session_id'] = 'another-thread'
        self.assertIsNone(terminal_restore_map.read(self.path, changed, self.context))
        for field, replacement in (('server_pid', '101'), ('server_start_tick', '501'),
                                   ('session_id', '$5')):
            with self.subTest(field=field):
                self.runtime_mock.return_value = {**self.runtime, field: replacement}
                self.assertIsNone(terminal_restore_map.read(self.path, session(), self.context))
        self.runtime_mock.return_value = self.runtime

    def test_stale_map_never_falls_back_to_same_name_session(self):
        self.runtime_mock.return_value = None
        with patch.object(cli, '_startup_directory', return_value=self.root), patch.object(
            cli, 'missing_codex_ids', return_value=set(),
        ) as missing:
            self.assertEqual(cli._mapped_missing_codex_ids(session(), self.context, 'work'),
                             {'thread-a', 'thread-b'})
        missing.assert_not_called()

    def test_unknown_or_duplicate_pane_bindings_are_rejected(self):
        original = json.loads(self.path.read_text())
        for bindings in ({'unknown-thread': '%4'}, {'thread-a': '%4', 'thread-b': '%4'}):
            value = deepcopy(original)
            value['sessions']['work']['codex_panes'] = bindings
            self.path.write_text(json.dumps(value))
            self.assertIsNone(terminal_restore_map.read(self.path, session(), self.context))

    def test_cancelled_publisher_cannot_replace_receipt(self):
        before = self.path.read_bytes()
        status = json.loads(self.status.read_text())
        status['operation_state'] = 'cancelled'
        self.status.write_text(json.dumps(status))
        with self.assertRaisesRegex(RuntimeError, 'no longer owns'):
            terminal_restore_map.publish(self.path, [session()], {'work': 'work'},
                                         self.context, self.status)
        self.assertEqual(self.path.read_bytes(), before)
