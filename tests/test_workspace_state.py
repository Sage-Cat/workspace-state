from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli as workspace_state_cli, operations
from workspace_state.cli import main as cli_main
from workspace_state.browser import (
    BROWSER_REQUIRED_CAPABILITIES,
    BrowserUnavailable, BrowserPlacementPending,
    BrowserRestoreResult,
    _attach_desktop_placements,
    _identify_native_window,
    _place_browser_window,
    browser_window_placement,
    ensure_browser_profiles,
    profile_socket_path,
    restore_browser,
    wait_for_browser_settle,
)
from workspace_state.cli import (
    _autosave_from_tmux,
    _browser_problems,
    _browser_restore_token_prefix,
    _close_startup_browser_duplicates,
    _unconfigure_tmux_file,
    _publish_workspace_restored,
    _restore_browsers,
    _restore_items,
    _restore_terminals,
    _select,
    _session_workspace,
    _startup_directory,
    _wait_for_shell,
    _workspace_groups,
    cmd_save,
    cmd_startup,
    cmd_tmux_save,
    parser,
)
from workspace_state.capture import ROLLOUT_RE, _parse_proc_stat, _session_from_open_files
from workspace_state.desktop import (
    capture_shell,
    desktop_readiness,
    desktop_topology_signature,
    expect_window,
    place_by_title,
    remap_monitor,
    remap_workspace,
)
from workspace_state.native_host import (
    _acquire_profile_lock,
    _prepare_listener,
    _resolved_profile,
    _socket_identity,
    _unlink_owned_socket,
    decode_native_messages,
    encode_native_message,
    serve,
)
from workspace_state.restore import (
    _pane_shell_command,
    _same_tmux_session,
    _tmux_exists,
    launch_terminal,
    place_terminal,
)
from workspace_state.storage import load, path_for, save
from workspace_state.util import launch_graphical_service

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class GraphicalServiceTests(unittest.TestCase):
    @patch("workspace_state.util.subprocess.run")
    def test_gui_process_tree_outlives_the_finite_restore_worker(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "", "")

        unit = launch_graphical_service(["/usr/bin/example", "--flag"], "example gui")

        invocation = run.call_args.args[0]
        self.assertRegex(unit, r"^wsctl-app-example-gui-[0-9a-f]{8}\.service$")
        self.assertIn("--property=ExitType=cgroup", invocation)
        self.assertIn("--property=PartOf=graphical-session.target", invocation)
        self.assertIn("--property=KillMode=mixed", invocation)
        self.assertIn("--property=After=graphical-session.target org.gnome.Shell@wayland.service", invocation)
        self.assertTrue(any(value.startswith("--property=ExecStop=/usr/bin/python3 -I ")
                            and value.endswith(f'graphical_stop.py" {unit}') for value in invocation))
        self.assertEqual(invocation[-3:], ["--", "/usr/bin/example", "--flag"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"XDG_DATA_HOME": self.temp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_round_trip_uses_one_canonical_state(self):
        snapshot = {"name": "ignored", "created_at": "2026-08-04T20:00:00+03:00", "sessions": []}
        save(snapshot)
        self.assertEqual(load()["name"], "current")
        self.assertEqual(path_for().name, "current.json")

    def test_snapshot_permissions_are_private(self):
        path = save({"name": "private", "sessions": []})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_replacement_keeps_a_private_last_good_copy(self):
        save({"created_at": "first", "sessions": []})
        save({"created_at": "second", "sessions": []})
        backup = Path(self.temp.name) / "workspace-state/recovery/current.last-good.json"
        self.assertEqual(json.loads(backup.read_text())["created_at"], "first")
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)

    def test_load_rejects_tmux_window_that_cannot_be_restored(self):
        malformed = {
            "sessions": [{"name": "work", "windows": [{
                "index": 1, "name": "broken", "layout": "layout", "panes": [],
            }]}],
            "terminals": [{"session": "work"}],
        }
        with self.assertRaisesRegex(ValueError, "invalid window"):
            save(malformed)

    def test_load_rejects_malformed_browser_structure(self):
        malformed = {
            "sessions": [],
            "terminals": [],
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default", "windows": [{"tabs": "not-a-list"}],
            }]}},
        }
        with self.assertRaisesRegex(ValueError, "invalid window"):
            save(malformed)

    def test_partial_snapshot_allows_unresolved_codex_identity(self):
        partial = {
            "sessions": [{"name": "work", "windows": [{
                "index": 1,
                "name": "codex",
                "layout": "layout",
                "panes": [{
                    "index": 1,
                    "cwd": "/tmp",
                    "command": "codex",
                    "codex": {"session_id": None, "confidence": "unknown"},
                }],
            }]}],
            "terminals": [{"session": "work"}],
        }
        save(partial)
        self.assertIsNone(load()["sessions"][0]["windows"][0]["panes"][0]["codex"]["session_id"])

class GroupingTests(unittest.TestCase):
    def test_workspace_name_from_placement(self):
        session = {"placement": {"workspace": 2}}
        self.assertEqual(_session_workspace(session, ["Life", "Work", "PhD"]), "PhD")

    def test_unassigned_without_companion(self):
        self.assertEqual(_session_workspace({"placement": None}, ["Life"]), "Unassigned")

    def test_groups_terminal_counts_by_workspace(self):
        snapshot = {
            "version": 2,
            "desktop": {"workspace_names": ["Life", "Work"]},
            "terminals": [
                {"session": "a", "placement": {"workspace": 0}},
                {"session": "b", "placement": {"workspace": 0}},
                {"session": "c", "placement": {"workspace": 1}},
            ],
            "sessions": [
                {"name": "a", "windows": [{"panes": [{"codex": {"session_id": "one"}}]}]},
                {"name": "b", "windows": [{"panes": [{"codex": {"session_id": "two"}}]}]},
                {"name": "c", "windows": [{"panes": [{"codex": {"session_id": "three"}}]}]},
            ],
        }
        groups = _workspace_groups(snapshot)
        self.assertEqual(len(groups["Life"]["terminals"]), 2)
        self.assertEqual(len(groups["Life"]["session_names"]), 2)
        self.assertEqual(len(groups["Life"]["codex_ids"]), 2)
        self.assertEqual(len(groups["Work"]["terminals"]), 1)

    def test_restore_keeps_two_terminals_for_same_tmux_session(self):
        snapshot = {
            "version": 2,
            "terminals": [
                {"session": "a", "placement": {"workspace": 0}},
                {"session": "a", "placement": {"workspace": 1}},
            ],
            "sessions": [{"name": "a", "windows": []}],
        }
        items = _restore_items(snapshot)
        self.assertEqual([item["placement"]["workspace"] for item in items], [0, 1])

    def test_restore_adds_saved_workspace_name_to_legacy_placement(self):
        snapshot = {
            "version": 2,
            "desktop": {"workspace_names": ["Life", "Work"]},
            "terminals": [{"session": "a", "placement": {"workspace": 1}}],
            "sessions": [{"name": "a", "windows": []}],
        }
        self.assertEqual(_restore_items(snapshot)[0]["placement"]["workspace_name"], "Work")

    def test_detached_tmux_session_is_not_marked_for_terminal_launch(self):
        snapshot = {
            "terminals": [{"session": "attached", "placement": None}],
            "sessions": [
                {"name": "attached", "windows": []},
                {"name": "detached", "windows": []},
            ],
        }
        items = {item["name"]: item for item in _restore_items(snapshot)}
        self.assertTrue(items["attached"]["launch_terminal"])
        self.assertFalse(items["detached"]["launch_terminal"])

    def test_select_refuses_noninteractive_fallback(self):
        with (
            patch("workspace_state.cli.shutil.which", return_value="/usr/bin/fzf"),
            patch("workspace_state.cli.sys.stdin.isatty", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "interactive terminal"):
                _select([{"name": "a", "placement": None}], ["Life"])

    def test_partial_save_keeps_the_last_good_terminal_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
                old = {"name": "current", "created_at": "old", "desktop": {"shell_companion": True},
                       "terminals": [], "sessions": [{"name": "work", "windows": []}]}
                save(old)
                partial = {
                    "name": "current",
                    "created_at": "new",
                    "desktop": {"shell_companion": False},
                    "terminals": [],
                    "sessions": [],
                }
                errors = io.StringIO()
                with patch("workspace_state.cli._capture_all", return_value=partial), \
                     redirect_stdout(io.StringIO()), redirect_stderr(errors):
                    self.assertEqual(cmd_save(Namespace(allow_partial=False)), 3)
                saved = load()
                self.assertEqual(saved["sessions"], old["sessions"])
                self.assertEqual(saved["desktop"], old["desktop"])
                self.assertEqual(saved["category_provenance"]["terminals"]["state"], "retained")
                self.assertIn("retained the last-good terminals checkpoint", errors.getvalue())

    def test_cli_has_no_snapshot_name_parameters(self):
        self.assertEqual(parser().parse_args(["save"]).command, "save")
        self.assertIsNone(parser().parse_args(["restore"]).category)
        self.assertEqual(parser().parse_args(["restore", "terminals"]).category, "terminals")
        self.assertEqual(parser().parse_args(["restore", "browsers"]).category, "browsers")
        with patch("sys.stderr"):
            with self.assertRaises(SystemExit):
                parser().parse_args(["save", "evening"])

    def test_groups_chrome_windows_and_tabs_by_workspace(self):
        snapshot = {
            "desktop": {"workspace_names": ["Life", "Work"]},
            "sessions": [],
            "terminals": [],
            "chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "window-1", "workspace": "Work", "tabs": [{}, {}]}],
            }]},
        }
        groups = _workspace_groups(snapshot)
        self.assertEqual(len(groups["Work"]["chrome_windows"]), 1)
        self.assertEqual(groups["Work"]["chrome_tabs"], 2)

    def test_partial_browser_profile_capture_is_rejected(self):
        previous = {
            "sessions": [],
            "browsers": {"google_chrome": {"profiles": [
                {"profile": "Default", "windows": []},
                {"profile": "Work", "windows": []},
            ]}},
        }
        captured = {
            "sessions": [],
            "browsers": {"google_chrome": {
                "available": True,
                "profiles": [{"profile": "Default", "windows": []}],
            }},
        }
        self.assertIn("Work", "; ".join(_browser_problems(captured, previous)))

    def test_autosave_rejects_an_empty_tmux_regression(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_DATA_HOME": directory}, clear=False,
        ):
            save({
                "created_at": "protected",
                "sessions": [{"name": "work", "windows": []}],
                "terminals": [],
            })
            degraded = {
                "desktop": {"shell_companion": True},
                "capture_errors": {"tmux": []},
                "sessions": [],
                "terminals": [],
            }
            with (
                patch("workspace_state.cli.capture", return_value=degraded),
                patch("workspace_state.cli.capture_browser", return_value={"available": False, "profiles": []}),
            ):
                path, problems = _autosave_from_tmux()
            self.assertIsNone(path)
            self.assertTrue(any("no sessions" in problem for problem in problems))
            self.assertEqual(load()["created_at"], "protected")

    def test_terminal_autosave_preserves_the_prior_browser_recipe(self):
        prior_browser = {
            "available": True,
            "profiles": [{
                "profile": "Default",
                "windows": [{"id": "saved-browser", "tabs": [{"url": "https://example.test"}]}],
            }],
        }
        previous = {
            "created_at": "protected",
            "desktop": {"shell_companion": True},
            "sessions": [{"name": "work", "windows": []}],
            "terminals": [],
            "browsers": {"google_chrome": prior_browser},
        }
        captured = {
            "created_at": "terminal-autosave",
            "desktop": {"shell_companion": True},
            "capture_errors": {"tmux": []},
            "sessions": [{"name": "work", "windows": []}],
            "terminals": [],
        }
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_DATA_HOME": directory}, clear=False,
        ):
            save(previous)
            with patch("workspace_state.cli.capture", return_value=captured), patch(
                "workspace_state.cli.capture_browser",
            ) as capture_chrome:
                path, problems = _autosave_from_tmux()

            self.assertEqual(problems, [])
            self.assertIsNotNone(path)
            self.assertEqual(
                load()["browsers"]["google_chrome"],
                prior_browser,
            )
            capture_chrome.assert_not_called()

    def test_startup_has_no_resurrect_wait_and_leaves_tmux_to_wsctl(self):
        args = Namespace(
            category="terminals", workspace=None, session=None, select=False,
            dry_run=False, no_place=False, force=False, wait=0,
            verify_codex=True,
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), patch(
            "workspace_state.cli._wait_for_shell",
        ), patch(
            "workspace_state.cli._startup_workspace_names", return_value=set(),
        ), patch("workspace_state.cli.load", return_value={"sessions": []}), patch(
            "workspace_state.cli._restore", return_value={"terminals": 1, "browsers": 0},
        ) as restore, patch("workspace_state.cli.update_stage") as update:
            self.assertEqual(cmd_startup(args), 0)
        self.assertFalse(hasattr(workspace_state_cli, "_wait_for_tmux_restore"))
        for retired in ("repair_processes", "adopt_restored"):
            self.assertFalse(hasattr(restore.call_args.args[1], retired))
        tmux_stages = [call.args[:2] for call in update.call_args_list if call.args[0] == "tmux"]
        self.assertEqual(tmux_stages, [("tmux", "skipped")])
        self.assertNotIn("resurrect", repr(update.call_args_list).lower())

    def test_category_failure_stays_visible_without_blocking_cloud_handoff(self):
        args = Namespace(
            category="vscode", workspace=None, session=None, select=False,
            dry_run=False, no_place=False, force=False, wait=0,
            verify_codex=True,
        )
        snapshot = {"created_at": "saved", "sessions": []}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), patch("workspace_state.cli._wait_for_shell"), patch(
            "workspace_state.cli._startup_workspace_names", return_value=set(),
        ), patch("workspace_state.cli.load", return_value=snapshot), patch(
            "workspace_state.cli._restore", side_effect=RuntimeError("display missing"),
        ), patch(
            "workspace_state.cli._publish_workspace_restored",
        ) as publish, patch("workspace_state.cli.update_stage") as update:
            self.assertEqual(cmd_startup(args), 0)  # the failed category is a result

            marker = (
                Path(directory)
                / "workspace-state/startup-test-boot/vscode.done"
            )
            self.assertTrue(marker.is_file())
        publish.assert_called_once_with()
        self.assertIn(
            ("vscode", "failed", "display missing"),
            [call.args[:3] for call in update.call_args_list],
        )

    def test_startup_journal_is_scoped_to_the_current_login(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"):
            runtime = Path(directory) / "workspace-state"
            runtime.mkdir()
            (runtime / "login-generation").write_text("0123456789abcdef\n")

            self.assertEqual(
                _startup_directory(),
                runtime / "startup-test-boot-0123456789abcdef",
            )

    def test_workspace_target_waits_for_every_restore_category(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), patch(
            "workspace_state.cli.subprocess.run",
        ) as run:
            root = Path(directory) / "workspace-state/startup-test-boot"
            root.mkdir(parents=True)
            (root / "terminals.done").write_text("done\n")

            _publish_workspace_restored()

        run.assert_not_called()

    def test_workspace_target_starts_after_every_restore_category(self):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), patch(
            "workspace_state.cli.subprocess.run", return_value=completed,
        ) as run:
            root = Path(directory) / "workspace-state/startup-test-boot"
            root.mkdir(parents=True)
            for category in ("terminals", "browsers", "social-apps", "file-manager", "vscode", "virtual-machines"):
                (root / f"{category}.done").write_text("done\n")

            _publish_workspace_restored()

        run.assert_called_once_with(
            [
                "/usr/bin/systemctl", "--user", "start", "--no-block",
                "wsctl-workspace-restored.target",
            ],
            check=False,
            capture_output=True,
            text=True,
        )


class MonitorIdentityTests(unittest.TestCase):
    def test_capture_shell_uses_gnome_winctl_json_contract(self):
        payload = {
            "capabilities": ["list_windows", "place_window"],
            "monitors": [{"index": 0, "identity": {"serial": "ABC"}}],
            "workspaces": [{"index": 0, "name": "Life"}],
            "windows": [],
        }
        with patch("workspace_state.desktop.run", return_value=json.dumps(payload)) as command:
            result = capture_shell()
        self.assertTrue(result["available"])
        self.assertEqual(result["monitors"][0]["identity"]["serial"], "ABC")
        command.assert_called_once_with(["gnome-winctl", "state", "--json"])

    def test_desktop_readiness_waits_for_physical_display_recovery(self):
        shell = {
            "available": True,
            "capabilities": ["list_monitors", "list_workspaces"],
            "monitors": [{"index": 0}],
            "workspaces": [{"index": 0, "name": "Life"}],
            "monitor_policy": {
                "display_identity_ready": True,
                "display_identity_cache_valid": True,
                "display_identity_refreshing": False,
                "display_identity_retry_pending": False,
                "recovery_active": True,
                "screen_unavailable": False,
            },
        }
        ready, reason = desktop_readiness(shell, {"list_monitors", "list_workspaces"})
        self.assertFalse(ready)
        self.assertIn("recovery", reason)
        shell["monitor_policy"]["recovery_active"] = False
        self.assertTrue(desktop_readiness(shell, {"list_monitors", "list_workspaces"})[0])

    def test_startup_wait_resets_when_display_topology_changes(self):
        def state(connector, recovery=False):
            return {
                "available": True,
                "capabilities": ["list_monitors", "list_workspaces"],
                "monitors": [{"index": 0, "connector": connector}],
                "workspaces": [{"index": 0, "name": "Life"}],
                "monitor_policy": {
                    "display_identity_ready": True,
                    "display_identity_cache_valid": True,
                    "display_identity_refreshing": False,
                    "display_identity_retry_pending": False,
                    "recovery_active": recovery,
                    "screen_unavailable": False,
                },
            }

        samples = [state("DP-1", True), state("DP-1"), state("HDMI-1"), state("HDMI-1")]
        with patch("workspace_state.cli.capture_shell", side_effect=samples), patch(
            "workspace_state.cli.time.monotonic", side_effect=[0.0, 0.0, 1.0, 2.0, 4.0]
        ), patch("workspace_state.cli.time.sleep"):
            _wait_for_shell(
                10,
                {"list_monitors", "list_workspaces"},
                {"Life"},
                stable_for=1,
            )

    def test_topology_signature_includes_workspace_names(self):
        first = {
            "monitors": [{"index": 0, "connector": "DP-1"}],
            "workspaces": [{"index": 0, "name": "Life"}],
        }
        second = {**first, "workspaces": [{"index": 0, "name": "Work"}]}
        self.assertNotEqual(
            desktop_topology_signature(first),
            desktop_topology_signature(second),
        )
    def test_remaps_geometry_to_same_serial(self):
        placement = {
            "monitor": 2,
            "monitor_identity": {"serial": "ABC"},
            "monitor_geometry": {"x": 5000, "y": 1000},
            "geometry": {"x": 5100, "y": 1050, "width": 1000, "height": 700},
        }
        current = {
            "monitors": [{
                "index": 0, "x": 2000, "y": 0,
                "identity": {"serial": "ABC"},
            }],
        }
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            result = remap_monitor(placement)
        self.assertEqual(result["monitor"], 0)
        self.assertEqual(result["monitor_intent"]["serial"], "ABC")
        self.assertEqual((result["geometry"]["x"], result["geometry"]["y"]), (2100, 50))

    def test_workspace_name_survives_reordering(self):
        placement = {"workspace": 1, "workspace_name": "Work"}
        with patch("workspace_state.desktop.workspace_names", return_value=["Work", "Life"]):
            self.assertEqual(remap_workspace(placement)["workspace"], 0)

    def test_absent_saved_monitor_falls_back_to_primary(self):
        placement = {
            "monitor": 8,
            "monitor_identity": {"edid_hash": "absent"},
            "geometry": {"x": 10, "y": 10, "width": 800, "height": 600},
        }
        current = {"monitors": [
            {"index": 0, "x": 0, "y": 0, "primary": False, "identity": {}},
            {"index": 1, "x": 1920, "y": 0, "primary": True, "identity": {}},
        ]}
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            result = remap_monitor(placement)
        self.assertEqual(result["monitor"], 1)
        self.assertEqual(result["monitor_identity"], {"edid_hash": "absent"})
        self.assertEqual(result["monitor_intent"], {"edid_hash": "absent"})

    def test_exact_monitor_restore_refuses_a_missing_physical_display(self):
        placement = {
            "monitor": 8,
            "monitor_identity": {"edid_hash": "absent", "serial": "DISPLAY-1"},
        }
        current = {"monitors": [{
            "index": 0,
            "primary": True,
            "identity": {"edid_hash": "different", "serial": "DISPLAY-2"},
        }]}
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            with self.assertRaisesRegex(RuntimeError, "not connected"):
                remap_monitor(placement, require_identity=True)

    def test_ambiguous_saved_monitor_falls_back_without_rewriting_intent(self):
        placement = {
            "monitor": 8,
            "monitor_identity": {"edid_hash": "same", "connector": "missing"},
        }
        current = {"monitors": [
            {
                "index": 0, "primary": False,
                "identity": {"edid_hash": "same", "connector": "DP-1"},
            },
            {
                "index": 1, "primary": True,
                "identity": {"edid_hash": "same", "connector": "DP-2"},
            },
        ]}
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            result = remap_monitor(placement)
        self.assertEqual(result["monitor"], 1)
        self.assertEqual(result["monitor_intent"]["connector"], "missing")

    def test_edid_hash_is_preferred_over_connector(self):
        placement = {"monitor": 0, "monitor_identity": {"connector": "DP-1", "edid_hash": "same"}}
        current = {"monitors": [
            {"index": 0, "identity": {"connector": "DP-1", "edid_hash": "different"}},
            {"index": 1, "identity": {"connector": "HDMI-1", "edid_hash": "same"}},
        ]}
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            self.assertEqual(remap_monitor(placement)["monitor"], 1)

    def test_title_placement_uses_gnome_winctl_selector(self):
        placement = {"state": "fullscreen", "geometry": {}}
        with patch("workspace_state.desktop._place", return_value=True) as place:
            self.assertTrue(place_by_title("restore-me", placement))
        place.assert_called_once_with({"title": "restore-me"}, placement)

    def test_place_and_expect_forward_monitor_intent(self):
        placement = {
            "monitor": 0,
            "monitor_intent": {"serial": "missing-display"},
            "geometry": {},
        }
        with patch("workspace_state.desktop._winctl", return_value={"placed": True}) as call:
            self.assertTrue(place_by_title("restore-me", placement))
        arguments = call.call_args.args[0]
        target = json.loads(arguments[arguments.index("--target-json") + 1])
        self.assertEqual(target["monitor_intent"]["serial"], "missing-display")

        with patch(
            "workspace_state.desktop._winctl", return_value={"token": "expect-one"},
        ) as call:
            self.assertEqual(
                expect_window(
                    "google-chrome",
                    placement,
                    title="wsctl-create:unique - Google Chrome",
                ),
                "expect-one",
            )
        arguments = call.call_args.args[0]
        selector = json.loads(arguments[arguments.index("--selector-json") + 1])
        self.assertEqual(selector, {
            "app_id": "google-chrome",
            "title": "wsctl-create:unique - Google Chrome",
        })
        target = json.loads(arguments[arguments.index("--target-json") + 1])
        self.assertEqual(target["monitor_intent"]["serial"], "missing-display")


class BrowserTests(unittest.TestCase):
    companion_info = {
        "protocol_version": 2,
        "capabilities": sorted(BROWSER_REQUIRED_CAPABILITIES),
    }

    def test_matches_chrome_api_window_to_shell_placement(self):
        profiles = [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [{
                "id": "window-1", "runtime_window_id": 7,
                "bounds": {"left": 100, "top": 50, "width": 1200, "height": 800},
                "state": "normal",
                "tabs": [],
            }],
        }]
        shell = {"windows": [{
            "id": 42, "wm_class": "Google-chrome",
            "workspace": 1,
            "monitor": 2,
            "monitor_identity": {
                "connector": "DP-1", "edid_hash": "abc", "edid_checksum": "sum",
            },
            "monitor_geometry": {"index": 2, "x": 0, "y": 0, "width": 1920, "height": 1080},
            "geometry": {"x": 102, "y": 52, "width": 1200, "height": 800},
            "state": "maximized",
        }]}
        with patch("workspace_state.browser._identify_native_window", return_value=(42, {})), patch("workspace_state.browser.request_browser", return_value={"released": True}):
            _attach_desktop_placements(profiles, shell, ["Life", "Research"])
        window = profiles[0]["windows"][0]
        self.assertEqual(window["workspace"], "Research")
        self.assertEqual(window["monitor"]["connector"], "DP-1")
        self.assertEqual(window["state"], "maximized")
        placement = browser_window_placement(window)
        self.assertEqual(placement["monitor_identity"]["edid_hash"], "abc")
        self.assertEqual(placement["monitor_identity"]["edid_checksum"], "sum")

    def test_restore_arms_and_finishes_each_window_sequentially(self):
        chrome = {"profiles": [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [
                {
                    "id": "window-1", "workspace": "Work", "workspace_index": 1,
                    "monitor": {"index": 0, "geometry": {}}, "geometry": {},
                    "state": "normal", "tabs": [{}],
                },
                {
                    "id": "window-2", "workspace": "Work", "workspace_index": 1,
                    "monitor": {"index": 0, "geometry": {}}, "geometry": {},
                    "state": "normal", "tabs": [{}, {}],
                },
            ],
        }]}
        events = []

        creation_titles = []

        def fake_expect(_app_id, _placement, *, title):
            expectation = f"expect-{len([event for event in events if event.startswith('expect')]) + 1}"
            events.append(expectation)
            creation_titles.append(title)
            return expectation

        def fake_request(_action, payload, *, profile, timeout=60):
            events.append(f"request-{payload['window']['id']}-{profile}")
            return {
                "window_id": int(payload["window"]["id"].split("-")[-1]),
                "warnings": [], "urls_restored": True,
            }

        def fake_status(expectation):
            events.append(f"status-{expectation}")
            return "placed"

        with (
            patch("workspace_state.browser.remap_monitor", side_effect=lambda value: value),
            patch("workspace_state.browser.remap_workspace", side_effect=lambda value: value),
            patch("workspace_state.browser.expect_window", side_effect=fake_expect),
            patch("workspace_state.browser.request_browser", side_effect=fake_request),
            patch("workspace_state.browser.expected_window_status", side_effect=fake_status),
            patch("workspace_state.browser._place_browser_window", return_value=True),
        ):
            actions = restore_browser(chrome)
        self.assertEqual(events, [
            "expect-1", "request-window-1-Default", "status-expect-1",
            "expect-2", "request-window-2-Default", "status-expect-2",
        ])
        self.assertEqual(len(set(creation_titles)), 2)
        self.assertTrue(all(title.startswith("wsctl-create:") for title in creation_titles))
        self.assertEqual(len(actions), 2)

    def test_restore_reuses_open_window_without_waiting_for_new_window(self):
        chrome = {"profiles": [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [{
                "id": "window-1", "workspace": "Work", "workspace_index": 1,
                "monitor": {"index": 0, "geometry": {}}, "geometry": {},
                "state": "normal", "tabs": [{}, {}],
            }],
        }]}
        with (
            patch("workspace_state.browser.remap_monitor", side_effect=lambda value: value),
            patch("workspace_state.browser.remap_workspace", side_effect=lambda value: value),
            patch("workspace_state.browser.expect_window", return_value="expect-1"),
            patch(
                "workspace_state.browser.request_browser",
                return_value={"window_id": 42, "warnings": [], "reused": True, "urls_restored": True},
            ),
            patch("workspace_state.browser.cancel_expected_window") as cancel,
            patch("workspace_state.browser.expected_window_status") as status,
            patch("workspace_state.browser._place_browser_window", return_value=True) as place,
        ):
            actions = restore_browser(chrome, restore_token_prefix="snapshot")
        cancel.assert_called_once_with("expect-1")
        status.assert_not_called()
        self.assertEqual(place.call_args.kwargs["chrome_window_id"], 42)
        self.assertEqual(place.call_args.kwargs["profile"], "Default")
        self.assertEqual(actions, [
            BrowserRestoreResult("reused open Chrome Default/window-1 (2 tabs)"),
        ])

    def test_failed_placement_never_closes_a_chrome_restored_window(self):
        chrome = {"profiles": [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [{
                "id": "window-1", "workspace": "Work", "workspace_index": 1,
                "monitor": {"index": 0, "geometry": {}}, "geometry": {},
                "state": "normal", "tabs": [{}],
            }],
        }]}
        requests = []

        def request(action, payload, *, profile, timeout=60):
            requests.append(action)
            if action == "restore_window":
                return {
                    "window_id": 42,
                    "warnings": [], "urls_restored": True,
                    "reused": True,
                    "created": False,
                }
            self.fail(f"unexpected action: {action}")

        with (
            patch("workspace_state.browser.remap_monitor", side_effect=lambda value: value),
            patch("workspace_state.browser.remap_workspace", side_effect=lambda value: value),
            patch("workspace_state.browser.expect_window", return_value="expect-1"),
            patch("workspace_state.browser.cancel_expected_window"),
            patch("workspace_state.browser.request_browser", side_effect=request),
            patch("workspace_state.browser._place_browser_window", return_value=False),
        ):
            actions = restore_browser(chrome, restore_token_prefix="snapshot")

        self.assertEqual(requests, ["restore_window"])
        self.assertFalse(actions[0].success)

    def test_failed_placement_closes_only_a_wsctl_created_window(self):
        chrome = {"profiles": [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [{
                "id": "window-1", "workspace": "Work", "workspace_index": 1,
                "monitor": {"index": 0, "geometry": {}}, "geometry": {},
                "state": "normal", "tabs": [{}],
            }],
        }]}
        requests = []

        def request(action, payload, *, profile, timeout=60):
            requests.append((action, payload))
            if action == "restore_window":
                return {"window_id": 43, "warnings": [], "created": True, "urls_restored": True}
            if action == "close_restored_window":
                return {"closed": True}
            self.fail(f"unexpected action: {action}")

        with (
            patch("workspace_state.browser.remap_monitor", side_effect=lambda value: value),
            patch("workspace_state.browser.remap_workspace", side_effect=lambda value: value),
            patch("workspace_state.browser.expect_window", return_value="expect-1"),
            patch("workspace_state.browser.expected_window_status", return_value="placed"),
            patch("workspace_state.browser.request_browser", side_effect=request),
            patch("workspace_state.browser._place_browser_window", return_value=False),
        ):
            actions = restore_browser(chrome, restore_token_prefix="snapshot")

        self.assertEqual([action for action, _payload in requests], [
            "restore_window", "close_restored_window",
        ])
        self.assertTrue(requests[-1][1]["created"])
        self.assertFalse(actions[0].success)

    def test_focus_marker_maps_one_chrome_window_to_stable_gnome_id(self):
        identification = {
            "window_id": 42,
            "marker_tab_id": 99,
            "previous_active_tab_id": 11,
            "token": "ignored-extension-token",
        }

        def request(action, payload=None, *, profile, timeout=60):
            self.assertEqual(profile, "Default")
            if action == "identify_window":
                identification["token"] = payload["token"]
                return identification
            self.fail(f"unexpected action: {action}")

        def shell():
            return {
                "windows": [{
                    "id": 7,
                    "app_ids": ["google-chrome"],
                    "title": f"wsctl-identify:{identification['token']} - Google Chrome",
                    "active": True,
                }],
            }

        with patch("workspace_state.browser.request_browser", side_effect=request), patch(
            "workspace_state.browser.capture_shell", side_effect=lambda: shell()
        ), patch("workspace_state.browser.time.sleep"):
            native_id, result = _identify_native_window(
                profile="Default",
                chrome_window_id=42,
                app_id="google-chrome",
            )
        self.assertEqual(native_id, 7)
        self.assertEqual(result["marker_tab_id"], 99)

    def test_unique_marker_maps_a_window_already_on_an_inactive_workspace(self):
        identification = {
            "window_id": 42,
            "marker_tab_id": 99,
            "previous_active_tab_id": 11,
            "token": "ignored-extension-token",
        }

        def request(action, payload=None, *, profile, timeout=60):
            if action == "identify_window":
                identification["token"] = payload["token"]
                return identification
            self.fail(f"unexpected action: {action}")

        def shell():
            return {
                "windows": [{
                    "id": 17,
                    "app_ids": ["google-chrome"],
                    "title": f"wsctl-identify:{identification['token']} - Google Chrome",
                    "active": False,
                    "workspace": 4,
                }],
            }

        with patch("workspace_state.browser.request_browser", side_effect=request), patch(
            "workspace_state.browser.capture_shell", side_effect=lambda: shell()
        ), patch("workspace_state.browser.time.sleep"):
            native_id, _result = _identify_native_window(
                profile="Default",
                chrome_window_id=42,
                app_id="google-chrome",
            )

        self.assertEqual(native_id, 17)

    def test_popup_identification_never_creates_a_marker_tab(self):
        requests = []

        def request(action, payload=None, *, profile, timeout=60):
            requests.append((action, payload))
            if action == "focus_window":
                return {
                    "id": 42,
                    "active_title": "Private document",
                    "bounds": {"left": 0, "top": 0, "width": 960, "height": 730},
                }
            self.fail(f"popup identification must not request {action}")

        shell = {
            "windows": [{
                "id": 17,
                "app_ids": ["google-chrome"],
                "title": "Private document - Google Chrome",
                "active": True,
                "geometry": {"x": 1970, "y": 94, "width": 940, "height": 707},
            }],
        }
        with patch(
            "workspace_state.browser.request_browser", side_effect=request,
        ), patch(
            "workspace_state.browser.capture_shell", return_value=shell,
        ), patch("workspace_state.browser.time.sleep"):
            native_id, identification = _identify_native_window(
                profile="Default",
                chrome_window_id=42,
                app_id="google-chrome",
                window_type="popup",
            )

        self.assertEqual(native_id, 17)
        self.assertIsNone(identification["marker_tab_id"])
        self.assertEqual([action for action, _payload in requests], ["focus_window"])

    def test_verified_native_receipt_does_not_refocus_window(self):
        identification = {
            "window_id": 42,
            "marker_tab_id": 99,
            "previous_active_tab_id": 11,
            "token": "marker",
        }
        target = {
            "workspace": 2,
            "monitor": 1,
            "state": "maximized",
            "geometry": {"x": 0, "y": 0, "width": 1920, "height": 1080},
        }
        placement_result = {
            "status": "verified",
            "handoff_owned": True,
            "resolved_target": target,
        }
        requests = []

        def request(action, payload=None, *, profile, timeout=60):
            requests.append((action, payload))
            return {"released": True}

        with patch(
            "workspace_state.browser._identify_native_window",
            return_value=(7, identification),
        ), patch(
            "workspace_state.browser.move_window_result",
            return_value=placement_result,
        ), patch("workspace_state.browser.request_browser", side_effect=request), patch(
            "workspace_state.browser.capture_shell",
            return_value={"capabilities": ["staged_placement_handoff"], "windows": [{
                "id": 7, "workspace": 2, "monitor": 1,
                "state": "maximized", "geometry_relative": target["geometry"],
            }]},
        ):
            self.assertTrue(_place_browser_window(
                profile="Default",
                chrome_window_id=42,
                app_id="google-chrome",
                placement=target,
            ))
        self.assertEqual([action for action, _payload in requests], [
            "release_window_identification",
        ])
        self.assertFalse(requests[0][1]["focus"])

    def test_inactive_workspace_deferred_placement_remains_waiting(self):
        identification = {
            "window_id": 42,
            "marker_tab_id": 99,
            "previous_active_tab_id": 11,
            "token": "marker",
        }
        target = {
            "workspace": 4,
            "monitor": 0,
            "state": "maximized",
            "geometry": {"x": 0, "y": 44, "width": 3840, "height": 2030},
        }
        placement_result = {
            "status": "deferred",
            "token": "owned", "handoff_owned": True,
            "resolved_target": target,
        }
        with patch(
            "workspace_state.browser._identify_native_window",
            return_value=(7, identification),
        ), patch(
            "workspace_state.browser.move_window_result",
            return_value=placement_result,
        ), patch(
            "workspace_state.browser.request_browser",
            return_value={"released": True},
        ), patch("workspace_state.browser.capture_shell", return_value={"capabilities": ["staged_placement_handoff"], "windows": []}):
            with self.assertRaises(BrowserPlacementPending):
                _place_browser_window(
                profile="Default",
                chrome_window_id=42,
                app_id="google-chrome",
                placement=target,
                timeout=0,
                )


    def test_browser_settle_reads_live_windows_before_wsctl_restores_missing_ones(self):
        with patch(
            "workspace_state.browser.request_browser", return_value=[{"id": 42, "signature": "one"}],
        ) as request:
            wait_for_browser_settle({"Default"}, timeout=0, stable_for=0)
        request.assert_called_once_with("list_windows", {}, profile="Default", timeout=2)

    def test_manual_browser_restore_uses_snapshot_window_token(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(workspace=None, dry_run=False, no_place=True)
        with (
            patch("workspace_state.cli.connected_profiles", return_value=["Default"]),
            patch("workspace_state.cli.browser_companion_info", return_value={
                "protocol_version": 2,
                "capabilities": sorted(BROWSER_REQUIRED_CAPABILITIES),
            }),
            patch(
                "workspace_state.cli.restore_browser",
                return_value=[BrowserRestoreResult("reused")],
            ) as restore,
        ):
            self.assertEqual(_restore_browsers(snapshot, args), 1)
        self.assertEqual(
            restore.call_args.kwargs["restore_token_prefix"],
            _browser_restore_token_prefix(snapshot),
        )

    def test_browser_restore_token_survives_unrelated_checkpoint_changes(self):
        recipe = {"profiles": [{"profile": "Default", "windows": [{
            "id": "one", "tabs": [{"url": "https://example.com/"}],
            "placement": {"workspace": 1, "monitor": 0},
        }]}]}
        snapshot = {"created_at": "before", "browsers": {"google_chrome": recipe}}
        updated = copy.deepcopy(snapshot)
        updated.update(created_at="after", sessions=[{"name": "new-terminal-session"}],
                       terminals=[{"session": "new-terminal-session"}], vscode={"windows": []})
        self.assertEqual(_browser_restore_token_prefix(snapshot), _browser_restore_token_prefix(updated))
        # Dictionary serialization order and the legacy category wrapper do
        # not change the browser recipe's identity either.
        reordered = {"profiles": [{"windows": [{
            "placement": {"monitor": 0, "workspace": 1},
            "tabs": [{"url": "https://example.com/"}], "id": "one",
        }], "profile": "Default"}]}
        self.assertEqual(_browser_restore_token_prefix(snapshot), _browser_restore_token_prefix({"chrome": reordered}))

    def test_browser_restore_token_changes_with_saved_urls_or_placement(self):
        snapshot = {"created_at": "same", "browsers": {"google_chrome": {"profiles": [{
            "profile": "Default", "windows": [{
                "id": "one", "tabs": [{"url": "https://example.com/"}],
                "placement": {"workspace": 1, "monitor": 0},
            }],
        }]}}}
        original = _browser_restore_token_prefix(snapshot)
        for field in ("url", "workspace", "monitor"):
            with self.subTest(field=field):
                changed = copy.deepcopy(snapshot)
                window = changed["browsers"]["google_chrome"]["profiles"][0]["windows"][0]
                if field == "url":
                    window["tabs"][0]["url"] = "https://example.com/changed"
                else:
                    window["placement"][field] += 1
                self.assertNotEqual(original, _browser_restore_token_prefix(changed))

    def test_startup_launches_supported_browser_and_waits_for_profile(self):
        chrome = {"profiles": [{"profile": "Default", "app_id": "google-chrome"}]}
        with (
            patch("workspace_state.browser.connected_profiles", side_effect=[[], ["Default"]]),
            patch("workspace_state.browser.shutil.which", return_value="/usr/bin/google-chrome"),
            patch("workspace_state.browser.launch_graphical_service") as launch,
            patch("workspace_state.browser.time.sleep"),
        ):
            self.assertEqual(ensure_browser_profiles(chrome), ["google-chrome (Default)"])
        self.assertEqual(launch.call_args.args[0], [
            "/usr/bin/google-chrome",
            "--profile-directory=Default",
            "--restore-last-session",
            "--hide-crash-restore-bubble",
        ])
        self.assertEqual(launch.call_args.args[1], "chrome-Default")

    def test_startup_launches_each_saved_chrome_profile_directory(self):
        chrome = {"profiles": [
            {"profile": "Personal", "profile_directory": "Default", "app_id": "google-chrome"},
            {"profile": "Work", "profile_directory": "Profile 1", "app_id": "google-chrome"},
        ]}
        with (
            patch("workspace_state.browser.connected_profiles", side_effect=[[], ["Personal", "Work"]]),
            patch("workspace_state.browser.shutil.which", return_value="/usr/bin/google-chrome"),
            patch("workspace_state.browser.launch_graphical_service") as launch,
            patch("workspace_state.browser.time.sleep"),
        ):
            ensure_browser_profiles(chrome)
        self.assertEqual(launch.call_count, 2)
        commands = [call.args[0] for call in launch.call_args_list]
        self.assertIn("--profile-directory=Default", commands[0])
        self.assertIn("--profile-directory=Profile 1", commands[1])

    def test_startup_browser_journal_verifies_live_window_token(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(workspace=None, dry_run=False, no_place=False, force=True)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), (
            patch("workspace_state.cli.ensure_browser_profiles", return_value=[])
        ), patch("workspace_state.cli.connected_profiles", return_value=["Default"]), (
            patch("workspace_state.cli.browser_companion_info", return_value=self.companion_info)
        ), patch("workspace_state.cli.wait_for_browser_settle"), (
            patch("workspace_state.cli.request_browser", return_value={"exists": False})
        ) as status, patch(
            "workspace_state.cli.restore_browser",
            return_value=[BrowserRestoreResult("restored")],
        ) as restore:
            self.assertEqual(_restore_browsers(snapshot, args, start_browser=True), 1)
        self.assertEqual(status.call_args.args[:2], (
            "restore_status",
            {"restore_token": f"{_browser_restore_token_prefix(snapshot)}:Default:one",
             "window": {"id": "one", "tabs": []}},
        ))
        restore.assert_called_once()

    def test_startup_browser_journal_reuses_only_a_live_window(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(workspace=None, dry_run=False, no_place=False, force=False)
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            token_prefix = _browser_restore_token_prefix(snapshot)
            item_key = hashlib.sha256(
                f"{token_prefix}\0Default\0one".encode(),
            ).hexdigest()
            item_marker = (
                Path(directory) / "workspace-state/startup-test-boot/browser-items"
                / f"{item_key}.done"
            )
            item_marker.parent.mkdir(parents=True)
            from workspace_state.provider_results import EvidenceState, PhaseEvidence, ProviderItemResult
            verified = PhaseEvidence(EvidenceState.VERIFIED)
            evidence = ProviderItemResult("chrome", "Default/one", verified, verified, verified)
            write_stage_marker(item_marker, StageMarker("browsers", "ready", "saved",
                                                       provider_results=(evidence.to_dict(),)))
            # A terminal autosave updates the checkpoint timestamp but keeps
            # the browser recipe; the retry must reuse this existing journal.
            snapshot.update(created_at="after-terminal-autosave", sessions=[{"name": "updated"}])
            with patch("workspace_state.cli._boot_id", return_value="test-boot"), (
                patch("workspace_state.cli.ensure_browser_profiles", return_value=[])
            ), patch("workspace_state.cli.connected_profiles", return_value=["Default"]), (
                patch("workspace_state.cli.browser_companion_info", return_value=self.companion_info)
            ), patch("workspace_state.cli.wait_for_browser_settle"), (
                patch("workspace_state.cli.request_browser", return_value={"exists": True, "window_id": 42, "urls_restored": True})
            ), patch("workspace_state.cli.restore_browser") as restore:
                self.assertEqual(_restore_browsers(snapshot, args, start_browser=True), 1)
        restore.assert_not_called()

    def test_startup_closes_only_unclaimed_windows_from_native_restore(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(
            workspace=None, dry_run=False, no_place=False, force=True, wait=0,
        )
        token = f"{_browser_restore_token_prefix(snapshot)}:Default:one"
        status_calls = 0
        list_calls = 0

        def request(action, payload, *, profile, timeout=60):
            nonlocal list_calls, status_calls
            self.assertEqual(profile, "Default")
            if action == "list_windows":
                # 10 and 11 came from Chrome's native restore. Window 12
                # appeared later and is outside the frozen cleanup set.
                list_calls += 1
                return (
                    [{"id": i, "full_signature": "exact", "urls_loaded": True} for i in (10, 11)]
                    if list_calls == 1
                    else [{"id": i, "full_signature": "exact", "urls_loaded": True} for i in (10, 11, 12)]
                )
            if action == "restore_status":
                self.assertEqual(payload["restore_token"], token)
                status_calls += 1
                return (
                    {"exists": False}
                    if status_calls == 1
                    else {"exists": True, "window_id": 11, "urls_restored": True}
                )
            if action == "close_restored_window":
                self.assertEqual(payload, {"window_id": 10, "created": True, "expected_full_signature": "exact"})
                return {"closed": True}
            self.fail(f"unexpected browser action: {action}")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), (
            patch(
                "workspace_state.cli.ensure_browser_profiles",
                return_value=["google-chrome (Default)"],
            )
        ), patch("workspace_state.cli.connected_profiles", side_effect=[[], ["Default"]]), (
            patch("workspace_state.cli.browser_companion_info", return_value=self.companion_info)
        ), patch("workspace_state.cli.wait_for_browser_settle"), (
            patch("workspace_state.cli.request_browser", side_effect=request)
        ) as native, patch(
            "workspace_state.cli.restore_browser",
            return_value=[BrowserRestoreResult("restored")],
        ):
            self.assertEqual(_restore_browsers(snapshot, args, start_browser=True), 1)

        close_calls = [
            call for call in native.call_args_list
            if call.args[0] == "close_restored_window"
        ]
        self.assertEqual(len(close_calls), 1)

    def test_duplicate_cleanup_requires_one_distinct_keeper_per_saved_window(self):
        def request(action, payload, *, profile, timeout=60):
            self.assertEqual(profile, "Default")
            if action == "restore_status":
                return {"exists": True, "window_id": 11, "urls_restored": True}
            self.fail(f"unexpected browser action: {action}")

        with patch("workspace_state.cli.request_browser", side_effect=request) as native:
            with self.assertRaisesRegex(BrowserUnavailable, "one distinct keeper"):
                _close_startup_browser_duplicates(
                    {"Default": {10, 11}},
                    {"Default": ["saved:one", "saved:two"]},
                )
        self.assertNotIn(
            "close_restored_window",
            [call.args[0] for call in native.call_args_list],
        )

    def test_startup_refuses_outdated_browser_companion_before_restore(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(workspace=None, dry_run=False, no_place=False, force=False)
        with patch("workspace_state.cli.ensure_browser_profiles", return_value=[]), (
            patch("workspace_state.cli.connected_profiles", return_value=["Default"])
        ), patch(
            "workspace_state.cli.browser_companion_info",
            return_value={"protocol_version": 1, "capabilities": ["list_windows"]},
        ), patch("workspace_state.cli.restore_browser") as restore:
            with self.assertRaisesRegex(BrowserUnavailable, "outdated"):
                _restore_browsers(snapshot, args, start_browser=True)
        restore.assert_not_called()

    def test_startup_retries_live_but_uncommitted_browser_window(self):
        snapshot = {
            "created_at": "saved",
            "browsers": {"google_chrome": {"profiles": [{
                "profile": "Default",
                "windows": [{"id": "one", "tabs": []}],
            }]}},
        }
        args = Namespace(workspace=None, dry_run=False, no_place=False, force=False)

        def request(action, _payload, *, profile):
            self.assertEqual(profile, "Default")
            if action == "restore_status":
                return {"exists": True, "window_id": 42, "urls_restored": True}
            self.fail(f"unexpected browser action: {action}")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"), (
            patch("workspace_state.cli.ensure_browser_profiles", return_value=[])
        ), patch("workspace_state.cli.connected_profiles", return_value=["Default"]), (
            patch("workspace_state.cli.browser_companion_info", return_value=self.companion_info)
        ), patch("workspace_state.cli.wait_for_browser_settle"), (
            patch("workspace_state.cli.request_browser", side_effect=request)
        ) as native, patch(
            "workspace_state.cli.restore_browser",
            return_value=[BrowserRestoreResult("restored")],
        ) as restore:
            self.assertEqual(_restore_browsers(snapshot, args, start_browser=True), 1)
        self.assertEqual([call.args[0] for call in native.call_args_list], [
            "restore_status",
        ])
        restore.assert_called_once()


class NativeMessagingTests(unittest.TestCase):
    def test_profile_lock_allows_only_one_native_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            first = _acquire_profile_lock("Default")
            try:
                with self.assertRaisesRegex(RuntimeError, "already connected"):
                    _acquire_profile_lock("Default")
            finally:
                os.close(first)
            replacement = _acquire_profile_lock("Default")
            os.close(replacement)

    def test_listener_replaces_an_unresponsive_stale_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            path = Path(directory) / "workspace-state" / "chrome-21b111cbfe6e8fca.sock"
            path.parent.mkdir()
            stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stale.bind(str(path))
            stale.listen()
            stale_identity = _socket_identity(stale)
            listener, actual_path, identity = _prepare_listener("Default")
            try:
                self.assertEqual(actual_path, path)
                self.assertNotEqual(stale_identity, identity)
            finally:
                listener.close()
                _unlink_owned_socket(actual_path, identity)
                stale.close()

    def test_listener_preserves_a_responsive_host(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            path = Path(directory) / "workspace-state" / "chrome-21b111cbfe6e8fca.sock"
            path.parent.mkdir()
            existing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            existing.bind(str(path))
            existing.listen()

            def answer_ping():
                connection, _address = existing.accept()
                with connection:
                    connection.recv(65536)
                    connection.sendall(b'{"ok":true,"result":{"profile":"Default"}}\n')

            worker = threading.Thread(target=answer_ping)
            worker.start()
            try:
                with self.assertRaisesRegex(RuntimeError, "already connected"):
                    _prepare_listener("Default")
            finally:
                worker.join()
                existing.close()
                path.unlink()

    def test_old_host_cleanup_cannot_unlink_a_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.sock"
            old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old.bind(str(path))
            old_identity = _socket_identity(old)
            path.unlink()
            replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            replacement.bind(str(path))
            try:
                _unlink_owned_socket(path, old_identity)
                self.assertTrue(path.exists())
            finally:
                replacement.close()
                path.unlink()
                old.close()

    def test_native_host_resolves_unconfigured_profile_from_signed_in_email(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config/google-chrome"
            config.mkdir(parents=True)
            (config / "Local State").write_text(json.dumps({
                "profile": {"info_cache": {
                    "Default": {"user_name": "personal@example.com"},
                    "Profile 1": {"user_name": "work@example.com"},
                }},
            }))
            with patch("workspace_state.native_host.Path.home", return_value=home):
                self.assertEqual(_resolved_profile({
                    "profile": "Default",
                    "profileDirectory": "Default",
                    "profileEmail": "work@example.com",
                    "profileConfigured": False,
                }), ("Profile 1", "Profile 1"))

    def test_decodes_fragmented_and_consecutive_messages(self):
        first = encode_native_message({"hello": "world"})
        second = encode_native_message({"value": 2})
        buffer = bytearray(first[:3])
        self.assertEqual(decode_native_messages(buffer), [])
        buffer.extend(first[3:] + second)
        self.assertEqual(decode_native_messages(buffer), [{"hello": "world"}, {"value": 2}])
        self.assertEqual(buffer, bytearray())

    def test_manifest_key_matches_allowed_extension_origin(self):
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / "chrome-extension/manifest.json").read_text())
        public_key = base64.b64decode(manifest["key"])
        prefix = hashlib.sha256(public_key).digest()[:16].hex()
        extension_id = prefix.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
        host_template = (root / "native-messaging/org.sagecat.workspace_state.json.in").read_text()
        self.assertIn(f"chrome-extension://{extension_id}/", host_template)

    def test_host_forwards_cli_request_and_extension_response(self):
        def read_native(stream):
            header = stream.read(4)
            self.assertEqual(len(header), 4)
            length = struct.unpack("=I", header)[0]
            return json.loads(stream.read(length))

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ):
            native_read, extension_write = os.pipe()
            extension_read, native_write = os.pipe()
            native_input = os.fdopen(native_read, "rb", buffering=0)
            native_output = os.fdopen(native_write, "wb", buffering=0)
            extension_input = os.fdopen(extension_read, "rb", buffering=0)
            extension_output = os.fdopen(extension_write, "wb", buffering=0)
            results = []
            thread = threading.Thread(
                target=lambda: results.append(serve(native_input, native_output)),
                daemon=True,
            )
            thread.start()
            extension_output.write(encode_native_message({"type": "hello", "profile": "Default"}))
            self.assertTrue(read_native(extension_input)["ok"])

            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.settimeout(2)
            client.connect(str(profile_socket_path("Default")))
            client.sendall(b'{"action":"ping","payload":{}}\n')
            forwarded = read_native(extension_input)
            self.assertEqual(forwarded["action"], "ping")
            extension_output.write(encode_native_message({
                "id": forwarded["id"], "ok": True, "result": {"profile": "Default"},
            }))
            response = json.loads(client.recv(4096).split(b"\n", 1)[0])
            self.assertEqual(response["result"]["profile"], "Default")
            self.assertIn("revision", response["result"]["native_host_build"])
            client.close()
            extension_output.close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [0])
            extension_input.close()
            native_input.close()
            native_output.close()


class ProcessTests(unittest.TestCase):
    def test_proc_stat_allows_spaces_in_process_name(self):
        # Fields after the name begin with state, PPID, then 17 values before starttime.
        raw = "123 (tmux: client) S 42 " + "0 " * 17 + "987 0 0"
        self.assertEqual(_parse_proc_stat(raw), (42, 987))

    def test_rollout_name_exposes_session_id(self):
        name = "rollout-2026-08-04T20-00-00-11111111-1111-4111-8111-111111111111.jsonl"
        self.assertEqual(ROLLOUT_RE.fullmatch(name).group(1), "11111111-1111-4111-8111-111111111111")

    def test_open_subagent_rollout_resolves_to_root_session(self):
        root_id = "11111111-1111-4111-8111-111111111111"
        child_id = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions/2026/08/04"
            sessions.mkdir(parents=True)
            child = sessions / f"rollout-2026-08-04T20-01-00-{child_id}.jsonl"
            child.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": child_id,
                    "session_id": root_id,
                    "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": root_id,
                        "depth": 1,
                    }}},
                },
            }) + "\n")
            descriptors = root / "proc/123/fd"
            descriptors.mkdir(parents=True)
            (descriptors / "3").symlink_to(child)

            self.assertEqual(
                _session_from_open_files(123, proc_root=root / "proc"),
                root_id,
            )

    def test_parent_and_subagent_descriptors_resolve_to_same_root(self):
        root_id = "11111111-1111-4111-8111-111111111111"
        child_id = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions/2026/08/04"
            sessions.mkdir(parents=True)
            parent = sessions / f"rollout-2026-08-04T20-00-00-{root_id}.jsonl"
            child = sessions / f"rollout-2026-08-04T20-01-00-{child_id}.jsonl"
            parent.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"id": root_id, "session_id": root_id, "source": "cli"},
            }) + "\n")
            child.write_text(json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": child_id,
                    "session_id": root_id,
                    "source": {"subagent": {"thread_spawn": {
                        "parent_thread_id": root_id,
                        "depth": 1,
                    }}},
                },
            }) + "\n")
            descriptors = root / "proc/123/fd"
            descriptors.mkdir(parents=True)
            (descriptors / "3").symlink_to(child)
            (descriptors / "4").symlink_to(parent)

            self.assertEqual(
                _session_from_open_files(123, proc_root=root / "proc"),
                root_id,
            )

    def test_conflicting_open_rollouts_are_not_guessed(self):
        first_id = "11111111-1111-4111-8111-111111111111"
        second_id = "22222222-2222-4222-8222-222222222222"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = root / "sessions/2026/08/04"
            sessions.mkdir(parents=True)
            first = sessions / f"rollout-2026-08-04T20-00-00-{first_id}.jsonl"
            second = sessions / f"rollout-2026-08-04T20-01-00-{second_id}.jsonl"
            for path, session_id in ((first, first_id), (second, second_id)):
                path.write_text(json.dumps({
                    "type": "session_meta",
                    "payload": {"id": session_id, "session_id": session_id, "source": "cli"},
                }) + "\n")
            descriptors = root / "proc/123/fd"
            descriptors.mkdir(parents=True)
            (descriptors / "3").symlink_to(first)
            (descriptors / "4").symlink_to(second)

            self.assertIsNone(_session_from_open_files(123, proc_root=root / "proc"))


class StartupLauncherTests(unittest.TestCase):
    def _launcher_environment(
        self,
        root: Path,
        sleep_body: str = "exit 0\n",
        wsctl_body: str | None = None,
    ):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        source = Path(__file__).parents[1] / "bin/wsctl-startup-launch"
        launcher = bin_dir / "wsctl-startup-launch"
        launcher.write_text(source.read_text())
        launcher.chmod(0o755)
        worker_source = Path(__file__).parents[1] / "bin/wsctl-startup-worker"
        worker = bin_dir / "wsctl-startup-worker"
        worker.write_text(worker_source.read_text())
        worker.chmod(0o755)
        wsctl_log = root / "wsctl.log"
        wsctl = bin_dir / "wsctl"
        wsctl.write_text(
            "#!/bin/sh\n" + (wsctl_body or
            "printf '%s\\n' \"$*\" >> \"$TEST_WSCTL_LOG\"\n")
        )
        wsctl.chmod(0o755)
        fake_bin = root / "fake-bin"
        fake_bin.mkdir()
        sleep = fake_bin / "sleep"
        sleep.write_text(f"#!/bin/sh\n{sleep_body}")
        sleep.chmod(0o755)
        systemd_run = fake_bin / "systemd-run"
        systemd_run.write_text(
            "#!/bin/sh\n"
            "while [ \"$#\" -gt 0 ]; do\n"
            "  case \"$1\" in --) shift; break ;; --*) shift ;; *) break ;; esac\n"
            "done\n"
            "\"$@\" </dev/null &\n"
        )
        systemd_run.chmod(0o755)
        runtime = root / "runtime"
        runtime.mkdir()
        environment = {
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "XDG_RUNTIME_DIR": str(runtime),
            "TEST_WSCTL_LOG": str(wsctl_log),
            "WSCTL_SYSTEMD_RUN": str(systemd_run),
        }
        return launcher, runtime, wsctl_log, environment

    def test_partial_restore_does_not_reclaim_later_terminal_launches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher, runtime, wsctl_log, environment = self._launcher_environment(root)
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            startup = runtime / "workspace-state" / f"startup-{boot_id}"
            startup.mkdir(parents=True)
            (startup / "terminals.done").write_text("done\n")

            first = subprocess.run([launcher], env=environment, check=False)
            second = subprocess.run([launcher], env=environment, check=False)

            self.assertEqual((first.returncode, second.returncode), (1, 1))
            self.assertTrue((startup / "launcher.claimed").is_file())
            self.assertFalse(wsctl_log.exists())

    def test_login_generation_joins_an_early_legacy_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher, runtime, wsctl_log, environment = self._launcher_environment(root)
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            runtime_root = runtime / "workspace-state"
            legacy = runtime_root / f"startup-{boot_id}"
            legacy.mkdir(parents=True)
            (runtime_root / "login-generation").write_text("0123456789abcdef\n")

            result = subprocess.run([launcher], env=environment, check=False)
            for _ in range(100):
                if wsctl_log.exists():
                    break
                time.sleep(0.01)

            self.assertEqual(result.returncode, 0)
            self.assertTrue((legacy / "launcher.claimed").is_file())
            self.assertFalse(
                (runtime_root / f"startup-{boot_id}-0123456789abcdef").exists(),
            )

    def test_concurrent_launch_joins_startup_once_then_becomes_normal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            launcher, runtime, wsctl_log, environment = self._launcher_environment(
                root,
                wsctl_body=(
                    "while [ ! -f \"$TEST_RELEASE\" ]; do /usr/bin/sleep 0.01; done\n"
                    "printf '%s\\n' \"$*\" >> \"$TEST_WSCTL_LOG\"\n"
                ),
            )
            environment["TEST_RELEASE"] = str(release)

            first = subprocess.run([launcher], env=environment, check=False)
            concurrent = subprocess.run([launcher], env=environment, check=False)
            release.write_text("go\n")
            for _ in range(100):
                if wsctl_log.exists():
                    break
                time.sleep(0.01)
            later = subprocess.run([launcher], env=environment, check=False)

            self.assertEqual(first.returncode, 0)
            self.assertEqual(concurrent.returncode, 2)
            self.assertEqual(later.returncode, 1)
            self.assertEqual(wsctl_log.read_text(), "startup --wait 120\n")
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            marker = runtime / "workspace-state" / f"startup-{boot_id}" / "launcher.claimed"
            self.assertEqual(marker.stat().st_mode & 0o777, 0o600)

    def test_failed_restore_is_not_replayed_automatically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            counter = root / "attempts"
            finished = root / "finished"
            body = (
                "trap 'touch \"$TEST_ATTEMPT_FINISHED\"' EXIT\n"
                "count=$(sed -n '1p' \"$TEST_ATTEMPTS\" 2>/dev/null || true)\n"
                "case \"$count\" in ''|*[!0-9]*) count=0 ;; esac\n"
                "count=$((count + 1))\n"
                "printf '%s\\n' \"$count\" > \"$TEST_ATTEMPTS\"\n"
                "printf '%s\\n' \"$*\" >> \"$TEST_WSCTL_LOG\"\n"
                "[ \"$count\" -ge 2 ]\n"
            )
            launcher, _runtime, wsctl_log, environment = self._launcher_environment(
                root,
                wsctl_body=body,
            )
            environment["TEST_ATTEMPTS"] = str(counter)
            environment["TEST_ATTEMPT_FINISHED"] = str(finished)

            self.assertEqual(subprocess.run([launcher], env=environment).returncode, 0)
            deadline = time.monotonic() + 5
            while not finished.exists() and time.monotonic() < deadline:
                time.sleep(0.01)

            self.assertTrue(finished.exists(), "fake restore worker did not finish")
            self.assertEqual(subprocess.run([launcher], env=environment).returncode, 1)
            self.assertEqual(counter.read_text().strip(), "1")
            self.assertEqual(
                wsctl_log.read_text().splitlines(),
                ["startup --wait 120"],
            )


class RestoreTests(unittest.TestCase):

    def test_slow_codex_verification_does_not_block_remaining_startup(self):
        snapshot = {"sessions": [{
            "name": "work",
            "windows": [{
                "index": 1,
                "name": "codex",
                "panes": [{
                    "index": 1,
                    "codex": {"session_id": "saved-id"},
                }],
            }],
        }], "terminals": []}
        args = Namespace(
            workspace=None, session=None, select=False, dry_run=False,
            no_place=False,
            verify_codex=True, wait=120, login_status=True,
        )
        with patch(
            "workspace_state.cli._live_terminal_clients", return_value={},
        ), patch(
            "workspace_state.cli.recreate_tmux", return_value=("work", []),
        ), patch(
            "workspace_state.cli.missing_codex_ids", return_value={"saved-id"},
        ), patch(
            "workspace_state.cli.time.monotonic", side_effect=[100.0, 115.0],
        ), patch(
            "workspace_state.cli._conversation_deadline", return_value=115.0,
        ), patch("workspace_state.cli.update_stage") as update:
            outcome = _restore_terminals(snapshot, args)

        self.assertEqual(outcome.restored, 1)
        self.assertEqual(outcome.codex_ready, 0)
        self.assertEqual(outcome.codex_total, 1)
        self.assertFalse(outcome.codex_verified)
        self.assertIn(
            "degraded",
            [invocation.args[1] for invocation in update.call_args_list],
        )

    def test_exact_numeric_tmux_session_target_has_colon(self):
        completed = type("Completed", (), {"returncode": 0})()
        with patch("workspace_state.restore.subprocess.run", return_value=completed) as mocked:
            self.assertTrue(_tmux_exists("1"))
        self.assertEqual(mocked.call_args.args[0], ["tmux", "has-session", "-t", "=1:"])

    def test_terminal_attach_uses_exact_numeric_session_target(self):
        result = launch_terminal({"name": "1"}, dry_run=True)
        self.assertIn("attach-session -t =1:", result.message)

    def test_codex_failure_falls_back_to_shell(self):
        pane = {"codex": {"session_id": "11111111-1111-4111-8111-111111111111"}}
        with patch.dict(os.environ, {"SHELL": "/usr/bin/zsh"}), patch(
            "workspace_state.operations.current", return_value=None,
        ):
            command = _pane_shell_command(pane)
        self.assertEqual(
            command,
            "wsctl-codex-resume 11111111-1111-4111-8111-111111111111; exec /usr/bin/zsh",
        )

    def test_saved_codex_session_requires_live_identity_match(self):
        session = {
            "windows": [{
                "index": 1,
                "name": "codex",
                "panes": [{"index": 1, "codex": {"session_id": "saved-id"}}],
            }],
        }
        state = {1: {"name": "codex", "panes": {1: {}}}}
        with patch("workspace_state.restore._live_codex_ids", return_value={}):
            self.assertFalse(_same_tmux_session(session, state))
        with patch("workspace_state.restore._live_codex_ids", return_value={(1, 1): "saved-id"}):
            self.assertTrue(_same_tmux_session(session, state))

    def test_live_codex_identity_survives_tmux_automatic_rename(self):
        session = {"windows": [{
            "index": 1,
            "name": "codex",
            "panes": [{"index": 1, "codex": {"session_id": "saved-id"}}],
        }]}
        state = {1: {"name": "sh", "panes": {1: {}}}}
        with patch(
            "workspace_state.restore._live_codex_ids",
            return_value={(1, 1): "saved-id"},
        ):
            self.assertTrue(_same_tmux_session(session, state))

    def test_private_restore_fingerprint_adopts_partial_codex_session(self):
        session = {"windows": [{
            "index": 1,
            "name": "codex",
            "panes": [{"index": 1, "codex": {"session_id": "saved-id"}}],
        }]}
        state = {1: {"name": "codex", "panes": {1: {}}}}
        with patch("workspace_state.restore._live_codex_ids", return_value={}):
            self.assertTrue(_same_tmux_session(session, state, trusted_recipe=True))

    def test_swapped_codex_conversations_are_not_the_same_session(self):
        session = {"windows": [{
            "index": 1,
            "name": "codex",
            "panes": [
                {"index": 1, "codex": {"session_id": "one"}},
                {"index": 2, "codex": {"session_id": "two"}},
            ],
        }]}
        state = {1: {"name": "codex", "panes": {1: {}, 2: {}}}}
        with patch("workspace_state.restore._live_codex_ids", return_value={
            (1, 1): "two", (1, 2): "one",
        }):
            self.assertFalse(_same_tmux_session(session, state))


class TmuxCompatibilityHookTests(unittest.TestCase):
    """wsctl alone owns tmux; hooks of older configurations are harmless shims."""

    def test_tmux_mapping_is_removed_without_touching_other_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "tmux.conf"
            config.write_text(
                "set -g mouse on\n"
                "# Keep restored Codex panes alive as shells when a resume exits.\n"
                "set -g @resurrect-processes '\"wsctl-codex->wsctl-codex-resume *\"'\n"
                "set -g status on\n"
            )

            self.assertTrue(_unconfigure_tmux_file(config))
            self.assertFalse(_unconfigure_tmux_file(config))

            self.assertEqual(
                config.read_text(),
                "set -g mouse on\nset -g status on\n",
            )

    def test_configure_never_writes_the_tmux_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "tmux.conf"
            with patch("workspace_state.cli.subprocess.run") as run:
                self.assertEqual(cli_main(["tmux", "configure", "--config", str(config)]), 0)
            self.assertFalse(config.exists())
            run.assert_not_called()

    def test_restore_hooks_do_nothing_and_succeed(self):
        for hook in (["begin", "123"], ["restore"], ["restore", "--wait", "5"], ["end"],
                     ["save-allowed"], ["contract", "/nonexistent/state"]):
            with self.subTest(hook=hook), patch(
                "workspace_state.cli._auto_checkpoint",
            ) as checkpoint, patch("workspace_state.cli.cmd_startup") as startup, patch(
                "workspace_state.cli.subprocess.run",
            ) as run:
                self.assertEqual(cli_main(["tmux", *hook]), 0)
                checkpoint.assert_not_called()
                startup.assert_not_called()
                run.assert_not_called()

    def test_save_hook_runs_only_the_terminal_checkpoint(self):
        for arguments in ([], ["/nonexistent/tmux_resurrect_1.txt"]):
            with self.subTest(arguments=arguments), patch(
                "workspace_state.cli._auto_checkpoint", return_value=(0, True),
            ) as checkpoint, patch("workspace_state.cli.subprocess.run") as run:
                self.assertEqual(cli_main(["tmux", "save", *arguments]), 0)
                checkpoint.assert_called_once_with(("terminals",))
                run.assert_not_called()

    def test_save_hook_never_reports_a_checkpoint_failure(self):
        with patch(
            "workspace_state.cli._auto_checkpoint", side_effect=RuntimeError("capture crashed"),
        ):
            self.assertEqual(cli_main(["tmux", "save"]), 0)

    def test_hooks_exit_zero_with_a_damaged_operation_context(self):
        with patch.dict(os.environ, {operations.CONTEXT_ENV: "{not json"}, clear=False):
            for hook in (["restore"], ["begin", "1"], ["end"], ["save-allowed"]):
                with self.subTest(hook=hook):
                    self.assertEqual(cli_main(["tmux", *hook]), 0)

    def test_hooks_ignore_an_inherited_operation_context(self):
        context = operations.OperationContext(
            boot_id="b", login_generation="g", operation_id="o", mode="startup",
            attempt=1, deadline=time.monotonic() + 60,
        )
        with patch("workspace_state.operations.current", return_value=context), patch(
            "workspace_state.operations.context_from_status", side_effect=RuntimeError("expired"),
        ) as verify:
            self.assertEqual(cli_main(["tmux", "restore"]), 0)
        verify.assert_not_called()

    def _wrapper_run(self, name, *arguments):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wrapper = root / name
            shutil.copy2(Path(__file__).parents[1] / "bin" / name, wrapper)
            log = root / "calls.log"
            command = root / "wsctl"
            command.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_LOG"\nexit "${TEST_STATUS:-0}"\n')
            command.chmod(0o755)
            plugin = root / "plugins/tmux-resurrect/scripts"
            plugin.mkdir(parents=True)
            for script in ("save.sh", "restore.sh"):
                (plugin / script).write_text('#!/bin/sh\necho resurrect >> "$TEST_LOG"\n')
                (plugin / script).chmod(0o755)
            for status in ("0", "2"):
                log.unlink(missing_ok=True)
                result = subprocess.run(
                    [wrapper, *arguments], capture_output=True, text=True, check=False,
                    env={**os.environ, "TEST_LOG": str(log), "TEST_STATUS": status,
                         "TMUX_PLUGIN_MANAGER_PATH": str(root / "plugins")},
                )
                self.assertEqual(result.returncode, 0)
                yield log.read_text() if log.exists() else ""

    def test_save_wrapper_runs_only_the_terminal_checkpoint(self):
        for calls in self._wrapper_run("wsctl-continuum-save", "ignored"):
            self.assertEqual(calls, "tmux save\n")

    def test_restore_wrapper_does_nothing(self):
        for calls in self._wrapper_run("wsctl-continuum-restore"):
            self.assertEqual(calls, "")

    def test_codex_restore_wrapper_returns_to_existing_shell_without_nesting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            codex = fake_bin / "codex"
            codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_CODEX_ARGS"\nexit 9\n')
            codex.chmod(0o755)
            shell_log = root / "shell.log"
            shell = fake_bin / "test-shell"
            shell.write_text(
                "#!/bin/sh\nprintf 'shell-preserved\\n' > \"$TEST_SHELL_LOG\"\n"
            )
            shell.chmod(0o755)
            wrapper = Path(__file__).parents[1] / "bin/wsctl-codex-resume"

            result = subprocess.run(
                [wrapper, "11111111-1111-4111-8111-111111111111"],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}:{os.environ['PATH']}",
                    "SHELL": str(shell),
                    "TEST_SHELL_LOG": str(shell_log),
                    "TEST_CODEX_ARGS": str(root / "codex.args"),
                    "XDG_RUNTIME_DIR": str(root / "runtime"),
                    "CODEX_HOME": str(root / "codex-home"),
                },
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self.assertEqual(result.returncode, 9)
            self.assertFalse(shell_log.exists())
            self.assertEqual((root / "codex.args").read_text().splitlines(), [
                "resume",
                "--no-alt-screen", "11111111-1111-4111-8111-111111111111",
            ])


if __name__ == "__main__":
    unittest.main()
