from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from workspace_state import shutdown_profiles as profiles


def profile(name, *, parallel=True, **extra):
    return profiles._profile_from_mapping({
        "schema_version": 1, "id": name, "label": name, "adapter": "command",
        "parallel": parallel,
        **{phase: ["/usr/bin/true"] for phase in ("probe", "prepare", "verify", "rollback")},
        **extra,
    })


class Cancel:
    def __init__(self):
        self.event = threading.Event()

    def requested(self):
        return self.event.is_set()


class Adapter:
    def probe(self, runtime):
        return True, "active"

    def prepare(self, runtime, cancel):
        return "prepared"

    def verify(self, runtime, cancel):
        return "verified"

    def rollback(self, runtime):
        return "restored"


class ParallelProfileSafetyTests(unittest.TestCase):
    def setUp(self):
        self.cancel = Cancel()
        writer = patch.object(profiles, "_write_transaction")
        self.writer = writer.start()
        self.addCleanup(writer.stop)
        for name in ("append_diagnostic", "_discard_startup_restore"):
            handle = patch.object(profiles, name)
            handle.start()
            self.addCleanup(handle.stop)

    def session(self, entries):
        return profiles.ShutdownProfileSession(entries, operation_id="a" * 32,
            session_id="b" * 16, action="poweroff", cancel=self.cancel,
            reporter=lambda *_: None)

    def test_pre_cancelled_never_probes_prepares_or_writes_journal(self):
        self.cancel.event.set()
        session = self.session([profile("one"), profile("two")])
        with patch.object(profiles, "_adapter_for") as adapter:
            with self.assertRaises(profiles.ShutdownProfilesCancelled):
                session.run()
        adapter.assert_not_called()
        self.writer.assert_not_called()
        self.assertFalse(session.runtimes)

    def test_cancel_during_probe_does_not_flush_pending_mutations(self):
        session = self.session([profile("one"), profile("two")])
        cancel = self.cancel

        class ProbeCancels(Adapter):
            def probe(self, runtime):
                if runtime.profile.identifier == "two":
                    cancel.event.set()
                return True, "active"

            def prepare(self, runtime, cancel):
                raise AssertionError("No mutation may start after cancellation")

        with patch.object(profiles, "_adapter_for", return_value=ProbeCancels()):
            with self.assertRaises(profiles.ShutdownProfilesCancelled):
                session.run()
        self.writer.assert_not_called()
        self.assertFalse(session.runtimes)

    def test_serial_profile_is_a_probe_and_mutation_barrier(self):
        order = []

        class Ordered(Adapter):
            def probe(self, runtime):
                order.append((runtime.profile.identifier, "probe"))
                return True, "active"

            def prepare(self, runtime, cancel):
                order.append((runtime.profile.identifier, "prepare"))
                return "prepared"

        session = self.session([profile("one"), profile("serial", parallel=False), profile("last")])
        with patch.object(profiles, "_adapter_for", return_value=Ordered()):
            session.run()
        self.assertEqual(order, [(name, phase) for name in ("one", "serial", "last") for phase in ("probe", "prepare")])

    def test_complete_writeahead_batch_exists_inside_every_prepare(self):
        barrier = threading.Barrier(4)
        recorded = set()
        self.writer.side_effect = lambda _op, _session, _action, runtimes: recorded.update(r.profile.identifier for r in runtimes)
        test = self

        class CheckJournal(Adapter):
            def prepare(self, runtime, cancel):
                test.assertEqual(recorded, {"a", "b", "c", "d"})
                barrier.wait(timeout=3)
                return "prepared"

        with patch.object(profiles, "_adapter_for", return_value=CheckJournal()):
            self.session([profile(name) for name in "abcd"]).run()

    def test_critical_failure_waits_for_inflight_finish_policy_before_rollback(self):
        entered, failed, release, finished = (threading.Event() for _ in range(4))
        test = self

        class InFlight(Adapter):
            def prepare(self, runtime, cancel):
                if runtime.profile.identifier == "inflight":
                    entered.set()
                    test.assertTrue(release.wait(3))
                    finished.set()
                    return "safely finished"
                test.assertTrue(entered.wait(3))
                failed.set()
                raise OSError("actual critical failure")

            def rollback(self, runtime):
                test.assertTrue(finished.is_set(), "rollback raced an in-flight mutation")
                return "restored"

        session = self.session([profile("inflight", cancel_policy="finish-then-rollback"), profile("bad")])
        errors = []

        def run():
            try:
                session.run()
            except Exception as error:
                errors.append(error)

        with patch.object(profiles, "_adapter_for", return_value=InFlight()):
            runner = threading.Thread(target=run)
            runner.start()
            try:
                self.assertTrue(failed.wait(3))
                self.assertTrue(runner.is_alive())
                release.set()
                runner.join(timeout=3)
                self.assertFalse(runner.is_alive())
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], profiles.ShutdownProfileError)
                self.assertIn("actual critical failure", str(errors[0]))
                session.rollback_all("test rollback")
            finally:
                release.set()
                runner.join(timeout=3)

    def test_duplicate_vm_rejected_before_any_probe(self):
        raw = {"schema_version": 1, "label": "VM", "adapter": "qemu-windows-hibernate",
               "adapter_config": {"vm_directory": "/tmp/wsctl-test-vm"}}
        session = self.session([profiles._profile_from_mapping({**raw, "id": name}) for name in ("one", "two")])
        with patch.object(profiles, "_adapter_for") as adapter:
            with self.assertRaisesRegex(profiles.ShutdownProfileError, "duplicate QEMU"):
                session.run()
        adapter.assert_not_called()
        self.writer.assert_not_called()

    def test_legacy_vm_default_does_not_change_serialized_fingerprint(self):
        raw = {"schema_version": 1, "id": "vm", "label": "VM", "adapter": "qemu-windows-hibernate",
               "adapter_config": {"vm_directory": "/tmp/wsctl-test-vm"}}
        saved = profiles._profile_mapping(profiles._profile_from_mapping(raw))
        self.assertNotIn("parallel", saved)
        self.assertEqual(profiles._profile_mapping(profiles._profile_from_mapping(saved)), saved)


if __name__ == "__main__":
    unittest.main()
