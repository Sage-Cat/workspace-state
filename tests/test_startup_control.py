import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from workspace_state import operations, login_status, startup_control, browser, login_finalize
from workspace_state.util import atomic_json


class StartupControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.temp.name, 'XDG_STATE_HOME': self.temp.name + '/state'})
        env.start(); self.addCleanup(env.stop)
        operations.bind(None); self.addCleanup(operations.bind, None)
        login_status.initialize('a' * 16)
        self.context = operations.current()
        self.path = login_status.runtime_root() / 'startup-abort.json'

    def receipt(self, **changes):
        value = dict(schema_version=1, mode='startup', session_id=self.context.login_generation,
                     operation_id=self.context.operation_id, operation_context=self.context.to_dict(), requested_at='test')
        atomic_json(self.path, value | changes)

    def test_exact_context_is_required_and_shutdown_not_cancelled(self):
        for changed in ({'session_id':'older'}, {'operation_id':'older'}, {'mode':'shutdown'},
                        {'operation_context':self.context.to_dict() | {'attempt':2}}):
            self.receipt(**changed)
            self.context.check()
            self.assertFalse(startup_control.abort_requested(self.context))
        self.receipt()
        with self.assertRaises(operations.StartupAborted): self.context.check()
        shutdown = operations.OperationContext.create('a' * 16, 'shutdown')
        shutdown.check()

    def test_untrusted_symlink_permissions_and_oversized_receipts_ignored(self):
        self.receipt(); self.path.chmod(0o644)
        self.assertFalse(startup_control.abort_requested(self.context))
        self.path.chmod(0o600); target = self.path.with_name('target.json'); self.path.rename(target)
        self.path.symlink_to(target)
        self.assertFalse(startup_control.abort_requested(self.context))
        self.path.unlink(); self.path.write_text(' ' * 8193); self.path.chmod(0o600)
        self.assertFalse(startup_control.abort_requested(self.context))

    def test_cancel_fences_late_success_preserves_snapshot_and_completed_stage(self):
        login_status.update_stage('tmux', 'ready', 'Restored')
        from workspace_state.startup import startup_directory
        directory = startup_directory(login_status.runtime_root(), self.context.boot_id, self.context.login_generation)
        (directory/'autosave.ready').touch()
        snapshot = Path(self.temp.name)/'user-snapshot.json'; snapshot.write_text('do not replace')
        self.receipt()
        with patch.object(browser, '_host_paths', return_value=[]):
            self.assertTrue(startup_control.cancel(self.context))
            self.assertTrue(startup_control.cancel(self.context))
        document=json.loads(login_status.status_path().read_text())
        self.assertEqual(document['operation_state'], 'cancelled')
        self.assertEqual(document['overall_state'], 'degraded')
        self.assertEqual(next(s for s in document['stages'] if s['id']=='tmux')['state'], 'ready')
        before=login_status.status_path().read_bytes()
        self.assertFalse(login_status.update_stage('browsers', 'ready', 'Late success'))
        self.assertFalse(login_status.fail_active('Expected stop'))
        self.assertEqual(login_status.status_path().read_bytes(), before)
        self.assertFalse((directory/'autosave.ready').exists())
        self.assertEqual(snapshot.read_text(), 'do not replace')

    def test_unacknowledged_provider_remains_cancelling_then_can_settle(self):
        self.receipt()
        with patch.object(browser, '_host_paths', return_value=[Path('/fake')]), \
             patch.object(browser, '_request_path', side_effect=browser.BrowserUnavailable('disconnected')):
            self.assertFalse(startup_control.cancel(self.context))
        document=json.loads(login_status.status_path().read_text())
        self.assertEqual(document['operation_state'],'cancelling')
        self.assertNotEqual(document['overall_state'],'ready')
        response={'aborted':True,'operation_context':self.context.to_dict()}
        with patch.object(browser, '_host_paths', return_value=[Path('/fake')]), \
             patch.object(browser, '_request_path', return_value=response) as request:
            self.assertTrue(startup_control.cancel(self.context))
        self.assertFalse(request.call_args.kwargs['check_operation'])

    def test_slow_native_response_wait_is_interrupted_without_mutating_browser(self):
        path=Path(self.temp.name)/'native.sock'
        server=socket.socket(socket.AF_UNIX); server.bind(str(path)); server.listen(1)
        self.addCleanup(server.close)
        received=threading.Event(); finish=threading.Event()
        def host():
            connection,_=server.accept()
            with connection:
                connection.recv(65536); received.set(); finish.wait(2)
        thread=threading.Thread(target=host);thread.start()
        def abort():
            received.wait(1); self.receipt()
        requester=threading.Thread(target=abort);requester.start()
        began=time.monotonic()
        try:
            with self.assertRaises(operations.StartupAborted):
                browser._request_path(path,'restore_status',timeout=10)
            self.assertLess(time.monotonic()-began,1)
        finally:
            finish.set();thread.join(2);requester.join(2)

    def test_later_checkpoint_can_observe_after_abort_but_restore_is_fenced(self):
        from workspace_state import util
        self.receipt()
        with patch.object(util.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'state','')) as run:
            self.assertEqual(util.run(['gnome-winctl','state']), 'state')
            with self.assertRaises(operations.StartupAborted):
                util.run(['gnome-winctl','place','123'])
            self.assertEqual(run.call_count, 1)
        with patch.object(browser.socket, 'socket') as socket:
            socket.return_value.recv.return_value = b'{"ok":true,"result":{"active_mutations":0}}\n'
            self.assertEqual(browser._request_path(Path('/fixture'),'native_mutation_status'), {'active_mutations':0})
        with patch.object(browser.socket, 'socket') as socket:
            with self.assertRaises(operations.StartupAborted):
                browser._request_path(Path('/fixture'),'restore_window')
            socket.assert_not_called()

    def test_later_checkpoint_observation_survives_expired_startup_deadline(self):
        from dataclasses import replace
        from workspace_state import util
        expired = replace(self.context, deadline=1)
        with operations.publisher(expired), patch.object(util.subprocess, 'run', return_value=subprocess.CompletedProcess([],0,'state','')):
            self.assertEqual(util.run(['gnome-winctl','state']), 'state')
        with operations.publisher(expired), patch.object(browser.socket, 'socket') as socket:
            socket.return_value.recv.return_value = b'{"ok":true,"result":{"active_mutations":0}}\n'
            self.assertEqual(browser._request_path(Path('/fixture'),'native_mutation_status'), {'active_mutations':0})

    def test_queued_codex_aborts_without_launching_client(self):
        from workspace_state import codex_resume
        self.receipt()
        with patch.object(codex_resume, '_record'), patch.object(codex_resume.subprocess, 'Popen') as launch:
            self.assertEqual(codex_resume.resume('00000000-0000-0000-0000-000000000013'), 0)
            launch.assert_not_called()

    def test_abort_preserves_already_launched_codex_client(self):
        from workspace_state import codex_resume
        with patch.object(codex_resume, '_record'), patch.object(codex_resume, 'saved_cwd', return_value=None), \
             patch.object(codex_resume, 'resumed_session', return_value=True), \
             patch.object(codex_resume.subprocess, 'Popen') as launch:
            child = launch.return_value
            child.poll.return_value = None
            child.wait.side_effect = lambda: (self.receipt(), 0)[1]
            self.assertEqual(codex_resume.resume('00000000-0000-0000-0000-000000000013'), 0)
            child.terminate.assert_not_called()
            child.kill.assert_not_called()

    def test_finalizer_stops_finite_wait_helper(self):
        def delayed_abort():
            time.sleep(.1);self.receipt()
        thread=threading.Thread(target=delayed_abort);thread.start()
        began=time.monotonic()
        try:
            with self.assertRaises(operations.StartupAborted):
                login_finalize._run('/usr/bin/python3','-c','import time; time.sleep(10)',timeout=20)
            self.assertLess(time.monotonic()-began,1.5)
        finally:thread.join(2)


if __name__=='__main__':unittest.main()
