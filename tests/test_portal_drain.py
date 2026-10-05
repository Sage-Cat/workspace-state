from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import graphical_drain, operations, portal_drain as portal
from workspace_state.util import atomic_json

BOOT = '01234567-0123-0123-0123-0123456789ab'
INVOCATION = 'a' * 32


def proof():
    return {'unit': portal.UNIT, 'invocation_id': INVOCATION, 'control_group': portal.group(),
            'bus_name': portal.BUS, 'bus_owner': ':1.22',
            'process': {'pid': 123, 'start_ticks': '77', 'executable': str(portal.EXECUTABLE),
                        'executable_dev': 1, 'executable_ino': 2, 'uid': os.getuid()},
            'mount': {'mount_id': 55, 'device': '0:66', 'path': f'/run/user/{os.getuid()}/doc',
                      'filesystem': 'fuse.portal', 'source': 'portal', 'uid': os.getuid()}}


class FakeManager:
    def __init__(self):
        self.boot = BOOT
        self.stops = []
        self.running = True
        self.pending = False
        self.alive = False
        self.owner = proof()
        self.records = None
        self.snapshot_error = None
        self.stop_error = None
        self.stop_callback = None
        self.inspect_override = None

    def snapshot(self):
        if self.snapshot_error:
            raise self.snapshot_error
        return deepcopy(self.owner) if self.running else None

    def stop(self, owner):
        self.stops.append(owner)
        if self.stop_callback:
            self.stop_callback()
        if self.stop_error:
            raise self.stop_error
        self.running = False

    def inspect(self, _unit):
        if self.inspect_override:
            return self.inspect_override
        return {'LoadState': 'loaded', 'InvocationID': INVOCATION, 'Result': 'success',
                'ActiveState': 'deactivating' if self.pending else 'active' if self.running else 'inactive',
                'Job': '55' if self.pending else ''}

    def empty(self, _owner):
        return not self.alive and not self.running

    def bus_owner(self):
        return (':1.22', 123) if self.running else None

    def journal(self, _owner):
        return self.records if self.records is not None else [{
            '_BOOT_ID': BOOT.replace('-', ''), '_UID': str(os.getuid()), '_COMM': 'systemd',
            '_SYSTEMD_USER_UNIT': 'init.scope', 'USER_UNIT': portal.UNIT,
            'USER_INVOCATION_ID': INVOCATION, 'JOB_TYPE': 'stop', 'JOB_RESULT': 'done'}]


class PortalDrainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / 'portal.json'
        self.context = operations.OperationContext(BOOT, 'login', 'operation', 'shutdown', 1, time.monotonic() + 60)
        self.manager = FakeManager()
        self.withdrawn = threading.Event()
        self.auth = patch.object(portal, 'authorized').start()
        self.addCleanup(patch.stopall)
        self.mount = patch.object(portal, 'mount_identity', return_value=None).start()

    def run_drain(self, **kwargs):
        return portal.drain(self.context, self.receipt, deadline=self.context.deadline,
                            timeout=.025, withdrawn=self.withdrawn,
                            manager_factory=lambda *_: self.manager, **kwargs)

    def existing(self, request='issuing', **extra):
        value = {'schema_version': 1, 'operation_context': self.context.to_dict(),
                 'status': 'running', 'settled': False, 'units': [proof()],
                 'requests': {portal.UNIT: request}, 'errors': [],
                 'not_running': False, 'settlement_only': False, 'deadline': time.monotonic() - 1}
        value.update(extra)
        atomic_json(self.receipt, value)

    def test_native_stop_has_durable_exact_intent_and_success_evidence(self):
        def check():
            value = json.loads(self.receipt.read_text())
            self.assertEqual(value['requests'], {portal.UNIT: 'issuing'})
            self.assertEqual(value['units'], [proof()])
        self.manager.stop_callback = check
        result = self.run_drain()
        self.assertEqual(graphical_drain.exit_status(result), 0)
        self.assertEqual(result['requests'], {portal.UNIT: 'done'})
        self.assertEqual(self.manager.stops, [proof()])
        portal.validate_receipt(result, self.context)

    def test_foreign_native_identity_refuses_before_stop(self):
        self.manager.snapshot_error = ValueError('foreign mount or executable')
        result = self.run_drain()
        self.assertEqual(graphical_drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])

    def test_cancellation_before_issuance_preserves_native_service(self):
        self.auth.side_effect = ValueError('authorization withdrawn')
        result = self.run_drain()
        self.assertTrue(result['settled'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.manager.stops, [])

    def test_expired_deadline_cannot_issue_stop(self):
        with patch.object(portal.time, 'monotonic', return_value=self.context.deadline + 1):
            result = self.run_drain()
        self.assertEqual(self.manager.stops, [])
        self.assertEqual(result['status'], 'failed')

    def test_cancellation_after_stop_issuance_retains_pending_job_ownership(self):
        self.manager.pending = True
        self.manager.stop_callback = self.withdrawn.set
        result = self.run_drain()
        self.assertFalse(result['settled'])
        self.assertEqual(graphical_drain.exit_status(result), 75)
        self.assertEqual(len(self.manager.stops), 1)
        self.manager.pending = False
        result = self.run_drain(settle_only=True)
        self.assertTrue(result['settled'])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(len(self.manager.stops), 1)

    def test_reentry_never_reissues_an_interrupted_stop(self):
        self.existing()
        self.manager.pending = True
        result = self.run_drain(settle_only=True)
        self.assertFalse(result['settled'])
        self.assertEqual(self.manager.stops, [])
        self.manager.pending = False
        self.manager.running = False
        result = self.run_drain(settle_only=True)
        self.assertTrue(result['settled'])
        self.assertEqual(self.manager.stops, [])

    def test_issuing_marker_without_terminal_stop_job_cannot_release_owner(self):
        self.existing()
        self.manager.running = False
        self.manager.records = []
        result = self.run_drain(settle_only=True)
        self.assertFalse(result['settled'])
        self.assertIn('issuance is unresolved', ' '.join(result['errors']))
        self.assertEqual(self.manager.stops, [])

    def test_timeout_client_does_not_cancel_native_stop_job(self):
        self.manager.pending = True
        self.manager.stop_error = subprocess.TimeoutExpired('systemctl', .01)
        result = self.run_drain()
        self.assertFalse(result['settled'])
        self.assertEqual(graphical_drain.exit_status(result), 75)
        self.assertEqual(result['requests'][portal.UNIT], 'failed')

    def test_remaining_mount_or_children_prevent_success(self):
        for variant in ('mount', 'children'):
            with self.subTest(variant=variant):
                self.receipt.unlink(missing_ok=True)
                self.manager = FakeManager()
                self.manager.alive = variant == 'children'
                self.mount.return_value = proof()['mount'] if variant == 'mount' else None
                result = self.run_drain()
                self.assertFalse(result['settled'])
                self.assertNotEqual(result['status'], 'succeeded')

    def test_native_failure_is_not_hidden_by_later_stop_done(self):
        records = self.manager.journal(proof())
        self.manager.records = [{**records[0], 'UNIT_RESULT': 'timeout'}, records[0]]
        result = self.run_drain()
        self.assertTrue(result['settled'])
        self.assertEqual(result['status'], 'failed')

    def test_recovery_before_any_issuance_does_not_probe_or_stop(self):
        self.manager.snapshot = Mock(side_effect=AssertionError('must not probe'))
        result = self.run_drain(settle_only=True)
        self.assertTrue(result['settled'])
        self.assertTrue(result['settlement_only'])
        self.assertEqual(self.manager.stops, [])
        with self.assertRaises(ValueError):
            portal.verify_stopped(result, self.context, manager_factory=lambda *_: self.manager)

    def test_inactive_absent_native_service_has_explicit_no_stop_receipt(self):
        self.manager.running = False
        result = self.run_drain()
        self.assertTrue(result['not_running'])
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.manager.stops, [])
        portal.verify_stopped(result, self.context, manager_factory=lambda *_: self.manager)

    def test_historical_success_cannot_hide_reactivated_replaced_or_busy_portal(self):
        result = self.run_drain()
        portal.verify_stopped(result, self.context, manager_factory=lambda *_: self.manager)
        for variant in ('reactivated', 'replaced', 'mount', 'job', 'bus'):
            with self.subTest(variant=variant):
                self.manager.inspect_override = None
                self.manager.running = False
                self.mount.return_value = None
                self.manager.bus_owner = lambda: None
                if variant == 'reactivated':
                    self.manager.running = True
                elif variant == 'replaced':
                    self.manager.inspect_override = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                                                     'InvocationID': 'b' * 32, 'Result': 'success', 'Job': ''}
                elif variant == 'mount':
                    self.mount.return_value = proof()['mount']
                elif variant == 'job':
                    self.manager.inspect_override = {'LoadState': 'loaded', 'ActiveState': 'inactive',
                                                     'InvocationID': INVOCATION, 'Result': 'success', 'Job': '55'}
                else:
                    self.manager.bus_owner = lambda: (':1.999', 999)
                with self.assertRaises(ValueError):
                    portal.verify_stopped(result, self.context, manager_factory=lambda *_: self.manager)

    def test_wrong_boot_operation_or_native_identity_receipt_refuses(self):
        result = self.run_drain()
        for change in ('operation', 'invocation', 'pid', 'mount', 'unit', 'no_issuance'):
            candidate = deepcopy(result)
            if change == 'operation': candidate['operation_context']['operation_id'] = 'another'
            elif change == 'invocation': candidate['units'][0]['invocation_id'] = ''
            elif change == 'pid': candidate['units'][0]['process']['pid'] = 1
            elif change == 'mount': candidate['units'][0]['mount']['path'] = '/foreign'
            elif change == 'unit': candidate['units'][0]['unit'] = 'other.service'
            else:
                candidate['units'] = []; candidate['requests'] = {}; candidate['not_running'] = False
            with self.subTest(change=change), self.assertRaises(ValueError):
                portal.validate_receipt(candidate, self.context)

    def test_corrupted_nested_receipt_refuses_with_validation_error(self):
        result = self.run_drain()
        changes = [("proof", None), ("process", None), ("mount", None),
                   ("invocation_id", 42), ("bus_owner", []), ("start_ticks", 77),
                   ("status", []), ("request", {})]
        for field, value in changes:
            candidate = deepcopy(result)
            if field == "proof": candidate["units"][0] = value
            elif field == "status": candidate[field] = value
            elif field == "request": candidate["requests"][portal.UNIT] = value
            elif field == "start_ticks": candidate["units"][0]["process"][field] = value
            else: candidate["units"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                portal.validate_receipt(candidate, self.context)


class NativeOwnershipTests(unittest.TestCase):
    def test_process_identity_requires_exact_native_uid_executable_start_and_cgroup(self):
        uid = os.getuid()
        root = Path('/proc/123')
        fields = ['S'] + ['0'] * 18 + ['77']
        texts = {root / 'status': f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n',
                 root / 'stat': '123 (xdg-document-po) ' + ' '.join(fields),
                 root / 'cgroup': '0::' + portal.group() + '\n'}
        expected = Mock(st_uid=0, st_mode=0o100755, st_dev=1, st_ino=2)
        actual = Mock(st_dev=1, st_ino=2)
        with patch.object(Path, 'stat', autospec=True,
                          side_effect=lambda path: expected if path == portal.EXECUTABLE else actual), \
             patch.object(Path, 'resolve', autospec=True, side_effect=lambda path: path), \
             patch.object(Path, 'read_text', autospec=True, side_effect=lambda path: texts[path]), \
             patch.object(Path, 'read_bytes', return_value=os.fsencode(portal.EXECUTABLE) + b'\0') as argv, \
             patch.object(Path, 'readlink', return_value=portal.EXECUTABLE) as executable:
            self.assertEqual(portal.process_identity(123), proof()['process'])
            for variant in ('inode', 'uid', 'group', 'argv', 'zombie', 'path', 'writable', 'owner', 'start'):
                actual.st_ino = 2; expected.st_uid = 0; expected.st_mode = 0o100755
                texts[root / 'status'] = f'Uid:\t{uid}\t{uid}\t{uid}\t{uid}\n'
                texts[root / 'cgroup'] = '0::' + portal.group() + '\n'
                texts[root / 'stat'] = '123 (xdg-document-po) ' + ' '.join(fields)
                argv.return_value = os.fsencode(portal.EXECUTABLE) + b'\0'
                executable.return_value = portal.EXECUTABLE
                if variant == 'inode': actual.st_ino = 99
                elif variant == 'uid': texts[root / 'status'] = 'Uid:\t99999\t99999\t99999\t99999\n'
                elif variant == 'group': texts[root / 'cgroup'] = '0::/foreign\n'
                elif variant == 'argv': argv.return_value += b'--other\0'
                elif variant == 'zombie': texts[root / 'stat'] = '123 (portal) Z ' + ' '.join(fields[1:])
                elif variant == 'path': executable.return_value = Path('/foreign/portal')
                elif variant == 'writable': expected.st_mode = 0o100777
                elif variant == 'owner': expected.st_uid = uid or 99999
                else: texts[root / 'stat'] = 'malformed'
                with self.subTest(variant=variant), self.assertRaises(ValueError):
                    portal.process_identity(123)

    def test_mount_identity_rejects_foreign_or_stacked_native_path(self):
        uid = os.getuid()
        valid = f'55 44 0:66 / /run/user/{uid}/doc rw - fuse.portal portal rw,user_id={uid}\n'
        with patch.object(Path, 'read_text', return_value=valid):
            self.assertEqual(portal.mount_identity()['mount_id'], 55)
        for text in (valid * 2, valid.replace('fuse.portal', 'fuse.sshfs'),
                     valid.replace(f'user_id={uid}', 'user_id=99999')):
            with patch.object(Path, 'read_text', return_value=text), self.assertRaises(ValueError):
                portal.mount_identity()

    def test_native_unit_snapshot_checks_bus_pid_invocation_and_vendor_fragment(self):
        manager = portal.Manager(time.monotonic() + 5, BOOT)
        properties = {'Id': portal.UNIT, 'LoadState': 'loaded', 'Transient': 'no',
                      'ControlGroup': portal.group(), 'FragmentPath': str(portal.FRAGMENT),
                      'BusName': portal.BUS, 'PartOf': 'graphical-session.target',
                      'InvocationID': INVOCATION, 'ActiveState': 'active', 'Job': '',
                      'Result': 'success', 'MainPID': '123'}
        manager.inspect = Mock(return_value=properties)
        manager.bus_owner = Mock(return_value=(':1.22', 123))
        with patch.object(portal, 'mount_identity', return_value=proof()['mount']), \
             patch.object(portal, 'process_identity', return_value=proof()['process']), \
             patch.object(Path, 'stat', return_value=Mock(st_uid=0, st_mode=0o100644)), \
             patch.object(Path, 'resolve', side_effect=lambda: portal.FRAGMENT):
            self.assertEqual(manager.snapshot(), proof())
            for key, bad in [('MainPID', '999'), ('InvocationID', ''), ('FragmentPath', '/foreign'),
                             ('ControlGroup', '/system.slice/other'), ('BusName', 'other'),
                             ('Transient', 'yes'), ('Job', '55'), ('Result', 'timeout')]:
                manager.inspect.return_value = {**properties, key: bad}
                with self.subTest(key=key), self.assertRaises(ValueError):
                    manager.snapshot()

    def test_bus_structured_types_and_unique_pid_are_required(self):
        manager = portal.Manager(time.monotonic() + 5, BOOT)
        manager.bus_call = Mock(side_effect=[True, ':1.22', 123])
        self.assertEqual(manager.bus_owner(), (':1.22', 123))
        for replies in ([1], [True, 'well-known'], [True, ':1.22', True], [True, ':1.22', 1]):
            manager.bus_call = Mock(side_effect=replies)
            with self.subTest(replies=replies), self.assertRaises(ValueError):
                manager.bus_owner()


if __name__ == '__main__':
    unittest.main()
