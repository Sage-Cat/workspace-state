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
from .codex_resume import resumed_session
from .desktop import (
    serialized_placement,
    capture_shell,
    move_window_result,
    remap_monitor,
    remap_workspace,
)
from .util import CommandError, launch_graphical_service, run
from .tmux_names import restore_pane_names


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
        return ["wsctl-codex-resume", session_id]
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


def _live_codex_ids(
    state: dict[int, dict[str, Any]], *, require_ready: bool = False,
) -> dict[tuple[int, int], str]:
    result = {}
    for window_index, window in state.items():
        for pane_index, pane in window["panes"].items():
            codex = codex_for_pane(pane["pid"], pane["cwd"])
            if codex and codex.get("session_id"):
                if require_ready and codex.get("confidence") == "restore-wrapper":
                    continue
                if require_ready and not resumed_session(
                    codex["pid"], str(codex["session_id"]), pane.get("id", ""),
                ):
                    continue
                result[(int(window_index), int(pane_index))] = str(codex["session_id"])
    return result


def missing_codex_ids(session: dict[str, Any], actual_name: str) -> set[str]:
    expected = _codex_ids(session)
    if not expected:
        return set()
    live = _live_codex_ids(_tmux_state(actual_name), require_ready=True)
    return {
        session_id for position, session_id in expected.items()
        if live.get(position) != session_id
    }


def _pristine_bootstrap(state: dict[int, dict[str, Any]]) -> bool:
    # A shell process/name/layout cannot prove an empty input buffer. Newly
    # created wsctl panes launch their command directly; existing shells never
    # become an authorized command channel just because they look idle.
    return False


def _same_tmux_session(
    session: dict[str, Any],
    state: dict[int, dict[str, Any]],
    *,
    repair_processes: bool = False,
    adopt_restored: bool = False,
    trusted_recipe: bool = False,
) -> bool:
    saved_windows = {int(window["index"]): window for window in session.get("windows", [])}
    overlap = set(saved_windows).intersection(state)
    if not overlap:
        return False
    bootstrap = repair_processes and _pristine_bootstrap(state)
    saved_ids = _codex_ids(session)
    live_ids = _live_codex_ids(state)
    exact_structure = (
        set(saved_windows) == set(state) and all(
            {
                int(pane["index"])
                for pane in saved_windows[index].get("panes", [])
            } == set(state[index]["panes"])
            and all(
                str(pane.get("cwd") or "")
                == str(state[index]["panes"][int(pane["index"])].get("cwd") or "")
                for pane in saved_windows[index].get("panes", [])
            )
            for index in saved_windows
        )
    )
    if saved_ids:
        # Never repair a same-named session unless its live Codex identities
        # prove that it belongs to this recipe. This avoids mutating an
        # unrelated numeric/default-named session.
        if live_ids:
            return all(saved_ids.get(position) == session_id for position, session_id in live_ids.items())
        if trusted_recipe:
            return True
        if adopt_restored and exact_structure:
            return True
        if not bootstrap and any(
            saved_windows[index]["name"] != state[index]["name"]
            for index in overlap
        ):
            return False
        named_structure = exact_structure and all(
            saved_windows[index]["name"] == state[index]["name"]
            for index in saved_windows
        )
        if repair_processes:
            return bootstrap or named_structure
        return False
    if trusted_recipe:
        return True
    if adopt_restored and exact_structure:
        return True
    if not bootstrap and any(
        saved_windows[index]["name"] != state[index]["name"]
        for index in overlap
    ):
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
        if _restore_fingerprint(name) == fingerprint:
            return name
    return None


def _restore_fingerprint(name: str) -> str | None:
    try:
        value = run([
            "tmux", "show-options", "-qv", "-t", f"={name}:",
            "@wsctl-restore-fingerprint",
        ]).strip()
    except (CommandError, FileNotFoundError):
        return None
    return value or None


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
    _restore_window_pane_names(target, window, pane_targets)
    if len(window.get("panes", [])) > 1 and window.get("layout"):
        # Layout strings are generated by tmux itself; tmux validates them.
        run(["tmux", "select-layout", "-t", target, window["layout"]], check=False)
    active = next((pane for pane in window.get("panes", []) if pane.get("active")), None)
    if active and int(active["index"]) in pane_targets:
        run(["tmux", "select-pane", "-t", pane_targets[int(active["index"])]])


def _restore_window_pane_names(
    target: str, window: dict[str, Any], pane_targets: dict[int, str],
) -> None:
    for pane in window.get("panes", []):
        pane_id = pane_targets.get(int(pane["index"]))
        if pane_id is not None:
            # Snapshot pane IDs belong to the old tmux server. Use only the
            # IDs returned by creation/reconciliation in this restore attempt.
            restore_pane_names(pane_id, target, pane)


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
    # Extra panes can shift indexes without changing window names or cwd.
    # Refuse before mutation instead of naming an unrelated inserted pane.
    for window in session.get("windows", []):
        current = state.get(int(window["index"]))
        saved_indexes = {int(pane["index"]) for pane in window.get("panes", [])}
        has_names = any("label" in pane or "title" in pane for pane in window.get("panes", []))
        if current and has_names and set(current["panes"]) - saved_indexes:
            raise CommandError(
                f"Tmux window {name}:{window['index']} has additional live panes; "
                "refusing to guess saved pane names by index (save the updated layout first)"
            )
        if current and repair_processes:
            for pane in window.get("panes", []):
                live = current["panes"].get(int(pane["index"]))
                if live and (pane.get("codex") or {}).get("session_id") and live.get("command") in {"bash", "dash", "fish", "sh", "zsh"}:
                    if codex_for_pane(live["pid"], live["cwd"]) is None:
                        raise CommandError(
                            f"Tmux pane {live['id']} has unverified shell input; "
                            "preserved without submitting a resume command"
                        )
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
                        raise CommandError(f"Tmux pane {current_pane['id']} has unverified shell input")
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
            trusted_recipe=True,
        ):
            return previous_restore, _reconcile_tmux(
                previous_restore, session, state,
                dry_run=dry_run,
                repair_processes=repair_processes,
            )
    if _tmux_exists(name):
        state = _tmux_state(name)
        exact_tag = _restore_fingerprint(name) == fingerprint
        if _same_tmux_session(
            session, state,
            repair_processes=repair_processes,
            adopt_restored=adopt_restored,
            trusted_recipe=exact_tag,
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
                trusted_recipe=True,
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


def _terminal_target(placement: dict[str, Any]) -> dict[str, Any]:
    shell = capture_shell()
    if not shell.get("available"):
        raise CommandError("GNOME window state is unavailable")
    name = placement.get("workspace_name")
    if name and sum(item.get("name") == name for item in shell.get("workspaces", [])) != 1:
        raise CommandError(f"saved workspace is unavailable or ambiguous: {name}")
    target = remap_monitor(
        remap_workspace(placement),
        require_identity=bool(placement.get("monitor_intent") or placement.get("monitor_identity")),
    )
    return dict(target, state=target.get("state") or "normal")


def _terminal_placement_matches(window: dict[str, Any], target: dict[str, Any]) -> bool:
    if any(window.get(key) != target.get(key) for key in ("workspace", "monitor", "state")):
        return False
    # Maximized/fullscreen dimensions belong to the current monitor work area,
    # which may differ from the saved dock/panel geometry. Normal windows retain
    # their saved frame, allowing only one pixel of compositor rounding.
    if target.get("state") == "normal" and target.get("geometry"):
        return all(
            isinstance((window.get("geometry") or {}).get(key), (int, float))
            and abs(window["geometry"][key] - value) <= 1
            for key, value in target["geometry"].items()
        )
    return True


@serialized_placement
def _verify_terminal_placement(selector: dict[str, Any], target: dict[str, Any], *, timeout: float = 12) -> None:
    """Use one exact Alacritty; accepted/deferred requests are not completion."""
    deadline = time.monotonic() + timeout
    stable_since = None
    last_move = float("-inf")
    staging = None
    detail = "the Alacritty window has not appeared"
    while time.monotonic() < deadline:
        shell = capture_shell()
        if not shell.get("available"):
            raise CommandError("GNOME window state became unavailable")
        candidates = [
            window for window in shell.get("windows", [])
            if "alacritty" in {
                str(value).casefold() for value in [
                    *window.get("app_ids", []), window.get("app_id"), window.get("wm_class"),
                ]
            }
            and all(window.get(key) == value for key, value in selector.items())
        ]
        if len(candidates) > 1:
            raise CommandError("Alacritty window selector is ambiguous; no windows moved")
        if not candidates:
            if "id" in selector:
                detail = f"window {selector['id']} disappeared; no matching Alacritty remains"
            stable_since = None
            time.sleep(.1)
            continue
        window = candidates[0]
        # Pin the selected Shell window for this attempt; never retarget another
        # terminal if it closes or its dynamic title changes during placement.
        selector = {"id": window["id"], "pid": window["pid"]}
        now = time.monotonic()
        expected = staging or target
        if _terminal_placement_matches(window, expected):
            if stable_since is None:
                stable_since = now
            if now - stable_since >= (.4 if staging else 1):
                if not staging:
                    return
                result = move_window_result(window["id"], target)
                if not result.get("placed"):
                    raise CommandError(f"GNOME rejected final Alacritty placement: {result}")
                staging = None
                stable_since = None
                last_move = now
        else:
            stable_since = None
            if now - last_move >= 1:
                active = shell.get("active_workspace")
                if not isinstance(active, int):
                    raise CommandError("GNOME active workspace is unavailable")
                staging = dict(target, workspace=active) if active != target["workspace"] else None
                if staging:
                    staging.pop("workspace_name", None)
                result = move_window_result(window["id"], staging or target)
                if not result.get("placed"):
                    raise CommandError(f"GNOME rejected Alacritty placement: {result}")
                last_move = now
        detail = (
            f"window {window['id']}: expected workspace {target.get('workspace_name') or target['workspace']}, "
            f"monitor {target['monitor']}, {target['state']}, geometry {target.get('geometry')}; "
            f"observed workspace {window.get('workspace')}, monitor {window.get('monitor')}, "
            f"{window.get('state')}, geometry {window.get('geometry')}"
        )
        time.sleep(.1)
    raise CommandError(f"Alacritty placement verification timed out: {detail}")


def launch_terminal(session: dict[str, Any], *, place: bool = True, dry_run: bool = False) -> RestoreResult:
    name = session["name"]
    title = f"wsctl:{name}:{uuid.uuid4().hex[:8]}"
    command = [
        "/usr/bin/alacritty", "--title", title, "-o", "window.dynamic_title=false",
        "-e", "tmux", "attach-session", "-t", f"={name}:",
    ]
    if dry_run:
        return RestoreResult("launch " + " ".join(shlex.quote(item) for item in command))
    try:
        placement = session.get("placement")
        target = _terminal_target(placement) if place and placement else None
        launch_graphical_service(command, f"alacritty-{name}")
        if target:
            _verify_terminal_placement({"title": title}, target)
    except CommandError as error:
        return RestoreResult(f"Alacritty for {name}: {error}", False)
    return RestoreResult(f"launched {name}" + ("; placement verified" if target else ""))


def place_terminal(client: dict[str, Any], placement: dict[str, Any], *, dry_run: bool = False) -> RestoreResult:
    """Place an already attached Alacritty window instead of launching a duplicate."""
    session = str(client.get("session") or "terminal")
    if dry_run:
        return RestoreResult(f"place existing Alacritty for {session}")
    live_placement = client.get("placement") or {}
    window_id = live_placement.get("id")
    pid = client.get("alacritty_pid")
    if window_id is not None:
        selector = {"id": int(window_id)}
        if pid is not None:
            selector["pid"] = int(pid)
    elif pid is not None:
        selector = {"pid": int(pid)}
    elif live_placement.get("title"):
        selector = {"title": str(live_placement["title"])}
    else:
        return RestoreResult(f"Alacritty for {session}: no exact window identity", False)
    try:
        _verify_terminal_placement(selector, _terminal_target(placement))
    except CommandError as error:
        return RestoreResult(f"Alacritty for {session}: {error}", False)
    return RestoreResult(f"verified existing Alacritty placement for {session}")
