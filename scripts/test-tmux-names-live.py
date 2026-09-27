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
                second = tmux("split-window", "-d", "-t", window, "-c", str(root),
                              "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                third = tmux("new-window", "-d", "-t", "=saved:", "-n", "project tab", "-c", str(root),
                             "-P", "-F", "#{pane_id}", "/bin/sleep 300").strip()
                names = [
                    {"label": "Перевірка #{pane_id} #(printf literal) ;", "title": "volatile app title"},
                    {"label": "", "title": "  spaces and Україна  "},
                    {"label": None, "title": "literal #{pane_id};"},
                ]
                for pane, naming in zip((first, second, third), names):
                    tmux_names.restore_pane_names(pane, window_for(pane), naming)
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
                tmux_names.restore_pane_names(sentinel, window_for(sentinel), {"label": "KEEP_THIS_PANE"})
                actual, _ = restore.recreate_tmux(saved)
                assert actual == "saved"
                state = restore._tmux_state(actual)
                new_ids = {p["id"] for w in state.values() for p in w["panes"].values()}
                assert old_ids != new_ids, "test must exercise pane ID remapping"

                def verify():
                    state = restore._tmux_state(actual)
                    assert [w["name"] for w in state.values()] == old_windows
                    for saved_window in saved["windows"]:
                        live_window = state[saved_window["index"]]
                        for saved_pane in saved_window["panes"]:
                            live_id = live_window["panes"][saved_pane["index"]]["id"]
                            naming = tmux_names.read_pane_names(live_id, window_id=live_window["id"])
                            assert naming["label"] == saved_pane["label"], (naming, saved_pane)
                            assert naming["title"] == (saved_pane["label"] or saved_pane["title"])
                    assert tmux_names.read_pane_names(sentinel)["label"] == "KEEP_THIS_PANE"
                    assert tmux("display-message", "-p", "-t", sentinel, "#{window_name}").strip() == "KEEP_THIS_TAB"
                    return state

                verify()
                # Simulate independently resurrected windows: no wsctl tag, and
                # pane labels missing although the saved layout is already live.
                tmux("set-option", "-u", "-t", "=saved:", "@wsctl-restore-fingerprint")
                for pane in new_ids:
                    tmux("set-option", "-p", "-u", "-t", pane, "@pane_label")
                restore.recreate_tmux(saved, adopt_restored=True)
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
                print(json.dumps({"saved_panes": len(old_ids), "fresh_restore": True,
                                  "resurrect_adoption": True, "literal_names": True,
                                  "extra_panes_preserved": True, "debug_window_name_preserved": True,
                                  "no_duplicate_panes": True, "user_tmux_untouched": True}))
        finally:
            if socket_path.exists():
                execute(["tmux", "kill-server"], check=False)


if __name__ == "__main__":
    main()
