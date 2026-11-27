import unittest
from unittest.mock import patch

from workspace_state import cli, restore


def session():
    return {'name':'work','windows':[{'index':1,'name':'one','panes':[{'index':1,'cwd':'/synthetic','codex':{'session_id':'thread-a'}}]}, {'index':2,'name':'two','panes':[{'index':1,'cwd':'/synthetic','codex':{'session_id':'thread-b'}}]}]}


def state():
    return {4:{'id':'@4','name':'user-one','panes':{1:{'id':'%4','pid':104,'cwd':'/synthetic','command':'codex'}}},7:{'id':'@7','name':'user-two','panes':{1:{'id':'%7','pid':107,'cwd':'/synthetic','command':'codex'}}}}


class ExistingConversationCollisionTests(unittest.TestCase):
    def test_shifted_exact_conversations_are_reused_without_layout_or_process_changes(self):
        with patch.object(restore,'_tmux_exists',return_value=True), patch.object(restore,'_tmux_state',return_value=state()), patch.object(restore,'_tagged_restore_session',return_value=None), patch.object(restore,'_restore_fingerprint',return_value=None), patch.object(restore,'_live_codex_ids',return_value={(4,1):'thread-a',(7,1):'thread-b'}), patch.object(restore.conversation_guard,'live_locations',return_value={}), patch.object(restore,'run',side_effect=AssertionError('unexpected new session mutation')) as run, patch.object(restore,'_reconcile_tmux') as reconcile:
            name, actions=restore.recreate_tmux(session())
        self.assertEqual(name,'work')
        self.assertTrue(any('preserve its current layout' in a for a in actions))
        run.assert_not_called();reconcile.assert_not_called()

    def test_ambiguous_live_conversations_do_not_launch_duplicates(self):
        for ids in ({(4,1):'thread-a',(5,1):'thread-a',(7,1):'thread-b'},):
            with self.subTest(ids=ids), patch.object(restore,'_tmux_exists',return_value=True), patch.object(restore,'_tmux_state',return_value=state()), patch.object(restore,'_tagged_restore_session',return_value=None), patch.object(restore,'_restore_fingerprint',return_value=None), patch.object(restore,'_live_codex_ids',return_value=ids), patch.object(restore.conversation_guard,'live_locations',return_value={}), patch.object(restore,'run',side_effect=AssertionError('unexpected new session mutation')) as run:
                with self.assertRaisesRegex(restore.CommandError,'already active'):
                    restore.recreate_tmux(session())
                run.assert_not_called()

    def test_exact_uuid_in_an_unready_pane_is_not_ready(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a'},
        ) as live:
            missing = restore.missing_codex_ids(session(), 'work-wsctl')
        self.assertEqual(missing, {'thread-b'})
        live.assert_called_once_with(state(), require_ready=True)

    def test_identity_decides_wherever_the_pane_is(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-b', (7, 1): 'thread-a'},
        ):
            self.assertEqual(restore.missing_codex_ids(session(), 'work-wsctl'), set())

    def test_a_conversation_that_is_not_live_is_missing(self):
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(7, 1): 'thread-b'},
        ):
            self.assertEqual(restore.missing_codex_ids(session(), 'work-wsctl'), {'thread-a'})


class RepeatedStartupVerificationTests(unittest.TestCase):
    """A rerun in one login finds its live conversations without any operation record."""

    KEY = 'codex:thread-a'

    def verify(self, *, ready, live=None, seen=(), sessions=None):
        sessions = sessions or [(session(), 'work')]
        live = {} if live is None else live
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value=ready,
        ), patch.object(restore, '_tagged_restore_session', return_value=None), patch.object(
            restore.conversation_guard, 'live_sessions', return_value=live,
        ), patch.object(cli, '_seen_directory', return_value=None), patch(
            'workspace_state.pending.seen_keys', return_value=set(seen),
        ):
            return cli._missing_conversations(sessions)

    def test_rearranged_panes_still_count_as_live(self):
        # The saved panes are 1.1 and 2.1; the live ones are 4.1 and 7.1.
        self.assertEqual(self.verify(ready={(4, 1): 'thread-a', (7, 1): 'thread-b'}), set())

    def test_no_operation_record_is_read(self):
        # An expired operation or conversation deadline leaves nothing to compare:
        # the answer comes from the live tmux state alone.
        with patch.object(cli, '_marker_context', side_effect=AssertionError('operation record read')):
            self.assertEqual(self.verify(ready={(4, 1): 'thread-a', (7, 1): 'thread-b'}), set())

    def test_a_busy_client_that_was_seen_live_is_not_missing(self):
        # `require_ready` fails mid-answer, but the live index and this login's record agree.
        self.assertEqual(self.verify(ready={(7, 1): 'thread-b'},
                                     live={self.KEY: {'work'}}, seen={self.KEY}), set())

    def test_a_busy_client_never_seen_before_stays_missing(self):
        self.assertEqual(self.verify(ready={(7, 1): 'thread-b'}, live={self.KEY: {'work'}}),
                         {self.KEY})

    def test_a_conversation_running_in_another_session_is_not_missing(self):
        self.assertEqual(self.verify(ready={(7, 1): 'thread-b'}, live={self.KEY: {'other'}}), set())

    def test_a_conversation_live_nowhere_is_missing(self):
        self.assertEqual(self.verify(ready={(7, 1): 'thread-b'}), {self.KEY})

    def test_a_satisfied_session_costs_no_copy_lookup_or_record_read(self):
        # The verification loop polls every half second.
        with patch.object(restore, '_tmux_state', return_value=state()), patch.object(
            restore, '_live_codex_ids', return_value={(4, 1): 'thread-a', (7, 1): 'thread-b'},
        ), patch.object(restore, '_tagged_restore_session', side_effect=AssertionError('tmux asked')), \
                patch.object(cli, '_seen_directory', side_effect=AssertionError('record read')):
            self.assertEqual(cli._missing_conversations([(session(), 'work')]), set())

    def test_a_copy_left_by_an_older_build_counts(self):
        with patch.object(restore, '_tmux_state', side_effect=lambda name: state() if name == 'work-wsctl' else {}), \
                patch.object(restore, '_live_codex_ids', side_effect=lambda st, **_: (
                    {(4, 1): 'thread-a', (7, 1): 'thread-b'} if st else {})), \
                patch.object(restore, '_tagged_restore_session', return_value='work-wsctl'), \
                patch.object(restore.conversation_guard, 'live_sessions', return_value={}), \
                patch.object(cli, '_seen_directory', return_value=None):
            self.assertEqual(cli._missing_conversations([(session(), 'work')]), set())
