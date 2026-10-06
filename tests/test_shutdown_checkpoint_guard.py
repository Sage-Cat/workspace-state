from __future__ import annotations

import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import cli, operations, shutdown_checkpoint_guard as guard, shutdown_finalize
from workspace_state.util import atomic_json


class LiveCheckpointIdentityTests(unittest.TestCase):
    def test_sealing_uses_native_pane_identity_like_capture(self):
        row = 'work\t1\tnotes\tlayout\t1\t0\t%7\t123\t/work\tcodex\t1\n'
        identity = {'pid': 456, 'session_id': '11111111-1111-4111-8111-111111111111'}
        with patch.object(guard, 'run', side_effect=[row, 'notes\n']), \
                patch.object(guard, '_process', return_value={'start_ticks': 12}), \
                patch('workspace_state.capture.codex_for_pane', return_value=identity) as capture_identity:
            live = guard._live_sessions()
        capture_identity.assert_called_once_with(123, '/work', pane_id='%7')
        self.assertEqual(live[0]['windows'][0]['panes'][0]['codex'], identity['session_id'])
        guard._verify_saved_sessions({'sessions': live}, live, degraded=False)


class ShutdownCheckpointGuardTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        environment = patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "data"),
                                               "XDG_RUNTIME_DIR": str(self.root / "runtime")})
        environment.start()
        self.addCleanup(environment.stop)
        self.runtime = self.root / "runtime/workspace-state"
        self.runtime.mkdir(mode=0o700, parents=True)
        (self.runtime / "login-generation").write_text("a" * 16)
        self.canonical = self.root / "data/workspace-state/snapshots/current.json"
        atomic_json(self.canonical, {"version": 5, "name": "current", "sessions": [], "terminals": []})
        self.tmux_dir = self.root / "tmux"
        self.tmux_dir.mkdir()
        self.tmux = self.tmux_dir / "tmux_resurrect_20261005T070001.txt"
        self.tmux.write_bytes(b"original pre-drain tmux checkpoint\n")
        self.tmux.chmod(0o644)  # The plugin need not use the private bundle mode.
        (self.tmux_dir / "last").symlink_to(self.tmux.name)
        self.proof = {"unit": "wsctl-app-terminal-12345678.service", "invocation_id": "c" * 32,
                      "control_group": f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/wsctl-app-terminal-12345678.service",
                      "helper": "/synthetic/immutable/graphical_stop.py"}
        self.unit_names = [self.proof["unit"]]
        self.manager = Mock()
        self.manager.candidates.side_effect = lambda: self.unit_names[:]
        self.manager.snapshot.side_effect = lambda unit: copy.deepcopy(self.proof)
        self.manager.inspect.side_effect = lambda unit: {"InvocationID": self.proof["invocation_id"], "ActiveState": "inactive", "Result": "success", "Job": ""}
        self.shell = {"available": True, "windows": [
            {"id": 1, "pid": 100, "app_id": "Alacritty", "workspace": 0, "monitor": 0,
             "geometry": {"x": 0, "y": 0, "width": 800, "height": 600}, "state": "normal"},
            {"id": 2, "pid": 200, "app_id": "nemo", "workspace": 1, "monitor": 0,
             "geometry": {"x": 20, "y": 20, "width": 800, "height": 600}, "state": "normal"}]}
        self.providers = {"nemo": {"pid": 200, "windows": [{"id": 20, "locations": ["file:///synthetic"], "active_tab": 0, "complete": True}]}}
        self.real_providers = guard._providers
        self.live = [{"name": "main", "windows": [{"index": 1, "name": "work", "layout": "layout-1", "active": True,
                     "panes": [{"index": 0, "id": "%1", "pid": 300, "start_ticks": 12, "active": True,
                                "label": None, "command": "zsh", "cwd": "/synthetic", "codex": "synthetic-uuid"}]}]}]
        snapshot_sessions = copy.deepcopy(self.live)
        snapshot_sessions[0]["windows"][0]["panes"][0]["codex"] = {"session_id": "synthetic-uuid"}
        atomic_json(self.canonical, {"version": 5, "name": "current", "sessions": snapshot_sessions, "terminals": []})
        patches = [patch.object(operations, "boot_id", return_value="test-boot"),
                   patch.object(guard, "_tmux_directory", return_value=self.tmux_dir),
                   patch.object(guard, "capture_shell", side_effect=lambda **kw: copy.deepcopy(self.shell)),
                   patch.object(guard, "_providers", side_effect=lambda shell: copy.deepcopy(self.providers)),
                   patch.object(guard, "_process", side_effect=lambda pid: {"start_ticks": pid + 5,
                               "control_group": self.proof["control_group"] if pid == 100 else "/unmanaged/nemo.scope"}),
                   patch.object(guard, "_live_sessions", side_effect=lambda: copy.deepcopy(self.live)),
                   patch.object(guard, "Manager", return_value=self.manager),
                   patch.object(guard, "reconcile", return_value=(True, True, []))]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(operations.bind, None)
        self.context = self.new_context("b")

    def new_context(self, letter, state="preparing"):
        context = operations.OperationContext.create("a" * 16, "shutdown", operation_id=letter * 32)
        operations.bind(context)
        self.status(context, state)
        return context

    def status(self, context, state):
        atomic_json(self.runtime / "login-hud-status.json", {"schema_version": 1, "mode": "shutdown",
            "session_id": context.login_generation, "operation_id": context.operation_id,
            "operation_context": context.to_dict(), "operation_state": state,
            "commit_authorized": state == "authorized", "cancelled": state == "cancelled"})

    def arm(self, *, degraded=False):
        self.descriptor = guard.seal_checkpoint(self.context, "d" * 32, degraded=degraded)
        self.completion = {"operation_context": self.context.to_dict(), "invocation_id": "d" * 32,
                           "checkpoint_bundle": self.descriptor}
        self.status(self.context, "authorized")
        guard.arm_retry_protection(self.completion)
        return self.completion

    def ledger(self, *, settled=True, requests=None, units=None):
        atomic_json(self.runtime / f"shutdown-graphical-drain-{self.context.operation_id}.json", {
            "schema_version": 1, "operation_context": self.context.to_dict(), "status": "failed",
            "settled": settled, "errors": ["synthetic portal cancellation"],
            "units": copy.deepcopy([self.proof] if units is None else units),
            "requests": {self.proof["unit"]: "done"} if requests is None else requests})

    def partial_desktop(self):
        self.shell["windows"] = self.shell["windows"][1:]
        self.unit_names = []

    def test_seal_is_private_immutable_and_binds_exact_bytes(self):
        descriptor = guard.seal_checkpoint(self.context, "d" * 32, degraded=True)
        path = self.root / "data/workspace-state/shutdown-checkpoints" / descriptor["bundle_name"]
        self.assertEqual(path.stat().st_mode & 0o777, 0o400)
        value = guard._bundle(descriptor)
        self.assertEqual(guard._record_bytes(value["canonical"]), self.canonical.read_bytes())
        self.assertEqual(guard._record_bytes(value["tmux"]), self.tmux.read_bytes())
        self.assertTrue(value["degraded"])

    def test_partial_survivors_reuse_original_checkpoint_without_writes(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        before = self.canonical.read_bytes(), self.tmux.read_bytes()
        new = self.new_context("e")
        self.assertTrue(guard.reuse_if_protected(new))
        self.assertEqual(before, (self.canonical.read_bytes(), self.tmux.read_bytes()))
        self.assertTrue(guard.protected())
        descriptor = guard.seal_checkpoint(new, "f" * 32)
        self.assertEqual(guard._bundle(descriptor)["inherited"], self.descriptor)

    def test_retry_worker_skips_all_save_jobs_with_explicit_ready_stages(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        with patch.object(shutdown_finalize, "_run_checkpoint") as save, patch.object(shutdown_finalize, "update_stage") as stage:
            self.assertFalse(shutdown_finalize._save_checkpoints(Path("/synthetic/bin"), new.operation_id, Mock()))
        save.assert_not_called()
        self.assertEqual([call.args[0:2] for call in stage.call_args_list], [("tmux-save", "ready"), ("workspace-save", "ready"),
             ("social-apps-save", "ready"), ("file-manager-save", "ready"), ("vscode-save", "ready")])

    def test_retry_keeps_inherited_degradation_and_completes_app_stages(self):
        self.arm(degraded=True)
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        with patch.object(shutdown_finalize, "_run_checkpoint") as save, patch.object(shutdown_finalize, "update_stage") as stage:
            self.assertTrue(shutdown_finalize._save_checkpoints(Path("/synthetic/bin"), new.operation_id, Mock()))
        save.assert_not_called()
        updates = {call.args[0]: call.args for call in stage.call_args_list}
        self.assertEqual(updates["workspace-save"][1], "degraded")
        self.assertIn("safe fallback", updates["workspace-save"][2])
        for identifier in ("tmux-save", "social-apps-save", "file-manager-save", "vscode-save"):
            self.assertEqual(updates[identifier][1], "ready")
            self.assertIn("no state recaptured", updates[identifier][2])

    def test_unsettled_or_missing_ledger_blocks_before_any_save(self):
        self.arm()
        new = self.new_context("e")
        with self.assertRaises(OSError):
            guard.reuse_if_protected(new)
        self.ledger(settled=False)
        with self.assertRaisesRegex(ValueError, "still settling"):
            guard.reuse_if_protected(new)
        self.ledger(requests={self.proof["unit"]: "issuing"})
        with self.assertRaisesRegex(ValueError, "still settling"):
            guard.reuse_if_protected(new)

    def test_no_stop_receipt_unarms_and_allows_fresh_save(self):
        self.arm()
        self.ledger(requests={}, units=[])
        new = self.new_context("e")
        self.assertFalse(guard.reuse_if_protected(new))
        self.assertFalse(guard.protected())

    def test_changed_canonical_or_tmux_bytes_block_retry(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        original = self.canonical.read_bytes()
        self.canonical.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            guard.reuse_if_protected(new)
        self.canonical.write_bytes(original)
        self.tmux.write_bytes(b"new tmux checkpoint")
        with self.assertRaisesRegex(ValueError, "checkpoint changed"):
            guard.reuse_if_protected(new)

    def test_changed_tmux_structure_or_identity_blocks_retry(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        for key, value in (("layout", "new-layout"), ("name", "renamed")):
            original = self.live[0]["windows"][0][key]
            self.live[0]["windows"][0][key] = value
            with self.assertRaisesRegex(ValueError, "tmux sessions changed"):
                guard.reuse_if_protected(new)
            self.live[0]["windows"][0][key] = original
        self.live[0]["windows"][0]["panes"][0]["codex"] = "another-uuid"
        with self.assertRaisesRegex(ValueError, "tmux sessions changed"):
            guard.reuse_if_protected(new)

    def test_new_or_moved_surviving_window_blocks_retry(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        self.shell["windows"][0]["workspace"] = 3
        with self.assertRaisesRegex(ValueError, "surviving desktop windows changed"):
            guard.reuse_if_protected(new)
        self.shell["windows"][0]["workspace"] = 1
        self.shell["windows"].append({"id": 99, "pid": 200})
        with self.assertRaisesRegex(ValueError, "surviving desktop windows changed"):
            guard.reuse_if_protected(new)

    def test_changed_surviving_nemo_content_blocks_retry(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        self.providers["nemo"]["windows"][0]["locations"].append("file:///new-tab")
        with self.assertRaisesRegex(ValueError, "surviving nemo content changed"):
            guard.reuse_if_protected(new)

    def test_browser_provider_projection_keeps_groups_and_ignores_loading_titles(self):
        from workspace_state import browser, file_manager, vscode
        captured = {"profile": "Default", "windows": [{"id": "window-1", "runtime_window_id": 10,
            "focused": True, "groups": [{"id": "group-1", "title": "Work", "color": "blue", "collapsed": True}],
            "tabs": [{"url": "https://example.test/", "title": "Loading", "pinned": False,
                      "group": "group-1", "active": True}]}]}
        with patch.object(file_manager, "matching_windows", return_value=[]), \
                patch.object(vscode, "matching_windows", return_value=[]), \
                patch.object(browser, "_shell_browser_windows", return_value=[{"id": 1}]), \
                patch.object(browser, "_host_paths", return_value=[Path("/synthetic/chrome.sock")]), \
                patch.object(browser, "_request_path", side_effect=lambda *args, **kw: copy.deepcopy(captured)):
            before = self.real_providers(self.shell)
            captured["windows"][0]["focused"] = False
            captured["windows"][0]["tabs"][0]["title"] = "Loaded page"
            self.assertEqual(self.real_providers(self.shell), before)
            captured["windows"][0]["tabs"][0]["group"] = None
            self.assertNotEqual(self.real_providers(self.shell), before)

    def test_migrated_chrome_window_is_bound_to_exact_stop_unit(self):
        from workspace_state import graphical_stop
        self.shell["windows"][0]["app_id"] = "google-chrome"
        owner = graphical_stop.Process(100, 1, 105, (1, 2), os.getuid(), "/unmanaged/google-chrome.scope")
        with patch.object(guard, "_process", side_effect=lambda pid: {"start_ticks": pid + 5, "control_group": "/unmanaged/google-chrome.scope" if pid == 100 else "/unmanaged/nemo.scope"}), \
                patch.object(graphical_stop, "migrated_owner", return_value=owner):
            self.descriptor = guard.seal_checkpoint(self.context, "d" * 32)
            self.status(self.context, "authorized")
            completion = {"operation_context": self.context.to_dict(), "invocation_id": "d" * 32, "checkpoint_bundle": self.descriptor}
            guard.arm_retry_protection(completion)
        pointer = json.loads(guard._pointer().read_text())
        self.assertEqual(pointer["window_units"]["1"], self.proof["unit"])
        self.ledger()
        self.partial_desktop()
        self.assertTrue(guard.reuse_if_protected(self.new_context("e")))

    def test_real_editor_owner_proof_maps_native_window_for_cancel_retry(self):
        from workspace_state import graphical_stop
        unit = "wsctl-app-vscode-native-recovery-abcd1234.service"
        group = str(Path(self.proof["control_group"]).parent / unit)
        scope = str(Path(group).parent / "app-com.microsoft.VSCode-100.scope")
        self.proof = dict(self.proof, unit=unit, control_group=group)
        self.unit_names = [unit]
        self.shell["windows"][0]["app_id"] = "com.microsoft.vscode"
        proc = self.root / "editor-proc"
        cgroups = self.root / "editor-cgroups"
        members = cgroups / group.lstrip("/") / "cgroup.procs"
        members.parent.mkdir(parents=True)
        members.write_text("101\n")
        executable = self.root / "editor-executable"
        executable.write_text("synthetic Electron executable inode")
        for pid, parent, ticks, control_group in ((100, 1, 105, scope), (101, 100, 110, group)):
            directory = proc / str(pid)
            directory.mkdir(parents=True)
            fields = ["S", str(parent), *(["0"] * 17), str(ticks)]
            (directory / "stat").write_text(f"{pid} (synthetic editor) " + " ".join(fields))
            (directory / "cgroup").write_text(f"0::{control_group}\n")
            (directory / "exe").symlink_to(executable)
        original_owner = graphical_stop.migrated_owner
        with patch.object(guard, "_process", side_effect=lambda pid: {"start_ticks": pid + 5,
                               "control_group": scope if pid == 100 else "/unmanaged/nemo.scope"}), \
                patch.object(graphical_stop, "migrated_owner", side_effect=lambda path: original_owner(path, proc=proc, cgroups=cgroups)):
            self.arm()
        pointer = json.loads(guard._pointer().read_text())
        self.assertEqual(pointer["window_units"]["1"], unit)
        self.ledger()
        self.partial_desktop()
        before = self.canonical.read_bytes(), self.tmux.read_bytes()
        self.assertTrue(guard.reuse_if_protected(self.new_context("e")))
        self.assertEqual(before, (self.canonical.read_bytes(), self.tmux.read_bytes()))

    def test_unowned_disappeared_window_blocks_retry(self):
        self.arm()
        self.ledger()
        self.shell["windows"] = []
        self.providers = {}
        self.unit_names = []
        new = self.new_context("e")
        with self.assertRaisesRegex(ValueError, "unowned original window disappeared"):
            guard.reuse_if_protected(new)

    def test_new_owned_invocation_blocks_retry(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        self.unit_names = ["wsctl-app-new-87654321.service"]
        new = self.new_context("e")
        with self.assertRaisesRegex(ValueError, "new application invocation"):
            guard.reuse_if_protected(new)

    def test_same_second_tmux_overwrite_is_repaired_from_sealed_bytes(self):
        self.arm()
        original = guard._record_bytes(guard._bundle(self.descriptor)["tmux"])
        self.tmux.write_bytes(b"overwritten partial candidate")
        self.assertEqual(guard.restore_protected_tmux_candidate(self.tmux), self.tmux)
        self.assertEqual(self.tmux.read_bytes(), original)
        candidate = self.tmux_dir / "tmux_resurrect_20261005T070002.txt"
        candidate.write_bytes(b"new candidate")
        candidate.chmod(0o644)
        self.assertEqual(guard.restore_protected_tmux_candidate(candidate), self.tmux)
        self.assertEqual(candidate.read_bytes(), original)

    def test_foreign_tmux_candidate_is_not_written(self):
        self.arm()
        candidate = self.root / "tmux_resurrect_20261005T070002.txt"
        candidate.write_bytes(b"keep this")
        with self.assertRaisesRegex(ValueError, "outside the protected"):
            guard.restore_protected_tmux_candidate(candidate)
        self.assertEqual(candidate.read_bytes(), b"keep this")

    def test_manual_save_requires_settled_cancel_then_disarms_exact_ledgers(self):
        self.arm()
        self.ledger()
        with self.assertRaisesRegex(ValueError, "shutdown is still active"):
            guard.check_manual_save_allowed()
        self.status(self.context, "cancelled")
        guard.check_manual_save_allowed()
        guard.clear_after_manual_save()
        self.assertFalse(guard.protected())
        self.ledger(settled=False)  # Altered proof is never covered by the old acknowledgement.
        self.assertTrue(guard.protected())

    def test_unprotected_current_shutdown_refuses_manual_save_before_capture_or_write(self):
        cases = [(state, state in operations.RECOVERY_STATES, state == "authorized")
                 for state in ("running", "preparing", "prepared", "authorized", "cancelling", "recovering", "recovery-failed")]
        cases += [("failed", True, False), ("cancelled", True, False), ("failed", False, True)]
        before = self.canonical.read_bytes()
        for state, recovery_pending, commit_authorized in cases:
            with self.subTest(state=state, recovery_pending=recovery_pending, commit_authorized=commit_authorized):
                self.status(self.context, state)
                status = json.loads((self.runtime / "login-hud-status.json").read_text())
                status.update(recovery_pending=recovery_pending, commit_authorized=commit_authorized)
                atomic_json(self.runtime / "login-hud-status.json", status)
                atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                            "operation_context": self.context.to_dict()})
                self.assertFalse(guard.protected())
                with patch.object(cli, "_capture_all") as capture, patch.object(cli, "save") as publish:
                    with self.assertRaisesRegex(ValueError, "shutdown is still active"):
                        cli.cmd_save(argparse.Namespace(allow_partial=False, shutdown_safe=False))
                capture.assert_not_called()
                publish.assert_not_called()
                self.assertEqual(self.canonical.read_bytes(), before)
                self.assertEqual(json.loads((self.runtime / "login-hud-status.json").read_text()), status)

    def test_unprotected_terminal_shutdown_with_matching_profile_journal_refuses_save(self):
        from workspace_state.shutdown_profiles import transaction_path
        for state in ("failed", "cancelled"):
            with self.subTest(state=state):
                self.status(self.context, state)
                atomic_json(transaction_path(), {"schema_version": 1, "operation_id": self.context.operation_id,
                            "session_id": self.context.login_generation, "action": "poweroff", "profiles": []})
                self.assertFalse(guard.protected())
                with patch.object(cli, "_capture_all") as capture, patch.object(cli, "save") as publish:
                    with self.assertRaisesRegex(ValueError, "profile recovery is still armed"):
                        cli.cmd_save(argparse.Namespace(allow_partial=False, shutdown_safe=False))
                capture.assert_not_called()
                publish.assert_not_called()

    def test_unprotected_settled_terminal_shutdown_permits_save_without_drain_proof(self):
        for state in ("cancelled", "failed"):
            with self.subTest(state=state):
                self.status(self.context, state)
                atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                            "operation_context": self.context.to_dict()})
                self.assertFalse(guard.protected())
                guard.check_manual_save_allowed()
                self.manager.inspect.assert_not_called()

    def test_startup_failure_does_not_require_shutdown_settlement_or_login_proof(self):
        context = operations.OperationContext.create("a" * 16, "startup")
        atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                    "operation_context": context.to_dict()})
        self.status(context, "failed")
        status = json.loads((self.runtime / "login-hud-status.json").read_text())
        status["mode"] = "startup"
        atomic_json(self.runtime / "login-hud-status.json", status)
        guard.check_manual_save_allowed()
        (self.runtime / "login-hud-status.json").unlink()
        (self.runtime / "login-generation").unlink()
        guard.check_manual_save_allowed()

    def test_historical_shutdown_does_not_block_unprotected_manual_save(self):
        for changed, value in (("boot_id", "old-boot"), ("login_generation", "old-login")):
            with self.subTest(changed=changed):
                historical = operations.OperationContext.from_dict(self.context.to_dict() | {changed: value})
                self.status(historical, "recovery-failed")
                atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                            "operation_context": historical.to_dict()})
                guard.check_manual_save_allowed()

    def test_historical_shutdown_status_cannot_override_current_startup_owner(self):
        self.status(self.context, "recovery-failed")
        current = operations.OperationContext.create("a" * 16, "startup")
        atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                    "operation_context": current.to_dict()})
        guard.check_manual_save_allowed()

    def test_current_shutdown_requires_its_own_settlement_status(self):
        self.status(self.context, "cancelled")
        current = operations.OperationContext.create("a" * 16, "shutdown")
        atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                    "operation_context": current.to_dict()})
        with self.assertRaisesRegex(ValueError, "matching current shutdown settlement"):
            guard.check_manual_save_allowed()
        (self.runtime / "login-hud-status.json").unlink()
        with self.assertRaises(FileNotFoundError):
            guard.check_manual_save_allowed()

    def test_expired_current_shutdown_recovery_remains_a_manual_save_blocker(self):
        expired = operations.OperationContext.from_dict(self.context.to_dict() | {"deadline": 1})
        self.status(expired, "recovery-failed")
        atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                    "operation_context": expired.to_dict()})
        with self.assertRaisesRegex(ValueError, "shutdown is still active"):
            guard.check_manual_save_allowed()

    def test_current_boot_shutdown_without_login_proof_cannot_allow_manual_save(self):
        self.status(self.context, "cancelled")
        atomic_json(self.runtime / "current-operation.json", {"schema_version": 1,
                    "operation_context": self.context.to_dict()})
        generation = self.runtime / "login-generation"
        generation.unlink()
        for empty in (False, True):
            with self.subTest(empty=empty):
                if empty:
                    generation.write_text("")
                with patch.object(cli, "_capture_all") as capture, patch.object(cli, "save") as publish:
                    with self.assertRaisesRegex(ValueError, "no current login proof"):
                        cli.cmd_save(argparse.Namespace(allow_partial=False, shutdown_safe=False))
                capture.assert_not_called()
                publish.assert_not_called()

    def test_legacy_issued_drain_refuses_without_bundle(self):
        self.ledger()
        self.assertTrue(guard.protected())
        with self.assertRaisesRegex(ValueError, "no sealed checkpoint"):
            guard.reuse_if_protected(self.new_context("e"))

    def test_corrupt_bundle_or_stale_boot_refuses_retry(self):
        self.arm()
        self.ledger()
        target = self.root / "data/workspace-state/shutdown-checkpoints" / self.descriptor["bundle_name"]
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + b" ")
        target.chmod(0o400)
        with self.assertRaisesRegex(ValueError, "bundle changed"):
            guard.reuse_if_protected(self.new_context("e"))

    def test_other_boot_login_or_withdrawn_owner_cannot_reuse(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        with patch.object(operations, "boot_id", return_value="another-boot"):
            with self.assertRaises(TimeoutError):
                guard.reuse_if_protected(new)
        (self.runtime / "login-generation").write_text("f" * 16)
        with self.assertRaisesRegex(ValueError, "another boot or login"):
            guard.reuse_if_protected(new)
        (self.runtime / "login-generation").write_text("a" * 16)
        self.status(new, "cancelled")
        with self.assertRaisesRegex(ValueError, "ownership was withdrawn"):
            guard.reuse_if_protected(new)

    def test_saved_tmux_structure_race_refuses_seal(self):
        self.live[0]["windows"][0]["layout"] = "changed-after-capture"
        with self.assertRaisesRegex(ValueError, "tmux structure changed after save"):
            guard.seal_checkpoint(self.context, "d" * 32)

    def test_unknown_identity_can_become_proven_without_changing_structure(self):
        snapshot = json.loads(self.canonical.read_text())
        snapshot["sessions"][0]["windows"][0]["panes"][0]["codex"] = {"session_id": None}
        atomic_json(self.canonical, snapshot)
        descriptor = guard.seal_checkpoint(self.context, "d" * 32)
        self.assertEqual(guard._bundle(descriptor)["sessions"], self.live)

    def test_known_identity_cannot_be_replaced_during_seal(self):
        self.live[0]["windows"][0]["panes"][0]["codex"] = "a-different-session"
        with self.assertRaisesRegex(ValueError, "conversation identity changed"):
            guard.seal_checkpoint(self.context, "d" * 32)

    def test_surviving_dirty_editor_requires_explicit_save(self):
        self.providers["vscode"] = [{"pid": 200, "instance": "synthetic", "project": {"dirty_count": 1}}]
        self.arm()
        self.ledger()
        self.partial_desktop()
        with self.assertRaisesRegex(ValueError, "surviving dirty VS Code editors"):
            guard.reuse_if_protected(self.new_context("e"))

    def test_legacy_pending_drain_cannot_bypass_manual_precheck(self):
        self.ledger(settled=False)
        self.status(self.context, "cancelled")
        with self.assertRaisesRegex(ValueError, "still settling"):
            guard.check_manual_save_allowed()

    def test_legacy_settled_drain_permits_explicit_manual_baseline(self):
        self.ledger()
        self.status(self.context, "cancelled")
        guard.check_manual_save_allowed()
        guard.clear_after_manual_save()
        self.assertFalse(guard.protected())

    def test_portal_receipt_is_not_a_legacy_application_ledger(self):
        atomic_json(self.runtime / f"shutdown-graphical-drain-{self.context.operation_id}-portal.json", {"schema_version": 1})
        self.assertFalse(guard.protected())

    def test_legacy_manual_save_refuses_wrong_boot_or_pending_job(self):
        self.ledger()
        self.status(self.context, "cancelled")
        wrong = operations.OperationContext.from_dict(self.context.to_dict() | {"boot_id": "wrong-boot"})
        self.status(wrong, "cancelled")
        with self.assertRaisesRegex(ValueError, "another boot or login"):
            guard.check_manual_save_allowed()
        self.status(self.context, "cancelled")
        self.manager.inspect.return_value = {"Job": "123", "ActiveState": "deactivating"}
        self.manager.inspect.side_effect = None
        with self.assertRaisesRegex(ValueError, "stop job is still active"):
            guard.check_manual_save_allowed()

    def test_no_stop_retry_keeps_inherited_pre_drain_protection(self):
        self.arm()
        self.ledger()
        self.partial_desktop()
        new = self.new_context("e")
        self.assertTrue(guard.reuse_if_protected(new))
        descriptor = guard.seal_checkpoint(new, "f" * 32)
        self.status(new, "authorized")
        guard.arm_retry_protection({"operation_context": new.to_dict(), "invocation_id": "f" * 32, "checkpoint_bundle": descriptor})
        atomic_json(self.runtime / f"shutdown-graphical-drain-{new.operation_id}.json", {
            "schema_version": 1, "operation_context": new.to_dict(), "status": "failed", "settled": True,
            "errors": ["cancelled before stops"], "units": [], "requests": {}})
        third = self.new_context("f")
        self.assertTrue(guard.reuse_if_protected(third))
        self.assertTrue(guard._pointer().exists())

    def test_ordinary_manual_clear_without_protection_is_noop(self):
        (self.runtime / "login-generation").unlink()
        guard.clear_after_manual_save()
        self.assertFalse((self.runtime / "shutdown-drain-manual-baseline.json").exists())

    def test_changed_plan_no_stop_receipt_allows_new_fresh_save(self):
        self.arm()
        different = dict(self.proof, unit="wsctl-app-other-87654321.service",
                         control_group=f"/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice/wsctl-app-other-87654321.service")
        self.ledger(units=[different], requests={different["unit"]: "planned"})
        self.assertFalse(guard.reuse_if_protected(self.new_context("e")))

    def test_new_window_between_seal_and_arm_is_not_adopted(self):
        self.descriptor = guard.seal_checkpoint(self.context, "d" * 32)
        self.status(self.context, "authorized")
        self.shell["windows"].append({"id": 99, "pid": 200})
        with self.assertRaisesRegex(ValueError, "native windows changed"):
            guard.arm_retry_protection({"operation_context": self.context.to_dict(), "invocation_id": "d" * 32,
                                        "checkpoint_bundle": self.descriptor})
        self.assertFalse(guard._pointer().exists())

    def test_worker_seals_after_profile_intentionally_closes_viewer(self):
        session = Mock()
        session.run.side_effect = lambda: self.shell["windows"].pop()
        before = self.canonical.read_bytes(), self.tmux.read_bytes()
        with patch.object(shutdown_finalize, "_shutdown_context", return_value=("poweroff", "preflight", "a" * 16)), \
                patch.object(shutdown_finalize, "load_profiles", return_value=[]), \
                patch.object(shutdown_finalize, "load_shutdown_profile_preflight", return_value={}), \
                patch.object(shutdown_finalize, "register_shutdown_stages", return_value=True), \
                patch.object(shutdown_finalize, "ShutdownProfileSession", return_value=session), \
                patch.object(shutdown_finalize, "_save_checkpoints", return_value=False), \
                patch.object(shutdown_finalize, "consume_shutdown_cancel", return_value=False), \
                patch.object(shutdown_finalize, "set_overall"), \
                patch.object(shutdown_finalize, "update_stage"), \
                patch.object(shutdown_finalize.signal, "signal"), \
                patch.dict(os.environ, {"INVOCATION_ID": "d" * 32}):
            self.assertEqual(shutdown_finalize.run_transaction(self.context.operation_id), 0)
        completion = json.loads((self.runtime / "shutdown-worker-complete.json").read_text())
        value = guard._bundle(completion["checkpoint_bundle"])
        self.assertEqual(set(value["native"]), {"1"})
        self.assertEqual(before, (self.canonical.read_bytes(), self.tmux.read_bytes()))
        self.status(self.context, "authorized")
        guard.arm_retry_protection(completion)

    def test_busy_save_lock_refuses_seal_before_drain(self):
        fd = os.open(self.runtime, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        before = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "save is still running"):
            guard.seal_checkpoint(self.context, "d" * 32)
        self.assertLess(time.monotonic() - before, 1)

    def test_plan_gate_refuses_new_window_or_changed_unit_before_stop(self):
        self.arm()
        guard.validate_drain_plan(self.context, [self.proof])
        altered = dict(self.proof, invocation_id="f" * 32)
        with self.assertRaisesRegex(ValueError, "plan changed"):
            guard.validate_drain_plan(self.context, [altered])
        self.shell["windows"].append({"id": 99, "pid": 200})
        with self.assertRaisesRegex(ValueError, "desktop content changed"):
            guard.validate_drain_plan(self.context, [self.proof])


if __name__ == "__main__":
    unittest.main()
