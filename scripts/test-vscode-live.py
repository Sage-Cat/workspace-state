#!/usr/bin/env python3
"""Opt-in checks on a separately launched disposable Code user-data directory.

Does not start/stop Code, touch the production recipe, or request OS shutdown.
The caller must launch test windows first and explicitly name their temporary
user-data directory. Only windows whose native PID has that exact command-line
argument are eligible for moving. Artifacts remain in the named test directory.
"""
from __future__ import annotations

import argparse
import copy
import json
import shlex
from pathlib import Path
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_state import vscode
from workspace_state.util import atomic_json


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--restore-saved", action="store_true")
    parser.add_argument("--minimize-first", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    if root.parent != Path("/tmp") or not root.name.startswith("wsctl-vscode-live."):
        parser.error("expected an explicit /tmp/wsctl-vscode-live.* fixture directory")
    data = str(root / "data")
    original_shell, original_states = vscode.capture_shell, vscode._states
    def owned(window):
        try:
            raw = Path(f"/proc/{window['pid']}/cmdline").read_bytes().rstrip(b"\0")
            argv = raw.split(b"\0")
            if len(argv) == 1:  # Electron's process.title flattens its argv.
                argv = [value.encode() for value in shlex.split(raw.decode())]
        except (OSError, KeyError):
            return False
        return b"--user-data-dir" in argv and data.encode() in argv
    def shell():
        value = original_shell()
        code_ids = {window["id"] for window in vscode.matching_windows(value)}
        return {**value, "windows": [window for window in value["windows"] if window["id"] not in code_ids or owned(window)]}
    def states(deadline):
        return [state for state in original_states(deadline) if state.get("user_data_dir") == data]
    before = original_shell()
    unrelated_ids = {window["id"] for window in before["windows"] if not owned(window)}
    with patch.object(vscode, "capture_shell", side_effect=shell), patch.object(vscode, "_states", side_effect=states):
        if args.restore_saved:
            recipe = json.loads((root / "recipe.json").read_text())
        else:
            recipe = vscode.capture_vscode()
            if len(recipe["windows"]) < 2:
                raise RuntimeError("Launch at least two fixture project windows first")
            monitors = before["monitors"]
            workspaces = before["workspaces"]
            for index, window in enumerate(recipe["windows"]):
                monitor = monitors[(index + 1) % len(monitors)]
                workspace = workspaces[index % len(workspaces)]
                geometry = {"x": monitor["x"] + 80, "y": monitor["y"] + 80, "width": 1100, "height": 760}
                window["placement"] = {
                    "workspace": workspace["index"], "workspace_name": workspace["name"],
                    "monitor": monitor["index"], "monitor_identity": monitor["identity"],
                    "monitor_intent": monitor["identity"], "monitor_geometry": monitor,
                    "geometry": geometry, "geometry_relative": {**geometry, "x": 80, "y": 80},
                    "state": "normal" if index % 2 == 0 else "maximized",
                }
            atomic_json(root / "recipe.json", recipe)
        if args.minimize_first:
            recipe["windows"][0]["placement"]["state"] = "minimized"
        original_command = vscode._command
        def command(item, *, native=False):
            return original_command(item, native=native) + ["--extensions-dir", str(root / "extensions")]
        with patch.object(vscode, "_command", side_effect=command):
            count = vscode.restore_vscode(recipe, timeout=45)
        after_first = {window["id"] for window in vscode.matching_windows(shell())}
        with patch.object(vscode, "launch_graphical_service", side_effect=AssertionError("Duplicate launch on repeated restore")):
            assert vscode.restore_vscode(recipe, timeout=30) == count
        after_second = {window["id"] for window in vscode.matching_windows(shell())}
        assert after_first == after_second, "native windows duplicated"
        captured = vscode.capture_vscode()
        assert len(captured["windows"]) == len(recipe["windows"])
        assert sorted(vscode._identity(window) for window in captured["windows"]) == sorted(vscode._identity(window) for window in recipe["windows"])
    after = original_shell()
    assert unrelated_ids.issubset({window["id"] for window in after["windows"]}), "unrelated window disappeared"
    result = {"verified_windows": count, "repeat_created_no_duplicates": True,
              "native_ids": sorted(after_second), "production_recipe_untouched": True,
              "unrelated_windows_preserved": True, "os_power_actions": False}
    atomic_json(root / "result.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
