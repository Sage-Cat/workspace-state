from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any

from .capture import codex_for_pane
from .desktop import move_window, place_by_pid, place_by_title, remap_monitor, remap_workspace
from .util import CommandError, run


@dataclass(frozen=True)
class RestoreResult:
    message: str
    success: bool = True


def _tmux_exists(name: str) -> bool:
    return subprocess.run(["tmux", "has-session", "-t", f"={name}:"], capture_output=True).returncode == 0


def _pane_command(pane: dict[str, Any]) -> list[str]:
    codex = pane.get("codex") or {}
    session_id = codex.get("session_id")
    if session_id:
        return ["codex", "resume", "--no-alt-screen", session_id]
    return [os.environ.get("SHELL", "/bin/sh")]


def _shell_join(command: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in command)


def _pane_shell_command(pane: dict[str, Any]) -> str:
    command = _pane_command(pane)
    shell = os.environ.get("SHELL", "/bin/sh")
    if (pane.get("codex") or {}).get("session_id"):
        # A failed or later-closed Codex process must not remove the pane and
        # cascade into lost windows or a lost tmux session.
        return f"{_shell_join(command)}; exec {_shell_join([shell])}"
    return f"exec {_shell_join(command)}"


def _tmux_state(name: str) -> dict[int, dict[str, Any]]:
    fields = "\t".join([
        "#{window_index}", "#{window_name}", "#{window_id}",
        "#{pane_index}", "#{pane_id}", "#{pane_pid}", "#{pane_current_path}",
        "#{pane_current_command}",
    ])
    try:
        output = run(["tmux", "list-panes", "-s", "-t", f"={name}:", "-F", fields])
    except CommandError:
        return {}
    windows: dict[int, dict[str, Any]] = {}
    for line in output.splitlines():
        parts = line.split("\t", 7)
        if len(parts) != 8:
            continue
        win_idx, win_name, win_id, pane_idx, pane_id, pane_pid, cwd, command = parts
        window = windows.setdefault(int(win_idx), {
            "name": win_name, "id": win_id, "panes": {},
        })
        window["panes"][int(pane_idx)] = {
            "id": pane_id, "pid": int(pane_pid), "cwd": cwd, "command": command,
        }
    return windows


def _codex_ids(session: dict[str, Any]) -> dict[tuple[int, int], str]:
    return {
        (int(window["index"]), int(pane["index"])): str(pane["codex"]["session_id"])
        for window in session.get("windows", []) for pane in window.get("panes", [])
        if pane.get("codex") and pane["codex"].get("session_id")
    }


def _live_codex_ids(state: dict[int, dict[str, Any]]) -> dict[tuple[int, int], str]:
    result = {}
    for window_index, window in state.items():
        for pane_index, pane in window["panes"].items():
            codex = codex_for_pane(pane["pid"], pane["cwd"])
            if codex and codex.get("session_id"):
                result[(int(window_index), int(pane_index))] = str(codex["session_id"])
    return result


def missing_codex_ids(session: dict[str, Any], actual_name: str) -> set[str]:
    expected = _codex_ids(session)
    if not expected:
        return set()
    live = _live_codex_ids(_tmux_state(actual_name))
    return {
        session_id for position, session_id in expected.items()
        if live.get(position) != session_id
    }


def _pristine_bootstrap(state: dict[int, dict[str, Any]]) -> bool:
    panes = [pane for window in state.values() for pane in window["panes"].values()]
    return (
        len(state) == 1 and len(panes) == 1
        and panes[0].get("command") in {"bash", "dash", "fish", "sh", "zsh"}
    )


def _same_tmux_session(
    session: dict[str, Any],
    state: dict[int, dict[str, Any]],
    *,
    repair_processes: bool = False,
    adopt_restored: bool = False,
) -> bool:
    saved_windows = {int(window["index"]): window for window in session.get("windows", [])}
    overlap = set(saved_windows).intersection(state)
    if not overlap:
        return False
    bootstrap = repair_processes and _pristine_bootstrap(state)
    if not bootstrap and any(saved_windows[index]["name"] != state[index]["name"] for index in overlap):
        return False
    saved_ids = _codex_ids(session)
    live_ids = _live_codex_ids(state)
    if saved_ids:
        # Never repair a same-named session unless its live Codex identities
        # prove that it belongs to this recipe. This avoids mutating an
        # unrelated numeric/default-named session.
        if live_ids:
            return all(saved_ids.get(position) == session_id for position, session_id in live_ids.items())
        exact_structure = (
            set(saved_windows) == set(state) and all(
                saved_windows[index]["name"] == state[index]["name"]
                and {
                    int(pane["index"]) for pane in saved_windows[index].get("panes", [])
                } == set(state[index]["panes"])
                for index in saved_windows
            )
        )
        if adopt_restored and exact_structure:
            return True
        if repair_processes:
            return bootstrap or exact_structure
        return False
    return (repair_processes and bootstrap) or all(
        saved_windows[index]["name"] == state[index]["name"] for index in overlap
    )


def _available_tmux_name(name: str) -> str:
    base = f"{name}-wsctl"
    candidate = base
    suffix = 2
    while _tmux_exists(candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _session_fingerprint(session: dict[str, Any]) -> str:
    identity = {
        "name": session.get("name"),
        "windows": [{
            "index": int(window["index"]),
            "name": window.get("name"),
            "panes": [{
                "index": int(pane["index"]),
                "cwd": pane.get("cwd"),
                "codex": (pane.get("codex") or {}).get("session_id"),
            } for pane in window.get("panes", [])],
        } for window in session.get("windows", [])],
    }
    raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _tagged_restore_session(saved_name: str, fingerprint: str) -> str | None:
    try:
        names = run(["tmux", "list-sessions", "-F", "#{session_name}"]).splitlines()
    except (CommandError, FileNotFoundError):
        return None
    prefix = f"{saved_name}-wsctl"
    for name in names:
        if name != prefix and not name.startswith(prefix + "-"):
            continue
        try:
            value = run([
                "tmux", "show-options", "-qv", "-t", f"={name}:",
                "@wsctl-restore-fingerprint",
            ]).strip()
        except (CommandError, FileNotFoundError):
            continue
        if value == fingerprint:
            return name
    return None


def _split_saved_pane(
    target: str,
    pane: dict[str, Any],
    *,
    dry_run: bool,
    placeholder: str,
) -> str:
    if dry_run:
        return placeholder
    return run([
        "tmux", "split-window", "-d", "-t", target, "-c", pane["cwd"],
        "-P", "-F", "#{pane_id}", _pane_shell_command(pane),
    ]).strip()


def _finish_window(
    target: str,
    window: dict[str, Any],
    pane_targets: dict[int, str],
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    if len(window.get("panes", [])) > 1 and window.get("layout"):
        # Layout strings are generated by tmux itself; tmux validates them.
        run(["tmux", "select-layout", "-t", target, window["layout"]], check=False)
    active = next((pane for pane in window.get("panes", []) if pane.get("active")), None)
    if active and int(active["index"]) in pane_targets:
        run(["tmux", "select-pane", "-t", pane_targets[int(active["index"])]])


def _create_window(
    name: str,
    window: dict[str, Any],
    *,
    dry_run: bool,
) -> str:
    first_pane = window["panes"][0]
    command = [
        "tmux", "new-window", "-d", "-t", f"={name}:{int(window['index'])}",
        "-n", window["name"], "-c", first_pane["cwd"],
        "-P", "-F", "#{window_id}", _pane_shell_command(first_pane),
    ]
    target = f"={name}:<window-{window['index']}>" if dry_run else run(command).strip()
    pane_targets = {
        int(first_pane["index"]): (
            f"{target}.<pane-{first_pane['index']}>"
            if dry_run else run(["tmux", "display-message", "-p", "-t", target, "#{pane_id}"]).strip()
        )
    }
    for pane in window["panes"][1:]:
        pane_targets[int(pane["index"])] = _split_saved_pane(
            target, pane, dry_run=dry_run,
            placeholder=f"{target}.<pane-{pane['index']}>",
        )
    _finish_window(target, window, pane_targets, dry_run=dry_run)
    if not dry_run:
        run(["tmux", "rename-window", "-t", target, window["name"]])
    return target


def _reconcile_tmux(
    name: str,
    session: dict[str, Any],
    state: dict[int, dict[str, Any]],
    *,
    dry_run: bool,
    repair_processes: bool,
) -> list[str]:
    actions = [f"reuse tmux session {name}"]
    active_target = None
    for window in session.get("windows", []):
        index = int(window["index"])
        current = state.get(index)
        if current is None:
            target = _create_window(name, window, dry_run=dry_run)
            actions.append(f"restore missing tmux window {name}:{index} ({window['name']})")
        else:
            target = current["id"]
            if repair_processes and current["name"] != window["name"]:
                if not dry_run:
                    run(["tmux", "rename-window", "-t", target, window["name"]])
                actions.append(f"rename bootstrap window {name}:{index} to {window['name']}")
            pane_targets = {
                int(pane_index): pane["id"]
                for pane_index, pane in current["panes"].items()
            }
            for pane in window.get("panes", []):
                pane_index = int(pane["index"])
                if pane_index in pane_targets:
                    current_pane = current["panes"][pane_index]
                    session_id = (pane.get("codex") or {}).get("session_id")
                    live_codex = (
                        codex_for_pane(current_pane["pid"], current_pane["cwd"])
                        if repair_processes and session_id else None
                    )
                    if (
                        repair_processes and session_id and live_codex is None
                        and current_pane.get("command") in {"bash", "dash", "fish", "sh", "zsh"}
                    ):
                        if not dry_run:
                            run([
                                "tmux", "send-keys", "-t", current_pane["id"],
                                _shell_join(_pane_command(pane)), "C-m",
                            ])
                        actions.append(f"resume Codex {session_id} in {name}:{index}.{pane_index}")
                    continue
                pane_targets[pane_index] = _split_saved_pane(
                    target, pane, dry_run=dry_run,
                    placeholder=f"{target}.<pane-{pane_index}>",
                )
                actions.append(f"restore missing tmux pane {name}:{index}.{pane_index}")
            if len(pane_targets) == len(window.get("panes", [])):
                _finish_window(target, window, pane_targets, dry_run=dry_run)
        if window.get("active"):
            active_target = target
    if active_target and not dry_run:
        run(["tmux", "select-window", "-t", active_target])
    return actions


def recreate_tmux(
    session: dict[str, Any],
    *,
    dry_run: bool = False,
    repair_processes: bool = False,
    adopt_restored: bool = False,
) -> tuple[str, list[str]]:
    saved_name = session["name"]
    windows = session.get("windows", [])
    if not windows:
        return saved_name, [f"skip empty tmux session {saved_name}"]

    name = saved_name
    fingerprint = _session_fingerprint(session)
    actions: list[str] = []
    previous_restore = _tagged_restore_session(saved_name, fingerprint)
    if not _tmux_exists(name) and previous_restore:
        state = _tmux_state(previous_restore)
        if _same_tmux_session(
            session, state,
            repair_processes=repair_processes,
            adopt_restored=adopt_restored,
        ):
            return previous_restore, _reconcile_tmux(
                previous_restore, session, state,
                dry_run=dry_run,
                repair_processes=repair_processes,
            )
    if _tmux_exists(name):
        state = _tmux_state(name)
        if _same_tmux_session(
            session, state,
            repair_processes=repair_processes,
            adopt_restored=adopt_restored,
        ):
            return name, _reconcile_tmux(
                name, session, state,
                dry_run=dry_run,
                repair_processes=repair_processes,
            )
        if previous_restore:
            state = _tmux_state(previous_restore)
            if _same_tmux_session(
                session, state,
                repair_processes=repair_processes,
                adopt_restored=adopt_restored,
            ):
                return previous_restore, _reconcile_tmux(
                    previous_restore, session, state,
                    dry_run=dry_run,
                    repair_processes=repair_processes,
                )
        name = _available_tmux_name(saved_name)
        actions.append(f"tmux name {saved_name} is in use; restore as {name}")

    first_window = windows[0]
    first_pane = first_window["panes"][0]
    command = [
        "tmux", "new-session", "-d", "-s", name,
        "-n", first_window["name"], "-c", first_pane["cwd"],
        _pane_shell_command(first_pane),
    ]
    actions.append("create " + name)
    if not dry_run:
        run(command)
        run([
            "tmux", "set-option", "-q", "-t", f"={name}:",
            "@wsctl-restore-fingerprint", fingerprint,
        ])
        target = run(["tmux", "display-message", "-p", "-t", f"={name}:", "#{window_id}"]).strip()
        actual_index = int(run([
            "tmux", "display-message", "-p", "-t", target, "#{window_index}",
        ]).strip())
        saved_index = int(first_window["index"])
        if actual_index != saved_index:
            run(["tmux", "move-window", "-s", target, "-t", f"={name}:{saved_index}"])
    else:
        target = f"={name}:<first-window>"

    pane_targets = {
        int(first_pane["index"]): (
            f"{target}.<pane-{first_pane['index']}>"
            if dry_run else run(["tmux", "display-message", "-p", "-t", target, "#{pane_id}"]).strip()
        )
    }
    for pane in first_window["panes"][1:]:
        pane_targets[int(pane["index"])] = _split_saved_pane(
            target, pane, dry_run=dry_run,
            placeholder=f"{target}.<pane-{pane['index']}>",
        )
    _finish_window(target, first_window, pane_targets, dry_run=dry_run)
    active_target = target if first_window.get("active") else None

    for window in windows[1:]:
        target = _create_window(name, window, dry_run=dry_run)
        if window.get("active"):
            active_target = target
    if active_target and not dry_run:
        run(["tmux", "select-window", "-t", active_target])
    return name, actions


def launch_terminal(session: dict[str, Any], *, place: bool = True, dry_run: bool = False) -> RestoreResult:
    name = session["name"]
    title = f"wsctl:{name}:{uuid.uuid4().hex[:8]}"
    command = [
        "/usr/bin/alacritty", "--title", title, "-o", "window.dynamic_title=false",
        "-e", "tmux", "attach-session", "-t", f"={name}:",
    ]
    if dry_run:
        return RestoreResult("launch " + " ".join(shlex.quote(item) for item in command))
    process = subprocess.Popen(
        command, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    placement = session.get("placement")
    if place and placement:
        placement = remap_monitor(remap_workspace(placement))
        for _ in range(30):
            if process.poll() is not None:
                return RestoreResult(f"Alacritty for {name} exited before attaching", False)
            if place_by_title(title, placement):
                return RestoreResult(f"launched {name}")
            time.sleep(0.1)
        return RestoreResult(f"launched {name} (window placement failed)", False)
    time.sleep(0.2)
    if process.poll() is not None:
        return RestoreResult(f"Alacritty for {name} exited before attaching", False)
    return RestoreResult(f"launched {name}")


def place_terminal(client: dict[str, Any], placement: dict[str, Any], *, dry_run: bool = False) -> RestoreResult:
    """Place an already attached Alacritty window instead of launching a duplicate."""
    session = str(client.get("session") or "terminal")
    if dry_run:
        return RestoreResult(f"place existing Alacritty for {session}")
    target = remap_monitor(remap_workspace(placement))
    live_placement = client.get("placement") or {}
    window_id = live_placement.get("id")
    if window_id is not None and move_window(int(window_id), target):
        return RestoreResult(f"placed existing Alacritty for {session}")
    pid = client.get("alacritty_pid")
    if pid is not None and place_by_pid(int(pid), target):
        return RestoreResult(f"placed existing Alacritty for {session}")
    title = live_placement.get("title")
    if title and place_by_title(str(title), target):
        return RestoreResult(f"placed existing Alacritty for {session}")
    return RestoreResult(f"kept existing Alacritty for {session} (window placement failed)", False)
