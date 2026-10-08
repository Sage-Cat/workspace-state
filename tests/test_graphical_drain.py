from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import graphical_drain as drain, operations
from workspace_state.util import atomic_json


BOOT = "01234567-0123-0123-0123-0123456789ab"
NAME = "wsctl-app-chrome-Default-abcdef01.service"
INVOCATION = "a" * 32


def proof(name=NAME):
    return {"unit": name, "invocation_id": INVOCATION,
            "control_group": f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/{name}",
            "helper": "/sealed/graphical_stop.py"}


def journal_record(owner=None, **values):
    owner = owner or proof()
    return {"_BOOT_ID": BOOT.replace("-", ""), "_UID": str(os.getuid()), "_COMM": "systemd",
            "_SYSTEMD_USER_UNIT": "init.scope", "USER_UNIT": owner["unit"],
            "USER_INVOCATION_ID": owner["invocation_id"], "JOB_TYPE": "stop", "JOB_RESULT": "done",
            **values}


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.releases = self.root / "desktop-releases"
        self.release = self.releases / ("r-" + "a" * 24)
        self.helper = self.release / "components/workspace-state/src/workspace_state/graphical_stop.py"
        self.helper.parent.mkdir(parents=True)
        self.helper.write_text("# sealed fixture\n")
        for path in [self.helper, *self.helper.parents[:5]]:
            path.chmod(0o555 if path.is_dir() else 0o444)
        self.props = {"Id": NAME, "LoadState": "loaded", "Transient": "yes",
                      "ControlGroup": proof()["control_group"], "InvocationID": INVOCATION,
                      "PartOf": "graphical-session.target", "Result": "success", "Job": "",
                      "ActiveState": "active", "SubState": "running",
                      "FragmentPath": f"/run/user/{os.getuid()}/systemd/transient/{NAME}"}
        self.commands = [["/usr/bin/python3", ["/usr/bin/python3", "-I", str(self.helper), NAME],
                          False, 0, 0, 0, 0, 0, 0, 0]]

    def tearDown(self):
        for path in [self.release, *self.release.rglob("*")]:
            if not path.is_symlink():
                path.chmod(0o700 if path.is_dir() else 0o600)
        self.temporary.cleanup()

    def test_exact_transient_user_unit_and_structured_command_are_proven(self):
        result = drain.prove_unit(NAME, self.props, self.commands)
        self.assertEqual(result, {**proof(), "helper": str(self.helper)})

    def test_foreign_static_unbound_or_wrong_invocation_unit_is_rejected(self):
        changes = {"Id": "unrelated.service", "Transient": "no", "PartOf": "default.target",
                   "InvocationID": "", "ControlGroup": "/system.slice/" + NAME,
                   "FragmentPath": "/etc/systemd/user/" + NAME}
        for key, value in changes.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                drain.prove_unit(NAME, {**self.props, key: value}, self.commands)

    def test_stop_command_cannot_hide_different_arguments_or_ignored_failure(self):
        variants = []
        for index, value in [(0, "/bin/sh"), (2, True)]:
            commands = deepcopy(self.commands)
            commands[0][index] = value
            variants.append(commands)
        for index, value in [(0, "python3"), (1, "-c"), (3, "wsctl-app-*.service")]:
            commands = deepcopy(self.commands)
            commands[0][1][index] = value
            variants.append(commands)
        variants.extend([[], self.commands * 2])
        for commands in variants:
            with self.subTest(commands=commands), self.assertRaises(ValueError):
                drain.prove_unit(NAME, self.props, commands)

    def test_existing_checkout_and_symlink_helpers_do_not_block_owned_units(self):
        checkout = self.root / "checkout" / "graphical_stop.py"
        checkout.parent.mkdir()
        checkout.write_text("# existing app helper\n")
        alias = self.releases / "current"
        alias.symlink_to(self.release)
        self.helper.chmod(0o644)
        for helper in (self.helper, checkout, alias / self.helper.relative_to(self.release)):
            commands = deepcopy(self.commands)
            commands[0][1][2] = str(helper)
            with self.subTest(helper=helper):
                self.assertEqual(drain.prove_unit(NAME, self.props, commands)["helper"], str(helper))

    def test_wrong_helper_or_relative_command_is_rejected(self):
        for helper in ("graphical_stop.py", "/tmp/unrelated.py"):
            commands = deepcopy(self.commands)
            commands[0][1][2] = helper
            with self.subTest(helper=helper), self.assertRaises(ValueError):
                drain.prove_unit(NAME, self.props, commands)

    def test_inventory_only_selects_exact_owned_name_shape(self):
        records = [{"unit": name, "active": "active"} for name in [
            NAME, "wsctl-app-chrome-abc.service", "wsctl-app-chrome-abcdef01.service.bak",
            "wsctl-bootstrap-terminal-abcd1234.service", "app-com.google.Chrome-123.scope"]]
        records.append({"unit": "wsctl-app-old-aaaaaaaa.service", "active": "inactive"})
        manager = drain.Manager(time.monotonic() + 10, BOOT)
        manager.run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(records), ""))
        self.assertEqual(manager.candidates(), [NAME])

    def test_stop_uses_exact_argument_and_no_shell_or_wildcard(self):
        manager = drain.Manager(time.monotonic() + 10, BOOT)
        manager.run = Mock(return_value=subprocess.CompletedProcess([], 0, "", ""))
        manager.stop(proof())
        manager.run.assert_called_once_with([
            "/usr/bin/systemctl", "--user", "--no-pager", "--job-mode=fail", "stop", NAME], stop=True)

    def test_structured_execstop_is_read_without_parsing_shell_text(self):
        manager = drain.Manager(time.monotonic() + 10, BOOT)
        manager.releases = self.releases
        manager.inspect = Mock(return_value=self.props)
        manager.run = Mock(return_value=subprocess.CompletedProcess(
            [], 0, json.dumps({"type": "a(sasbttttuii)", "data": self.commands}), ""))
        self.assertEqual(manager.snapshot(NAME)["helper"], str(self.helper))
        self.assertIn("/usr/bin/busctl", manager.run.call_args.args[0])
        self.assertEqual(manager.run.call_args.args[0][-1], "ExecStop")


class ResultTests(unittest.TestCase):
    def test_completion_requires_this_boot_manager_unit_and_invocation(self):
        valid = journal_record()
        self.assertEqual(drain.journal_result([valid], proof(), BOOT), (True, []))
        for key, value in {"USER_INVOCATION_ID": "b" * 32, "USER_UNIT": "other.service",
                           "_BOOT_ID": "other", "_UID": "999999", "_COMM": "app",
                           "_SYSTEMD_USER_UNIT": NAME}.items():
            with self.subTest(key=key):
                self.assertEqual(drain.journal_result([{**valid, key: value}], proof(), BOOT), (False, []))

    def test_stop_done_cannot_mask_timeout_or_execstop_failure_after_collection(self):
        for failure in [{"UNIT_RESULT": "timeout"}, {"COMMAND": "ExecStop", "EXIT_STATUS": "1"},
                        {"EXIT_CODE": "killed", "EXIT_STATUS": "9"}, {"JOB_RESULT": "failed"}]:
            with self.subTest(failure=failure):
                complete, errors = drain.journal_result([journal_record(**failure), journal_record()], proof(), BOOT)
                self.assertTrue(complete)
                self.assertTrue(errors)

    def test_residual_child_cleanup_is_not_a_main_or_helper_failure(self):
        records = [journal_record(MESSAGE="Killing residual process 321 with signal SIGKILL.")]
        self.assertEqual(drain.journal_result(records, proof(), BOOT), (True, []))


class FakeManager:
    def __init__(self, names=None):
        self.boot = BOOT
        self.names = names or [NAME]
        self.stops = []
        self.snapshots = []
        self.pending = False
        self.alive = False
        self.records = None
        self.snapshot_error = None
        self.stop_error = None
        self.barrier = None

    def candidates(self):
        return self.names

    def snapshot(self, name):
        self.snapshots.append(name)
        if self.snapshot_error:
            raise self.snapshot_error
        return proof(name)

    def stop(self, owner):
        self.stops.append(owner["unit"])
        if self.barrier:
            self.barrier.wait(timeout=2)
        if self.stop_error:
            raise self.stop_error

    def inspect(self, name):
        if self.pending:
            return {"LoadState": "loaded", "InvocationID": INVOCATION, "ActiveState": "deactivating", "Job": "55"}
        if self.alive:
            return {"LoadState": "loaded", "InvocationID": INVOCATION, "ActiveState": "active", "Job": ""}
        return {"LoadState": "not-found", "ActiveState": "inactive", "Job": ""}

    def empty(self, owner):
        return not self.alive

    def journal(self, owner):
        return self.records if self.records is not None else [journal_record(owner)]


class DrainTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.receipt = self.root / "receipt.json"
        self.context = operations.OperationContext(BOOT, "login", "operation", "shutdown", 1, time.monotonic() + 60)
        self.manager = FakeManager()
        self.authorization = patch.object(drain, "authorized")
        self.authorized = self.authorization.start()
        self.addCleanup(self.authorization.stop)
        guard = patch('workspace_state.shutdown_checkpoint_guard.validate_drain_plan')
        self.guard = guard.start()
        self.addCleanup(guard.stop)
        self.portal = Mock(side_effect=lambda context, *_args, **_kwargs: {
            "schema_version": 1, "operation_context": context.to_dict(),
            "status": "succeeded", "settled": True, "units": [], "requests": {}, "errors": [],
            "not_running": True, "settlement_only": False,
        })

    def run_drain(self, **kwargs):
        return drain.drain(self.context, self.receipt,
                           manager_factory=lambda *_: self.manager, portal_runner=self.portal, **kwargs)

    def existing(self, status="running", **values):
        atomic_json(self.receipt, {"schema_version": 1, "operation_context": self.context.to_dict(),
                    "status": status, "settled": False, "deadline": time.monotonic() - 1,
                    "units": [proof()], "requests": {NAME: "issuing"}, "errors": [], **values})

    def test_changed_checkpoint_plan_refuses_every_stop(self):
        self.guard.side_effect = ValueError('application inventory changed after checkpoint')
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])
        self.assertTrue(result['settled'])
        self.assertEqual(set(result['requests'].values()), {'planned'})
        self.assertIn('application inventory changed', '; '.join(result['errors']))

    def test_stops_all_proven_units_concurrently_after_durable_intent(self):
        self.manager.names = [NAME, "wsctl-app-chatgpt-1234abcd.service"]
        self.manager.barrier = threading.Barrier(2)
        original_stop = self.manager.stop

        def stop(owner):
            record = drain.private_json(self.receipt)
            self.assertEqual(len(record["units"]), 2)
            self.assertEqual(record["requests"][owner["unit"]], "issuing")
            original_stop(owner)

        self.manager.stop = stop
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 0)
        self.assertCountEqual(self.manager.stops, self.manager.names)
        self.assertEqual(result["requests"], {name: "done" for name in self.manager.names})
        self.assertGreaterEqual(result["finished_at"], result["started_at"])

    def test_portal_runs_after_app_settlement_and_is_required_for_handoff(self):
        original = self.portal.side_effect
        def portal(context, *args, **kwargs):
            self.assertEqual(self.manager.stops, [NAME])
            self.assertFalse(self.manager.pending)
            return original(context, *args, **kwargs)
        self.portal.side_effect = portal
        result = self.run_drain()
        self.assertTrue(result["portal_required"])
        drain.validate_receipt(result, self.context, require_portal=True)

    def test_unsettled_app_jobs_prevent_portal_stop_phase(self):
        self.manager.pending = True
        result = self.run_drain(timeout=.01)
        self.assertEqual(drain.exit_status(result), 75)
        self.portal.assert_not_called()

    def test_pending_portal_retains_parent_ownership_and_joins_without_reissuing_apps(self):
        pending = self.portal.side_effect(self.context)
        pending.update(status="failed", settled=False, errors=["portal pending"])
        done = {**pending, "settled": True}
        self.portal.side_effect = [pending, done]
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 75)
        result = self.run_drain(settle_only=True)
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [NAME])
        self.assertTrue(self.portal.call_args.kwargs["settle_only"])

    def test_child_receipt_write_error_retains_native_settlement_owner(self):
        def child_error(*_args, **_kwargs):
            durable = drain.private_json(self.receipt)
            self.assertTrue(durable["portal_required"])
            self.assertFalse(durable["settled"])
            raise OSError("native stop issued; nested receipt persistence unavailable")
        self.portal.side_effect = child_error
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 75)
        self.assertTrue(result["portal_required"])
        self.assertNotIn("portal", result)
        self.portal.side_effect = lambda context, *_args, **_kwargs: {
            "schema_version": 1, "operation_context": context.to_dict(),
            "status": "failed", "settled": True, "units": [], "requests": {}, "errors": [],
            "not_running": False, "settlement_only": True,
        }
        result = self.run_drain(settle_only=True)
        self.assertEqual(drain.exit_status(result), 1)
        self.assertTrue(self.portal.call_args.kwargs["settle_only"])
        self.assertEqual(self.manager.stops, [NAME])

    def test_direct_file_entrypoint_can_import_native_phase_and_validate_nested_receipt(self):
        # GNOME executes this file with python -I, rather than importing the
        # module. Exercise that package-less namespace without host mutations.
        entry = runpy.run_path(str(Path(drain.__file__).resolve()), run_name="_direct_drain_fixture")
        entry["drain"].__globals__["authorized"] = Mock()
        result = entry["drain"](self.context, self.receipt,
                                manager_factory=lambda *_args: self.manager, portal_runner=self.portal)
        self.assertEqual(entry["exit_status"](result), 0)
        entry["validate_receipt"](result, self.context, require_portal=True)

    def test_old_app_only_success_cannot_authorize_handoff(self):
        self.existing(status="succeeded", settled=True)
        with self.assertRaisesRegex(ValueError, "native document portal"):
            drain.validate_receipt(drain.private_json(self.receipt), self.context, require_portal=True)

    def test_any_ambiguous_candidate_prevents_all_stop_requests(self):
        self.manager.snapshot_error = ValueError("unrelated transient lookalike")
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])

    def test_replaced_invocation_is_not_stopped(self):
        calls = 0

        def snapshot(name):
            nonlocal calls
            calls += 1
            return {**proof(name), "invocation_id": INVOCATION if calls == 1 else "b" * 32}

        self.manager.snapshot = snapshot
        self.manager.alive = True
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])
        self.assertTrue(any("changed before stop" in error for error in result["errors"]))

    def test_withdrawal_after_snapshot_does_not_stop_unchanged_live_app(self):
        self.authorized.side_effect = [None, ValueError("authorization withdrawn")]
        self.manager.alive = True
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertTrue(result["settled"])
        self.assertEqual(self.manager.stops, [])

    def test_interrupted_intent_rejoins_without_new_stops(self):
        self.existing(requests={NAME: "planned"})
        self.manager.alive = True
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])
        self.assertEqual(self.manager.snapshots, [])

    def test_interrupted_issuing_without_visible_job_retains_ownership(self):
        self.existing(requests={NAME: "issuing"})
        self.manager.alive = True
        result = self.run_drain(settle_only=True, timeout=.02)
        self.assertEqual(drain.exit_status(result), 75)
        self.assertFalse(result["settled"])
        self.assertTrue(any("stop issuance is unresolved" in error for error in result["errors"]))
        self.assertEqual(self.manager.stops, [])
        # A later join must not turn the still-unknown issuing state into a
        # known failed request, or enqueue a replacement stop of its own.
        result = self.run_drain(settle_only=True, timeout=.02)
        self.assertEqual(drain.exit_status(result), 75)
        self.assertEqual(result["requests"][NAME], "issuing")
        self.assertEqual(self.manager.stops, [])

    def test_interrupted_issuing_requires_stop_job_not_just_natural_exit(self):
        self.existing()
        self.manager.records = [journal_record(JOB_TYPE=None, JOB_RESULT=None,
                                MESSAGE_ID="7ad2d189f7e94e70a38c781354912448")]
        result = self.run_drain(settle_only=True, timeout=.02)
        self.assertEqual(drain.exit_status(result), 75)
        self.manager.records = [journal_record()]
        result = self.run_drain(settle_only=True)
        self.assertTrue(result["settled"])
        # The earlier uncertainty is a sticky failure, never a late commit.
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(self.manager.stops, [])

    def test_explicit_coordinator_deadline_is_not_extended_by_spawn_time(self):
        deadline = time.monotonic() + 12
        result = self.run_drain(action_deadline=deadline)
        self.assertEqual(drain.exit_status(result), 0)
        self.assertEqual(result["deadline"], deadline)
        self.assertLessEqual(self.manager.deadline, deadline)

    def test_failed_receipt_stays_failed_after_remaining_jobs_settle(self):
        self.existing(status="failed", errors=["original timeout"])
        result = self.run_drain(settle_only=True)
        self.assertEqual(drain.exit_status(result), 1)
        self.assertTrue(result["settled"])
        self.assertEqual(self.manager.stops, [])

    def test_timeout_retains_ownership_and_next_join_only_observes(self):
        self.manager.pending = True
        self.manager.stop_error = subprocess.TimeoutExpired("systemctl", .01)
        result = self.run_drain(timeout=.02)
        self.assertEqual(drain.exit_status(result), 75)
        self.assertFalse(result["settled"])
        old_deadline = result["deadline"]
        self.manager.pending = False
        result = self.run_drain(settle_only=True)
        self.assertEqual(drain.exit_status(result), 1)
        self.assertEqual(result["deadline"], old_deadline)
        self.assertEqual(self.manager.stops, [NAME])

    def test_collected_failed_unit_cannot_report_success(self):
        self.manager.records = [journal_record(UNIT_RESULT="exit-code"), journal_record()]
        result = self.run_drain()
        self.assertEqual(drain.exit_status(result), 1)
        self.assertTrue(result["settled"])

    def test_missing_completion_receipt_is_settled_failure_after_budget(self):
        self.manager.records = []
        result = self.run_drain(timeout=.02)
        self.assertEqual(drain.exit_status(result), 1)
        self.assertTrue(result["settled"])

    def test_settle_only_without_prior_intent_never_inspects_or_stops(self):
        self.manager.candidates = Mock(side_effect=AssertionError("must not enumerate"))
        result = self.run_drain(settle_only=True)
        self.assertEqual(drain.exit_status(result), 0)
        self.assertEqual(result["units"], [])
        self.authorized.assert_not_called()

    def test_completed_receipt_is_idempotent_and_context_replacement_is_rejected(self):
        result = self.run_drain()
        self.assertEqual(self.run_drain(), result)
        self.assertEqual(self.manager.stops, [NAME])
        self.context = operations.OperationContext(BOOT, "other-login", "operation", "shutdown", 1, self.context.deadline)
        with self.assertRaisesRegex(ValueError, "another operation"):
            self.run_drain()

    def test_private_receipt_rejects_symlink_and_shared_permissions(self):
        self.existing()
        self.receipt.chmod(0o644)
        with self.assertRaisesRegex(ValueError, "unsafe"):
            drain.private_json(self.receipt)
        alias = self.root / "alias"
        alias.symlink_to(self.receipt)
        with self.assertRaises(OSError):
            drain.private_json(alias)


class AuthorizationTests(unittest.TestCase):
    def test_cancellation_or_changed_epoch_revokes_before_stop(self):
        context = operations.OperationContext(BOOT, "login", "operation", "shutdown", 1, 100)
        document = {"operation_context": context.to_dict(), "mode": "shutdown", "session_id": "login",
                    "operation_id": "operation", "operation_state": "authorized", "commit_authorized": True}
        event = threading.Event()
        with patch.object(operations.OperationContext, "check"), patch.object(drain, "private_json", return_value=document):
            drain.authorized(context, event)
            for changes in [{"cancelled": True}, {"commit_authorized": False},
                            {"operation_state": "recovering"}, {"session_id": "other"}]:
                with patch.object(drain, "private_json", return_value={**document, **changes}):
                    with self.subTest(changes=changes), self.assertRaises(ValueError):
                        drain.authorized(context, event)
            event.set()
            with self.assertRaises(ValueError):
                drain.authorized(context, event)


if __name__ == "__main__":
    unittest.main()
