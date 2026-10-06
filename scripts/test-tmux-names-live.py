#!/usr/bin/env python3
"""Opt-in end-to-end pane naming test on a private, unattached tmux server.

Creates no desktop/terminal windows; never addresses the user's tmux socket.
The temporary server, processes and checkpoint are removed on completion.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
capture = importlib.import_module("workspace_state.capture")
from workspace_state import restore, storage, tmux_names
from workspace_state.util import CommandError


def main():
    with tempfile.TemporaryDirectory(prefix="wsctl-tmux-names.") as temp:
        root = Path(temp)
        socket_path = root / "server.sock"
        environment = {**os.environ, "TMUX": "", "SHELL": "/bin/sh"}

        def execute(command, *, check=True, **kwargs):
            assert command[0] == "tmux", command
            result = subprocess.run(
                ["tmux", "-S", str(socket_path), "-f", "/dev/null", *command[1:]],
                env=environment, text=True, capture_output=True, timeout=10,
            )
            if check and result.returncode:
                raise CommandError(result.stderr.strip())
            return result.stdout

        def tmux(*args):
            return execute(["tmux", *args])

        def start(name, window):
            pane = tmux("new-session", "-d", "-s", name, "-n", window,
                        "-c", str(root), "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
            tmux("set-option", "-g", "default-shell", "/bin/sh")
            return pane

        def window_for(pane):
            return tmux("display-message", "-p", "-t", pane, "#{window_id}").strip()

        def exists(name):
            return name in tmux("list-sessions", "-F", "#{session_name}").splitlines()

        def geometry(name):
            fields = "\t".join(("#{window_index}", "#{pane_index}", "#{pane_left}",
                                 "#{pane_top}", "#{pane_width}", "#{pane_height}",
                                 "#{pane_active}", "#{window_active}"))
            rows = (line.split("\t") for line in tmux(
                "list-panes", "-s", "-t", f"={name}:", "-F", fields).splitlines())
            return {(int(row[0]), int(row[1])): tuple(map(int, row[2:])) for row in rows}

        def custom_layout(body):
            checksum = 0
            for character in body:
                checksum = ((checksum >> 1) + ((checksum & 1) << 15) + ord(character)) & 0xffff
            return f"{checksum:04x},{body}"

        def application_output(pane, command, expected_title=None, expected_name=None):
            # This input goes only to the fresh shell owned by this private
            # test server. Exercise terminal escapes, not tmux title commands.
            tmux("send-keys", "-t", pane, "-l", command)
            tmux("send-keys", "-t", pane, "Enter")
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                title = tmux_names.read_pane_names(pane)["title"]
                name = tmux_names.read_window_names(window_for(pane))["name"]
                if (expected_title is None or title == expected_title) and (expected_name is None or name == expected_name):
                    return
                time.sleep(0.02)
            raise AssertionError((title, name, expected_title, expected_name))

        shell = {"available": True, "windows": [], "monitors": [], "workspaces": []}
        try:
            with patch.object(capture, "run", side_effect=execute), patch.object(
                capture, "capture_shell", return_value=shell,
            ), patch.object(capture, "workspace_names", return_value=[]), patch.object(
                capture, "codex_for_pane", return_value=None,
            ), patch.object(tmux_names, "run", side_effect=execute), patch.object(
                restore, "run", side_effect=execute,
            ), patch.object(restore, "_tmux_exists", side_effect=exists), patch.object(
                restore, "codex_for_pane", return_value=None,
            ), patch.dict(os.environ, {"XDG_DATA_HOME": str(root / "data"), "SHELL": "/bin/sh"}):
                first = start("saved", "DEBUG_WINDOW")
                window = window_for(first)
                # Exercise enough panes to expose repeated insertion after the
                # original active pane and repeated halving of its height.
                main_panes = [first]
                for _ in range(4):
                    tmux("select-layout", "-t", window, "tiled")
                    main_panes.append(tmux(
                        "split-window", "-d", "-t", main_panes[-1], "-c", str(root),
                        "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip())
                ids = [pane[1:] for pane in main_panes]
                layout = custom_layout(
                    f"140x50,0,0[140x30,0,0{{83x30,0,0[83x17,0,0,{ids[0]},"
                    f"83x12,0,18,{ids[1]}],56x30,84,0,{ids[2]}}},"
                    f"140x19,0,31{{47x19,0,31,{ids[3]},92x19,48,31,{ids[4]}}}]")
                tmux("select-layout", "-t", window, layout)
                tmux("set-option", "-w", "-t", window, "pane-base-index", "1")
                tmux("select-pane", "-t", main_panes[3])
                literal_window_name = "project #{pane_id} #(printf expanded) ;"
                third = tmux("new-window", "-d", "-t", "=saved:", "-n",
                             tmux_names.literal_tmux_argument(literal_window_name, expands_formats=True),
                             "-c", str(root), "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                fourth = tmux("new-window", "-d", "-t", "=saved:", "-n", "tab\tname\nwith delimiters",
                              "-c", str(root), "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                names = [
                    {"label": "Перевірка #{pane_id} #(printf literal) ;", "title": "volatile app title"},
                    {"label": "", "title": "  spaces and Україна  "},
                    {"label": "third pane", "title": "third app"},
                    {"label": "fourth pane", "title": "fourth app"},
                    {"label": "fifth pane", "title": "fifth app"},
                    {"label": None, "title": "literal #{pane_id};"},
                    {"label": None, "title": "delimiter window pane"},
                ]
                for pane, naming in zip((*main_panes, third, fourth), names):
                    tmux_names.restore_pane_names(pane, window_for(pane), naming)
                    tmux("set-option", "-p", "-t", pane, "allow-rename", "on" if pane == third else "off")
                old_geometry = geometry("saved")
                checkpoint = capture.capture()
                assert not checkpoint["capture_errors"]["tmux"], checkpoint["capture_errors"]
                storage.save(checkpoint)
                saved = storage.load()["sessions"][0]
                old_ids = {p["id"] for w in saved["windows"] for p in w["panes"]}
                old_windows = [w["name"] for w in saved["windows"]]
                # Stop only this test's private server, never the default socket.
                tmux("kill-server")
                socket_path = root / "restored.sock"
                sentinel = start("untouched", "KEEP_THIS_TAB")
                tmux("set-option", "-g", "allow-rename", "on")
                tmux_names.restore_pane_names(sentinel, window_for(sentinel), {"label": "KEEP_THIS_PANE"})
                actual, _ = restore.recreate_tmux(saved)
                assert actual == "saved"
                state = restore._tmux_state(actual)
                new_ids = {p["id"] for w in state.values() for p in w["panes"].values()}
                assert old_ids != new_ids, "test must exercise pane ID remapping"

                def verify():
                    state = restore._tmux_state(actual)
                    assert [w["name"] for w in state.values()] == old_windows
                    assert geometry(actual) == old_geometry, (geometry(actual), old_geometry)
                    for saved_window in saved["windows"]:
                        live_window = state[saved_window["index"]]
                        assert tmux_names.read_window_names(live_window["id"])["automatic_rename"] == saved_window["automatic_rename"]
                        for saved_pane in saved_window["panes"]:
                            live_id = live_window["panes"][saved_pane["index"]]["id"]
                            naming = tmux_names.read_pane_names(live_id, window_id=live_window["id"])
                            assert naming["label"] == saved_pane["label"], (naming, saved_pane)
                            assert naming["title"] == (saved_pane["label"] or saved_pane["title"])
                            assert tmux_names.read_pane_rename_policy(live_id) == saved_pane["allow_rename"]
                    assert tmux_names.read_pane_names(sentinel)["label"] == "KEEP_THIS_PANE"
                    assert tmux("display-message", "-p", "-t", sentinel, "#{window_name}").strip() == "KEEP_THIS_TAB"
                    return state

                verify()
                live_first = state[saved["windows"][0]["index"]]["panes"][1]["id"]
                application_output(live_first,
                                   r"printf '\033]2;application title changed\007'; printf '\033kFORBIDDEN_TAB\033\\'",
                                   expected_title="application title changed", expected_name="DEBUG_WINDOW")
                assert tmux_names.read_pane_names(live_first)["label"] == names[0]["label"]
                live_literal = state[saved["windows"][1]["index"]]["panes"][0]["id"]
                application_output(live_literal, r"printf '\033kALLOWED_APP_TAB\033\\'",
                                   expected_name="ALLOWED_APP_TAB")
                # Simulate independently resurrected windows: no wsctl tag, and
                # pane labels missing although the saved layout is already live.
                tmux("set-option", "-u", "-t", "=saved:", "@wsctl-restore-fingerprint")
                for pane in new_ids:
                    tmux("set-option", "-p", "-u", "-t", pane, "@pane_label")
                restore.recreate_tmux(saved, adopt_restored=True)
                verify()
                # Same-count swaps must not repaint pane labels merely because
                # all saved indexes still exist.
                swapped = [state[saved["windows"][0]["index"]]["panes"][index]["id"] for index in (1, 2)]
                before_swap_names = {pane: tmux_names.read_pane_names(pane) for pane in new_ids}
                tmux("swap-pane", "-d", "-s", swapped[0], "-t", swapped[1])
                try:
                    restore.recreate_tmux(saved, adopt_restored=True)
                except CommandError as error:
                    assert "ambiguous saved naming identity" in str(error)
                else:
                    raise AssertionError("same-count swapped panes must be rejected")
                assert before_swap_names == {pane: tmux_names.read_pane_names(pane) for pane in new_ids}
                tmux("swap-pane", "-d", "-s", swapped[0], "-t", swapped[1])
                verify()
                # An inserted pane can renumber saved panes. Refuse ambiguous
                # reconciliation without changing any live pane's name.
                target = state[saved["windows"][0]["index"]]["id"]
                extra = tmux("split-window", "-d", "-t", target, "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                tmux_names.restore_pane_names(extra, target, {"label": "UNSAVED_PANE"})
                before_names = {pane: tmux_names.read_pane_names(pane) for pane in new_ids | {extra}}
                try:
                    restore.recreate_tmux(saved, adopt_restored=True)
                except CommandError as error:
                    assert "additional live panes" in str(error)
                else:
                    raise AssertionError("ambiguous pane mapping must be rejected")
                assert before_names == {pane: tmux_names.read_pane_names(pane) for pane in new_ids | {extra}}
                assert tmux_names.read_pane_names(extra)["label"] == "UNSAVED_PANE"
                after_ids = {p["id"] for w in restore._tmux_state(actual).values() for p in w["panes"].values()}
                assert after_ids == new_ids | {extra}, "repeated restore created duplicates"
                assert storage.load()["sessions"][0] == saved
                # Replace one saved pane while retaining the pane count. Its
                # replacement's explicit name must also survive refusal.
                tmux("kill-pane", "-t", extra)
                last = restore._tmux_state(actual)[saved["windows"][0]["index"]]["panes"][5]["id"]
                tmux("kill-pane", "-t", last)
                new_ids.remove(last)
                previous = restore._tmux_state(actual)[saved["windows"][0]["index"]]["panes"][4]["id"]
                tmux("select-layout", "-t", target, "tiled")
                replacement = tmux("split-window", "-d", "-t", previous, "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                new_ids.add(replacement)
                tmux_names.restore_pane_names(replacement, target, {"label": "UNSAVED_REPLACEMENT"})
                before_replacement_names = {pane: tmux_names.read_pane_names(pane) for pane in new_ids}
                try:
                    restore.recreate_tmux(saved, adopt_restored=True)
                except CommandError as error:
                    assert "ambiguous saved naming identity" in str(error)
                else:
                    raise AssertionError("same-count replacement must be rejected")
                assert before_replacement_names == {pane: tmux_names.read_pane_names(pane) for pane in new_ids}
                print(json.dumps({"saved_panes": len(old_ids), "fresh_restore": True,
                                  "resurrect_adoption": True, "literal_names": True,
                                  "five_pane_asymmetric_layout": True, "active_pane_preserved": True,
                                  "nonzero_pane_base_preserved": True,
                                  "application_osc_title_preserves_label": True, "rename_policies_preserved": True,
                                  "same_count_swaps_preserved": True, "same_count_replacements_preserved": True,
                                  "extra_panes_preserved": True, "debug_window_name_preserved": True,
                                  "no_duplicate_panes": True, "user_tmux_untouched": True}))
        finally:
            if socket_path.exists():
                execute(["tmux", "kill-server"], check=False)


if __name__ == "__main__":
    main()
