"""Independent category saves, `save --auto` and snapshot time; synthetic data only."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from copy import deepcopy
from datetime import datetime
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from workspace_state import (browser_reconciliation as reconciliation, capture, checkpoint, cli,
                             operations, shutdown_checkpoint_guard, startup_control, storage)
from workspace_state.social_apps import APP_BY_ID
from workspace_state.util import data_home
from test_browser_reconciliation import fixture, observation

LOGIN = {"boot_id": "b", "login_generation": "a" * 16}
UNRESOLVED = {"session_id": None, "confidence": "unknown", "pid": 1, "start_ticks": "1",
              "tty": "/dev/pts/9"}
CATEGORIES = ("terminals", "browsers", "social_apps", "file_manager", "vscode")


def sessions(codex=None):
    pane = {"index": 0, "id": "%1", "cwd": "/synthetic", "command": "bash"}
    if codex is not None:
        pane["codex"] = dict(codex)
    return [{"name": "main", "windows": [{"index": 0, "name": "w", "layout": "even-horizontal",
                                          "panes": [pane]}]}]


def recipe(label="old", *, codex=None):
    social = {app: {"running": False, "mode": "stopped", "windows": []} for app in APP_BY_ID}
    return {
        "version": 6, "created_at": "2026-01-01T00:00:00+00:00",
        "sessions": sessions(codex),
        "terminals": [{"session": "main", "placement": {"workspace": 0}}],
        "desktop": {"shell_companion": True, "workspace_names": [label]},
        "browsers": {"google_chrome": {"available": True, "errors": [], "profiles": [{
            "profile": "Default", "windows": [{
                "id": "one", "workspace_index": 0, "monitor": 0,
                "tabs": [{"url": f"https://example.test/{label}"}]}]}]}},
        "social_apps": social,
        "file_manager": {"provider": "nemo", "desktop_id": "nemo.desktop",
                         "windows": [], "label": label},
        "vscode": {"provider": "vscode", "version": 1, "windows": [], "label": label},
    }


def terminal_capture(label="new", *, codex=None):
    value = recipe(label, codex=codex)
    return {key: value[key] for key in ("version", "created_at", "sessions", "terminals", "desktop")}


class Base(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.enter_context(patch.dict(os.environ, {
            "XDG_DATA_HOME": str(self.root / "data"), "XDG_RUNTIME_DIR": str(self.root / "runtime"),
            "XDG_CONFIG_HOME": str(self.root / "config"), "WSCTL_OPERATION_CONTEXT": ""}))
        self.out, self.err = io.StringIO(), io.StringIO()
        self.stack.enter_context(redirect_stdout(self.out))
        self.stack.enter_context(redirect_stderr(self.err))

    def patch(self, *args, **kwargs):
        return self.stack.enter_context(patch.object(*args, **kwargs))

    def seed(self, previous):
        checkpoint.record_provenance(previous, None, source="manual-save",
                                     owner=None, problems={})
        storage.save(previous)
        return storage.load()


class CaptureAllTests(Base):
    @contextmanager
    def patched(self, **overrides):
        terminals = terminal_capture()
        jobs = {"capture": lambda **_: deepcopy(terminals),
                "capture_browser": lambda **_: {"available": True, "errors": [], "profiles": []},
                "capture_social_apps": lambda *_: {},
                "capture_file_manager": lambda *_: {"windows": []},
                "capture_vscode": lambda *_: {"provider": "vscode", "version": 1, "windows": []}}
        with ExitStack() as stack:
            for name, job in jobs.items():
                override = overrides.get(name)
                stack.enter_context(patch.object(cli, name, **(override or {"side_effect": job})))
            stack.enter_context(patch("workspace_state.checkpoint.capture_shell",
                                      return_value={"available": True}))
            stack.enter_context(patch("workspace_state.checkpoint.workspace_names", return_value=[]))
            yield

    def evidence(self, snapshot, name):
        return snapshot["capture_context"]["provider_evidence"][name]

    def test_terminals_failure_is_recorded_and_other_categories_survive(self):
        with self.patched(capture={"side_effect": ValueError("tmux down")}):
            snapshot = cli._capture_all()
        self.assertEqual(snapshot["capture_errors"]["terminals"], ["tmux down"])
        self.assertEqual(snapshot["sessions"], [])
        for key in ("browsers", "social_apps", "file_manager", "vscode"):
            self.assertIn(key, snapshot)
        self.assertEqual(self.evidence(snapshot, "terminals")["state"], "failed")

    def test_browsers_failure_is_recorded_without_browser_state(self):
        with self.patched(capture_browser={"side_effect": RuntimeError("no chrome")}):
            snapshot = cli._capture_all()
        self.assertEqual(snapshot["capture_errors"]["browsers"], ["no chrome"])
        self.assertNotIn("browsers", snapshot)
        self.assertEqual(self.evidence(snapshot, "browsers")["state"], "failed")
        self.assertNotIn("content_digest", self.evidence(snapshot, "browsers"))

    def test_social_apps_failure_leaves_other_categories(self):
        with self.patched(capture_social_apps={"side_effect": RuntimeError("apps down")}):
            snapshot = cli._capture_all()
        self.assertEqual(snapshot["capture_errors"]["social_apps"], ["apps down"])
        self.assertNotIn("social_apps", snapshot)
        for key in ("sessions", "browsers", "file_manager", "vscode"):
            self.assertIn(key, snapshot)

    def test_browsers_disabled_never_calls_the_browser_capture(self):
        with self.patched(capture_browser={"side_effect": AssertionError("must not run")}) as _:
            snapshot = cli._capture_all(browsers=False)
        self.assertNotIn("browsers", snapshot)
        self.assertNotIn("browsers", snapshot["capture_context"]["provider_evidence"])
        self.assertIn("terminals", snapshot["capture_context"]["provider_evidence"])


class ManualSaveTests(Base):
    def setUp(self):
        super().setUp()
        self.previous = self.seed(recipe("old"))
        self.protected = self.patch(shutdown_checkpoint_guard, "protected", return_value=False)
        self.patch(cli, "_checkpoint_login", return_value=dict(LOGIN))
        self.patch(shutdown_checkpoint_guard, "check_manual_save_allowed")
        self.clear = self.patch(shutdown_checkpoint_guard, "clear_after_manual_save")

    def save(self, captured, *, allow_partial=False):
        with patch.object(cli, "_capture_all", side_effect=lambda: deepcopy(captured)):
            code = cli.cmd_save(argparse.Namespace(allow_partial=allow_partial, shutdown_safe=False,
                                                   verify_idle_codex=False))
        return code, storage.load()

    def failed_terminals(self):
        captured = recipe("new")
        captured.update(sessions=[], terminals=[], desktop={},
                        capture_errors={"terminals": ["tmux down"]})
        return captured

    def assertTerminalsKept(self, saved):
        for key in ("sessions", "terminals", "desktop"):
            self.assertEqual(saved[key], self.previous[key])

    def test_terminal_failure_keeps_previous_terminals_and_saves_the_rest(self):
        code, saved = self.save(self.failed_terminals())
        self.assertEqual(code, 3)
        self.assertTerminalsKept(saved)
        self.assertEqual(saved["file_manager"], recipe("new")["file_manager"])
        self.assertEqual(saved["category_provenance"]["terminals"]["state"], "retained")

    def test_allow_partial_never_accepts_a_failed_terminal_capture(self):
        code, saved = self.save(self.failed_terminals(), allow_partial=True)
        self.assertEqual(code, 0)
        self.assertTerminalsKept(saved)
        self.assertEqual(saved["category_provenance"]["terminals"]["state"], "retained")

    def test_unresolved_conversation_is_published_only_with_allow_partial(self):
        captured = recipe("new", codex=UNRESOLVED)
        code, saved = self.save(captured, allow_partial=True)
        self.assertEqual(code, 0)
        self.assertEqual(saved["sessions"], captured["sessions"])
        self.assertEqual(saved["category_provenance"]["terminals"]["state"], "failed")

    def test_unresolved_conversation_keeps_previous_sessions_without_allow_partial(self):
        code, saved = self.save(recipe("new", codex=UNRESOLVED))
        self.assertEqual(code, 3)
        self.assertEqual(saved["sessions"], self.previous["sessions"])
        self.assertIn("--allow-partial", self.err.getvalue())

    def test_social_apps_failure_keeps_previous_social_apps(self):
        captured = recipe("new")
        del captured["social_apps"]
        captured["capture_errors"] = {"social_apps": ["apps down"]}
        code, saved = self.save(captured)
        self.assertEqual(code, 3)
        self.assertEqual(saved["social_apps"], self.previous["social_apps"])
        self.assertIn("retained the last-good social-apps checkpoint", self.err.getvalue())

    def test_unsafe_vscode_failure_keeps_previous_vscode(self):
        captured = recipe("new")
        captured["capture_errors"] = {"vscode": ["unsaved edits"], "vscode_unsafe": True}
        code, saved = self.save(captured)
        self.assertEqual(code, 3)
        self.assertEqual(saved["vscode"], self.previous["vscode"])

    def test_only_complete_categories_get_an_adoption_record(self):
        captured = recipe("new")
        del captured["social_apps"]
        captured["capture_errors"] = {"social_apps": ["apps down"]}
        _, saved = self.save(captured)
        records = saved["category_provenance"]
        self.assertEqual(records["social-apps"]["state"], "retained")
        self.assertNotIn("adoption", records["social-apps"])
        for category in ("terminals", "browsers", "file-manager", "vscode"):
            self.assertIn("adoption", records[category], category)

    def assertResumed(self, resumed):
        # Only a complete baseline ends the protection of a cancelled shutdown.
        self.assertEqual(self.clear.called, resumed)

    def test_kept_terminals_never_end_shutdown_protection(self):
        code, _ = self.save(recipe("new", codex=UNRESOLVED))
        self.assertEqual(code, 3)
        self.assertResumed(False)
        self.assertNotIn("paused", self.err.getvalue())

    def test_kept_application_category_never_ends_protection_even_with_allow_partial(self):
        captured = recipe("new")
        captured["capture_errors"] = {"vscode": ["bridge down"]}
        code, saved = self.save(captured, allow_partial=True)
        self.assertEqual(code, 0)
        self.assertEqual(saved["vscode"], self.previous["vscode"])
        self.assertResumed(False)

    def test_kept_browsers_alone_end_protection(self):
        captured = recipe("new")
        captured["browsers"]["google_chrome"]["available"] = False
        code, saved = self.save(captured)
        self.assertEqual(code, 3)
        self.assertEqual(saved["browsers"], self.previous["browsers"])
        self.assertResumed(True)

    def test_accepted_partial_terminals_end_protection(self):
        code, _ = self.save(recipe("new", codex=UNRESOLVED), allow_partial=True)
        self.assertEqual(code, 0)
        self.assertResumed(True)

    def test_partial_save_never_replaces_a_protected_shutdown_checkpoint(self):
        self.protected.return_value = True
        before = storage.path_for().read_bytes()
        with self.assertRaisesRegex(RuntimeError, "stays protected until a save captures terminals"):
            self.save(recipe("new", codex=UNRESOLVED))
        self.assertEqual(storage.path_for().read_bytes(), before)
        self.assertResumed(False)

    def test_complete_save_still_replaces_a_protected_shutdown_checkpoint(self):
        self.protected.return_value = True
        code, saved = self.save(recipe("new"))
        self.assertEqual(code, 0)
        self.assertEqual(saved["file_manager"], recipe("new")["file_manager"])
        self.assertResumed(True)

    def test_complete_save_ends_protection(self):
        code, _ = self.save(recipe("new"))
        self.assertEqual(code, 0)
        self.assertResumed(True)


class ShutdownSafeSaveTests(Base):
    """The shutdown transaction keeps its rules until milestone 5."""

    def setUp(self):
        super().setUp()
        self.previous = self.seed(recipe("old"))
        self.patch(cli, "_shutdown_allows_unresolved_codex", return_value=True)
        self.patch(cli, "_checkpoint_login", return_value=dict(LOGIN))
        self.patch(startup_control, "check_shutdown_checkpoint")
        self.stage = self.patch(cli, "update_stage")

    def save(self, captured):
        with patch.object(cli, "_capture_all", side_effect=lambda: deepcopy(captured)):
            code = cli.cmd_save(argparse.Namespace(allow_partial=True, shutdown_safe=True,
                                                   verify_idle_codex=False))
        return code, storage.load()

    def stages(self):
        return [call.args[:2] for call in self.stage.call_args_list]

    def without_apps(self, label):
        value = recipe(label)
        del value["social_apps"]
        value["capture_errors"] = {"social_apps": ["apps down"]}
        return value

    def test_desktop_app_failure_keeps_last_good_apps_and_degrades(self):
        code, saved = self.save(self.without_apps("new"))
        self.assertEqual(code, 3)
        self.assertEqual(saved["social_apps"], self.previous["social_apps"])
        self.assertIn(("social-apps-save", "degraded"), self.stages())

    def test_desktop_app_failure_without_last_good_apps_fails_closed(self):
        previous = recipe("old")
        del previous["social_apps"]
        self.seed(previous)
        before = storage.path_for().read_bytes()
        with self.assertRaisesRegex(RuntimeError, "No last-good desktop app checkpoint"):
            self.save(self.without_apps("new"))
        self.assertEqual(storage.path_for().read_bytes(), before)
        self.assertIn(("social-apps-save", "failed"), self.stages())

    def test_chrome_capture_exception_keeps_last_good_browsers_and_degrades(self):
        captured = recipe("new")
        del captured["browsers"]
        captured["capture_errors"] = {"browsers": ["no chrome"]}
        code, saved = self.save(captured)
        self.assertEqual(code, 3)
        self.assertEqual(saved["browsers"], self.previous["browsers"])
        self.assertEqual(saved["category_provenance"]["browsers"]["state"], "retained")


class AutoSaveTests(Base):
    def setUp(self):
        super().setUp()
        self.previous = self.seed(recipe("old"))
        self.protected = self.patch(shutdown_checkpoint_guard, "protected", return_value=False)
        self.shutdown = self.patch(cli, "_shutdown_operation_active", return_value=False)
        self.login = self.patch(cli, "_checkpoint_login", return_value=dict(LOGIN))
        self.check = self.patch(startup_control, "check_shutdown_checkpoint")
        self.verify = self.patch(checkpoint, "verify_capture_context")
        self.clear = self.patch(shutdown_checkpoint_guard, "clear_after_manual_save")
        self.capture = self.patch(cli, "_capture_all")
        self.capture.side_effect = lambda **_: self.next_capture()
        self.captured = recipe("new")
        del self.captured["browsers"]

    def next_capture(self):
        return deepcopy(self.captured)

    def run_auto(self, **flags):
        args = argparse.Namespace(auto=True, allow_partial=False, shutdown_safe=False,
                                  verify_idle_codex=False, **flags)
        self.out.seek(0), self.out.truncate(), self.err.seek(0), self.err.truncate()
        code = cli.cmd_save(args)
        self.clear.assert_not_called()
        return code

    def raw(self):
        return storage.path_for().read_bytes()

    def assertSkipped(self):
        before = self.raw()
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint skipped", self.err.getvalue())
        self.assertEqual(self.raw(), before)

    def history(self):
        directory = data_home() / "recovery" / "history"
        return sorted(directory.glob("*.json")) if directory.exists() else []

    def test_runs_without_any_startup_marker(self):
        # Unrestored items stay pending, so no restore marker gates a checkpoint.
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint saved", self.out.getvalue())

    def test_skip_when_shutdown_guard_is_protected(self):
        self.protected.return_value = True
        self.assertSkipped()

    def test_skip_during_shutdown_operation(self):
        self.shutdown.return_value = True
        self.assertSkipped()

    def test_skip_without_previous_checkpoint_creates_nothing(self):
        storage.path_for().unlink()
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint skipped", self.err.getvalue())
        self.assertFalse(storage.path_for().exists())

    def test_skip_on_fallback_monitor(self):
        previous = recipe("old")
        previous["desktop"]["monitors"] = [{"connector": "DP-1", "identity": {
            "vendor": "AAA", "product": "1", "serial": "2"}}]
        self.previous = self.seed(previous)
        self.captured["desktop"]["monitors"] = [{"connector": "unknown"}]
        self.assertSkipped()

    def test_skip_when_capture_reports_topology_change(self):
        self.capture.side_effect = checkpoint.TopologyChanged("topology changed")
        self.assertSkipped()

    def test_skip_when_context_verification_reports_topology_change(self):
        self.verify.side_effect = checkpoint.TopologyChanged("topology changed")
        self.assertSkipped()

    def test_skip_when_login_changes_during_capture(self):
        self.login.side_effect = [dict(LOGIN), {**LOGIN, "login_generation": "c" * 16}]
        self.assertSkipped()

    def test_cancelled_startup_check_no_longer_applies(self):
        self.check.side_effect = AssertionError("must not run")
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint saved", self.out.getvalue())
        self.check.assert_not_called()

    def test_shutdown_operation_detection(self):
        path = Path(os.environ["XDG_RUNTIME_DIR"]) / "workspace-state" / "current-operation.json"
        path.parent.mkdir(parents=True)

        def write(*, mode="shutdown", boot=None, deadline=None):
            context = operations.OperationContext(
                boot or operations.boot_id(), "a" * 16, "b" * 32, mode, 1,
                time.monotonic() + 100 if deadline is None else deadline)
            path.write_text(json.dumps({"schema_version": 1,
                                        "operation_context": context.to_dict()}))

        real = lambda: type(self)._real_active()
        self.assertFalse(real())
        write()
        self.assertTrue(real())
        write(deadline=time.monotonic() - 1)
        self.assertFalse(real())
        write(mode="startup")
        self.assertFalse(real())
        write(boot="another-boot")
        self.assertFalse(real())
        path.write_text("{corrupt")
        self.assertTrue(real())
        path.unlink()
        self.assertFalse(real())

    def test_without_chrome_capture_previous_browsers_are_kept(self):
        terminals = terminal_capture("new")
        shell = {"available": True, "monitors": [], "workspaces": []}
        self.capture.side_effect = type(self)._real_capture
        with patch.object(cli, "capture", side_effect=lambda **_: deepcopy(terminals)), \
                patch.object(cli, "capture_browser", side_effect=AssertionError("no chrome")), \
                patch.object(cli, "capture_social_apps", return_value=recipe("new")["social_apps"]), \
                patch.object(cli, "capture_file_manager", return_value=recipe("new")["file_manager"]), \
                patch.object(cli, "capture_vscode", return_value=recipe("new")["vscode"]), \
                patch("workspace_state.checkpoint.capture_shell", return_value=shell), \
                patch("workspace_state.checkpoint.workspace_names", return_value=[]):
            self.assertEqual(self.run_auto(), 0)
        saved = storage.load()
        self.assertEqual(saved["browsers"], self.previous["browsers"])
        self.assertEqual(saved["category_provenance"]["browsers"],
                         self.previous["category_provenance"]["browsers"])
        self.assertNotIn("browsers", saved["capture_context"]["provider_evidence"])

    def test_unchanged_capture_is_not_republished(self):
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint saved", self.out.getvalue())
        before, generations = self.raw(), self.history()
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint unchanged", self.out.getvalue())
        self.assertEqual(self.raw(), before)
        self.assertEqual(self.history(), generations)

    def test_changed_capture_publishes_auto_save_provenance_without_adoption(self):
        self.assertEqual(self.run_auto(), 0)
        saved = storage.load()
        self.assertEqual(saved["file_manager"], self.captured["file_manager"])
        for category in ("terminals", "social-apps", "file-manager", "vscode"):
            record = saved["category_provenance"][category]
            self.assertEqual(record["source"], "auto-save", category)
            self.assertNotIn("adoption", record)

    def test_unresolved_conversation_is_saved_as_an_unresolved_pane(self):
        self.captured = recipe("new", codex=UNRESOLVED)
        del self.captured["browsers"]
        self.assertEqual(self.run_auto(), 0)
        saved = storage.load()
        self.assertEqual(saved["sessions"], self.captured["sessions"])
        self.assertEqual(saved["file_manager"], self.captured["file_manager"])
        self.assertEqual(saved["category_provenance"]["terminals"]["state"], "failed")
        self.assertNotIn("pending", saved)
        self.assertIn("Partial state", self.err.getvalue())

    def test_other_terminal_gap_keeps_the_whole_terminal_recipe(self):
        self.captured["desktop"]["shell_companion"] = False
        self.assertEqual(self.run_auto(), 0)
        saved = storage.load()
        for key in ("sessions", "terminals", "desktop"):
            self.assertEqual(saved[key], self.previous[key])
        self.assertEqual(saved["category_provenance"]["terminals"]["state"], "retained")

    def test_empty_capture_keeps_previous_sessions(self):
        self.captured["sessions"], self.captured["terminals"] = [], []
        self.assertEqual(self.run_auto(), 0)
        saved = storage.load()
        self.assertEqual(saved["sessions"], self.previous["sessions"])
        self.assertEqual(saved["terminals"], self.previous["terminals"])

    def witness_previous(self, *, valid):
        snapshot, _native = fixture()
        snapshot.update(version=5, sessions=[], terminals=[], desktop={"shell_companion": True})
        if not valid:
            observation(snapshot)["browser_state"]["profiles"][0]["windows"][0]["tabs"][0].update(
                url="https://example.test/tampered")
        storage.save(snapshot)
        return storage.load()

    def auto_witness(self):
        # The context `_capture_all(browsers=False)` builds: no browser evidence,
        # so it can never vouch for the carried browser observation by itself.
        self.captured = {"version": 6, "created_at": "2026-02-01T00:00:00+00:00", "sessions": [],
                         "terminals": [], "desktop": {"shell_companion": True},
                         "file_manager": recipe("new")["file_manager"],
                         "capture_context": {
                             "schema_version": 2, "topology_signature": "synthetic",
                             "capture_id": "6f1c2a4e-8d3b-4c5a-9e7f-0a1b2c3d4e5f",
                             "captured_at": "2026-02-01T00:00:00.100000+00:00",
                             "completed_at": "2026-02-01T00:00:00.900000+00:00",
                             "snapshot_created_at": "2026-02-01T00:00:00+00:00",
                             "provider_evidence": {
                                 name: {"state": "captured", "detail": "provider capture completed"}
                                 for name in ("terminals", "social_apps", "file_manager", "vscode")},
                         }}
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("checkpoint saved", self.out.getvalue())
        return storage.load()

    def test_valid_browser_witness_is_carried_forward(self):
        previous = self.witness_previous(valid=True)
        expected = reconciliation.retained_observation(previous)
        saved = self.auto_witness()
        self.assertIn(reconciliation.WITNESS_KEY, saved)
        self.assertEqual(saved["browsers"], previous["browsers"])
        self.assertEqual(reconciliation.retained_observation(saved), expected)

    def test_unvalidated_browser_observation_gets_no_witness(self):
        self.witness_previous(valid=False)
        saved = self.auto_witness()
        self.assertNotIn(reconciliation.WITNESS_KEY, saved)
        with self.assertRaises(reconciliation.ReconciliationRequired):
            reconciliation.retained_observation(saved)

    def test_skip_when_shutdown_starts_during_capture(self):
        self.shutdown.side_effect = [False, True]
        self.assertSkipped()
        self.assertIn("started during capture", self.err.getvalue())

    def test_skip_while_shutdown_recovery_forbids_saving(self):
        self.patch(shutdown_checkpoint_guard, "check_manual_save_allowed", side_effect=ValueError(
            "shutdown is still active; wait for cancellation/recovery before saving"))
        self.assertSkipped()
        self.capture.assert_not_called()

    def test_skip_when_another_save_holds_the_lock(self):
        # Only publication takes the state lock; the capture runs regardless.
        with storage.state_lock():
            self.assertSkipped()
        self.capture.assert_called_once()
        self.assertIn("another workspace save is running", self.err.getvalue())

    def test_skip_when_another_capture_runs(self):
        with storage.capture_lock():
            self.assertSkipped()
        self.capture.assert_not_called()
        self.assertIn("another workspace capture is running", self.err.getvalue())

    def test_skip_while_the_login_restore_runs(self):
        import fcntl
        with open(cli._startup_directory() / "restore.lock", "a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertSkipped()
        self.capture.assert_not_called()
        self.assertIn("login restore is running", self.err.getvalue())

    def test_skip_when_a_save_publishes_during_capture(self):
        other = recipe("other")
        checkpoint.record_provenance(other, None, source="manual-save", owner=None, problems={})

        def capture_while_saving(**_):
            storage.save(deepcopy(other))
            return self.next_capture()

        self.capture.side_effect = capture_while_saving
        self.assertEqual(self.run_auto(), 0)
        self.assertIn("changed during capture", self.err.getvalue())
        self.assertEqual(storage.load()["desktop"], other["desktop"])

    def test_terminal_only_checkpoint_keeps_the_other_categories(self):
        self.out.seek(0), self.out.truncate()
        code, current = cli._auto_checkpoint(("terminals",))
        self.assertEqual((code, current), (0, True))
        self.assertEqual(self.capture.call_args.kwargs, {"browsers": False, "only": ("terminals",)})
        saved = storage.load()
        self.assertEqual(saved["desktop"], self.captured["desktop"])
        for category, field in (("file-manager", "file_manager"), ("vscode", "vscode"),
                                ("social-apps", "social_apps")):
            self.assertEqual(saved[field], self.previous[field], category)
            self.assertEqual(saved["category_provenance"][category],
                             self.previous["category_provenance"][category], category)

    def test_same_login_adoption_is_never_extended(self):
        previous = recipe("old")
        checkpoint.record_provenance(previous, None, source="manual-save", owner=dict(LOGIN),
                                     problems={})
        storage.save(previous)
        self.assertIn("adoption", storage.load()["category_provenance"]["file-manager"])
        self.assertEqual(self.run_auto(), 0)
        records = storage.load()["category_provenance"]
        for category in ("terminals", "file-manager", "vscode"):
            self.assertEqual(records[category]["source"], "auto-save", category)
            self.assertNotIn("adoption", records[category], category)
        # Unchanged content keeps the acceptance it already had.
        self.assertIn("adoption", records["social-apps"])

    def test_auto_rejects_incompatible_flags(self):
        for flag in ("allow_partial", "shutdown_safe", "verify_idle_codex"):
            with self.subTest(flag=flag):
                args = argparse.Namespace(auto=True, allow_partial=False, shutdown_safe=False,
                                          verify_idle_codex=False)
                setattr(args, flag, True)
                with self.assertRaises(RuntimeError):
                    cli.cmd_save(args)
        self.capture.assert_not_called()

    def test_parser_accepts_auto(self):
        self.assertIs(cli.parser().parse_args(["save", "--auto"]).auto, True)


# Keep references to the real functions before setUp patches them on the module.
AutoSaveTests._real_active = staticmethod(cli._shutdown_operation_active)
AutoSaveTests._real_capture = staticmethod(cli._capture_all)


class TimeTests(unittest.TestCase):
    def test_snapshot_time_is_utc_with_second_precision(self):
        stamp = datetime.fromisoformat(capture.snapshot_time())
        self.assertEqual(stamp.utcoffset().total_seconds(), 0)
        self.assertEqual(stamp.microsecond, 0)

    def test_local_time(self):
        value = "2026-01-01T00:00:00+00:00"
        self.assertEqual(cli._local_time(value),
                         datetime.fromisoformat(value).astimezone().isoformat(timespec="seconds"))
        self.assertEqual(cli._local_time("old"), "old")
        self.assertEqual(cli._local_time(None), "")


if __name__ == "__main__":
    unittest.main()
