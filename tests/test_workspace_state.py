from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from workspace_state.browser import (
    _attach_desktop_placements,
    browser_window_placement,
    ensure_browser_profiles,
    profile_socket_path,
    restore_browser,
)
from workspace_state.cli import (
    _autosave_from_tmux,
    _browser_problems,
    _restore_items,
    _select,
    _session_workspace,
    _wait_for_tmux_restore,
    _workspace_groups,
    cmd_save,
    parser,
)
from workspace_state.capture import ROLLOUT_RE, _parse_proc_stat
from workspace_state.desktop import _add_monitor_identities, place_by_title, remap_monitor, remap_workspace
from workspace_state.native_host import _resolved_profile, decode_native_messages, encode_native_message, serve
from workspace_state.resurrect import annotate_state_file, preserve_last_state
from workspace_state.restore import _pane_shell_command, _same_tmux_session, _tmux_exists, launch_terminal
from workspace_state.storage import load, path_for, save


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

    def test_partial_save_does_not_overwrite_existing_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"XDG_DATA_HOME": directory}):
                old = {"name": "current", "created_at": "old", "sessions": []}
                save(old)
                partial = {
                    "name": "current",
                    "desktop": {"shell_companion": False},
                    "terminals": [],
                    "sessions": [],
                }
                with patch("workspace_state.cli._capture_all", return_value=partial):
                    with self.assertRaisesRegex(RuntimeError, "state not saved"):
                        cmd_save(Namespace(allow_partial=False))
                self.assertEqual(load()["created_at"], "old")

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

    def test_tmux_restore_timeout_does_not_remove_running_marker(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": directory}, clear=False,
        ), patch("workspace_state.cli._boot_id", return_value="test-boot"):
            marker = Path(directory) / "workspace-state/startup-test-boot/tmux-restore.running"
            marker.parent.mkdir(parents=True)
            marker.write_text("123\n")
            with self.assertRaisesRegex(RuntimeError, "still running"):
                _wait_for_tmux_restore(0)
            self.assertTrue(marker.exists())


class MonitorIdentityTests(unittest.TestCase):
    def test_matches_shell_geometry_to_monitor_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".config").mkdir()
            (home / ".config/monitors.xml").write_text("""
<monitors version="2"><configuration><logicalmonitor>
<x>1920</x><y>0</y><monitor><monitorspec><connector>DP-1</connector>
<vendor>GSM</vendor><product>LG HDR 4K</product><serial>ABC</serial>
</monitorspec><mode><width>3840</width><height>2160</height></mode></monitor>
</logicalmonitor></configuration></monitors>
""")
            state = {
                "monitors": [{"index": 0, "x": 1920, "y": 0, "width": 3840, "height": 2160}],
                "windows": [{"monitor": 0}],
            }
            with patch("workspace_state.desktop.Path.home", return_value=home):
                _add_monitor_identities(state)
            self.assertEqual(state["monitors"][0]["identity"]["serial"], "ABC")
            self.assertEqual(state["windows"][0]["monitor_identity"]["product"], "LG HDR 4K")

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
            self.assertEqual(remap_monitor(placement)["monitor"], 1)

    def test_edid_hash_is_preferred_over_connector(self):
        placement = {"monitor": 0, "monitor_identity": {"connector": "DP-1", "edid_hash": "same"}}
        current = {"monitors": [
            {"index": 0, "identity": {"connector": "DP-1", "edid_hash": "different"}},
            {"index": 1, "identity": {"connector": "HDMI-1", "edid_hash": "same"}},
        ]}
        with patch("workspace_state.desktop.capture_shell", return_value=current):
            self.assertEqual(remap_monitor(placement)["monitor"], 1)

    def test_title_placement_prefers_state_aware_stable_window_move(self):
        placement = {"state": "fullscreen", "geometry": {}}
        with (
            patch("workspace_state.desktop.list_windows", return_value=[{"id": 42, "title": "restore-me"}]),
            patch("workspace_state.desktop.move_window", return_value=True) as move,
        ):
            self.assertTrue(place_by_title("restore-me", placement))
        move.assert_called_once_with(42, placement)


class BrowserTests(unittest.TestCase):
    def test_matches_chrome_api_window_to_shell_placement(self):
        profiles = [{
            "profile": "Default",
            "app_id": "google-chrome",
            "windows": [{
                "id": "window-1",
                "bounds": {"left": 100, "top": 50, "width": 1200, "height": 800},
                "state": "normal",
                "tabs": [],
            }],
        }]
        shell = {"windows": [{
            "wm_class": "Google-chrome",
            "workspace": 1,
            "monitor": 2,
            "monitor_identity": {"connector": "DP-1", "edid_hash": "abc"},
            "monitor_geometry": {"index": 2, "x": 0, "y": 0, "width": 1920, "height": 1080},
            "geometry": {"x": 102, "y": 52, "width": 1200, "height": 800},
            "state": "maximized",
        }]}
        _attach_desktop_placements(profiles, shell, ["Life", "Research"])
        window = profiles[0]["windows"][0]
        self.assertEqual(window["workspace"], "Research")
        self.assertEqual(window["monitor"]["connector"], "DP-1")
        self.assertEqual(window["state"], "maximized")
        placement = browser_window_placement(window)
        self.assertEqual(placement["monitor_identity"]["edid_hash"], "abc")

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

        def fake_expect(_app_id, _placement):
            expectation = f"expect-{len([event for event in events if event.startswith('expect')]) + 1}"
            events.append(expectation)
            return expectation

        def fake_request(_action, payload, *, profile, timeout=60):
            events.append(f"request-{payload['window']['id']}-{profile}")
            return {"warnings": []}

        def fake_status(expectation):
            events.append(f"status-{expectation}")
            return "placed"

        with (
            patch("workspace_state.browser.remap_monitor", side_effect=lambda value: value),
            patch("workspace_state.browser.remap_workspace", side_effect=lambda value: value),
            patch("workspace_state.browser.expect_window", side_effect=fake_expect),
            patch("workspace_state.browser.request_browser", side_effect=fake_request),
            patch("workspace_state.browser.expected_window_status", side_effect=fake_status),
        ):
            actions = restore_browser(chrome)
        self.assertEqual(events, [
            "expect-1", "request-window-1-Default", "status-expect-1",
            "expect-2", "request-window-2-Default", "status-expect-2",
        ])
        self.assertEqual(len(actions), 2)

    def test_startup_launches_supported_browser_and_waits_for_profile(self):
        chrome = {"profiles": [{"profile": "Default", "app_id": "google-chrome"}]}
        with (
            patch("workspace_state.browser.connected_profiles", side_effect=[[], ["Default"]]),
            patch("workspace_state.browser.shutil.which", return_value="/usr/bin/google-chrome"),
            patch("workspace_state.browser.subprocess.Popen") as popen,
            patch("workspace_state.browser.data_home", return_value=Path("/tmp/wsctl-test-data")),
            patch("workspace_state.browser.time.sleep"),
        ):
            self.assertEqual(ensure_browser_profiles(chrome), ["google-chrome (Default)"])
        self.assertEqual(popen.call_args.args[0], [
            "/usr/bin/google-chrome",
            "--profile-directory=Default",
            "--load-extension=/tmp/wsctl-test-data/chrome-extension",
            "--no-startup-window",
        ])

    def test_startup_launches_each_saved_chrome_profile_directory(self):
        chrome = {"profiles": [
            {"profile": "Personal", "profile_directory": "Default", "app_id": "google-chrome"},
            {"profile": "Work", "profile_directory": "Profile 1", "app_id": "google-chrome"},
        ]}
        with (
            patch("workspace_state.browser.connected_profiles", side_effect=[[], ["Personal", "Work"]]),
            patch("workspace_state.browser.shutil.which", return_value="/usr/bin/google-chrome"),
            patch("workspace_state.browser.subprocess.Popen") as popen,
            patch("workspace_state.browser.data_home", return_value=Path("/tmp/wsctl-test-data")),
            patch("workspace_state.browser.time.sleep"),
        ):
            ensure_browser_profiles(chrome)
        self.assertEqual(popen.call_count, 2)
        commands = [call.args[0] for call in popen.call_args_list]
        self.assertIn("--profile-directory=Default", commands[0])
        self.assertIn("--profile-directory=Profile 1", commands[1])


class NativeMessagingTests(unittest.TestCase):
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


class RestoreTests(unittest.TestCase):
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
        with patch.dict(os.environ, {"SHELL": "/usr/bin/zsh"}):
            command = _pane_shell_command(pane)
        self.assertEqual(
            command,
            "codex resume --no-alt-screen 11111111-1111-4111-8111-111111111111; exec /usr/bin/zsh",
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
        with patch("workspace_state.restore._live_codex_ids", return_value={}):
            self.assertTrue(_same_tmux_session(session, state, repair_processes=True))
        with patch("workspace_state.restore._live_codex_ids", return_value={(1, 1): "saved-id"}):
            self.assertTrue(_same_tmux_session(session, state))

    def test_startup_can_adopt_one_pristine_bootstrap_shell(self):
        session = {
            "windows": [{
                "index": 1,
                "name": "general",
                "panes": [{"index": 1, "codex": {"session_id": "saved-id"}}],
            }],
        }
        state = {1: {
            "name": "zsh",
            "panes": {1: {"command": "zsh", "pid": 1, "cwd": "/tmp"}},
        }}
        with patch("workspace_state.restore._live_codex_ids", return_value=set()):
            self.assertTrue(_same_tmux_session(session, state, repair_processes=True))

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


class ResurrectHookTests(unittest.TestCase):
    def test_contracts_codex_uuid_for_resurrect_argument_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "tmux_resurrect.txt"
            state.write_text(
                "pane\tWork-1\t2\t1\t:*\t1\ttitle\t:/tmp\t1\tcodex\t:/usr/bin/codex\n"
                "pane\tmain\t1\t1\t:*\t1\ttitle\t:/tmp\t1\tzsh\t:\n"
            )
            with patch(
                "workspace_state.resurrect.codex_resume_token",
                side_effect=["wsctl-codex 11111111-1111-4111-8111-111111111111", None],
            ):
                result = annotate_state_file(state)
            self.assertEqual(result, {"annotated": 1, "unresolved": 0})
            self.assertIn(
                ":wsctl-codex 11111111-1111-4111-8111-111111111111",
                state.read_text(),
            )

    def test_unarmed_save_preserves_previous_resurrect_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "tmux_resurrect_old.txt"
            candidate = root / "tmux_resurrect_new.txt"
            protected.write_text("protected\n")
            candidate.write_text("partial\n")
            (root / "last").symlink_to(protected.name)
            self.assertEqual(preserve_last_state(candidate), protected)
            self.assertEqual(candidate.read_text(), "protected\n")

    def test_historical_state_can_contract_from_the_canonical_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "tmux_resurrect.txt"
            state.write_text(
                "pane\tWork-1\t2\t0\t:*\t1\ttitle\t:/tmp\t1\tcodex\t:\n"
                "window\tWork-1\t2\t:codex\t1\t:*\tlayout\toff\n"
            )
            recipe = {"sessions": [{
                "name": "Work-1",
                "windows": [{
                    "index": 2,
                    "name": "codex",
                    "panes": [{
                        "index": 1,
                        "cwd": "/tmp",
                        "codex": {"session_id": "saved-id"},
                    }],
                }],
            }]}
            with patch("workspace_state.resurrect.codex_resume_token", return_value=None):
                result = annotate_state_file(state, recipe)
            self.assertEqual(result["annotated"], 1)
            self.assertIn(":wsctl-codex saved-id", state.read_text())


if __name__ == "__main__":
    unittest.main()
