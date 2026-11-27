"""GNOME login restore and the bounded final save at shutdown."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from contextvars import copy_context
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

from .desktop import (
    DESKTOP_REQUIRED_CAPABILITIES,
    capture_shell,
    desktop_readiness,
    desktop_topology_signature,
)
from .login_status import (
    append_diagnostic,
    claim_startup_hud,
    fail_active,
    initialize as initialize_login_status,
    status_path,
    update_stage,
)
from . import operations
from .session_ending import clear_marker, write_marker

SESSION_BUS_NAME = "org.gnome.SessionManager"
SESSION_OBJECT = "/org/gnome/SessionManager"
SESSION_INTERFACE = "org.gnome.SessionManager"
CLIENT_INTERFACE = "org.gnome.SessionManager.ClientPrivate"
APP_ID = "org.sagecat.WorkspaceState"
DBUS_BUS_NAME = "org.freedesktop.DBus"
DBUS_OBJECT = "/org/freedesktop/DBus"
DBUS_INTERFACE = "org.freedesktop.DBus"
SYSTEMD_BUS_NAME = "org.freedesktop.systemd1"
SYSTEMD_OBJECT = "/org/freedesktop/systemd1"
SYSTEMD_INTERFACE = "org.freedesktop.systemd1.Manager"
SYSTEMD_UNIT_INTERFACE = "org.freedesktop.systemd1.Unit"
LOGIN1_BUS_NAME = "org.freedesktop.login1"
LOGIN1_OBJECT = "/org/freedesktop/login1"
LOGIN1_MANAGER_INTERFACE = "org.freedesktop.login1.Manager"
GRAPHICAL_SESSION_TARGET = "graphical-session.target"
DESKTOP_SETTLE_SECONDS = 2.0
LOGIN1_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
DEFAULT_INHIBIT_DELAY_SECONDS = 5.0
FINAL_SAVE_BUDGET_CAP_SECONDS = 20.0
FINAL_SAVE_BUDGET_RESERVE_SECONDS = 3.0
FINAL_SAVE_KILL_GRACE_SECONDS = 1.0

GRAPHICAL_ENVIRONMENT = (
    "WAYLAND_DISPLAY",
    "DISPLAY",
    "XDG_RUNTIME_DIR",
    "XDG_SESSION_TYPE",
    "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP",
    "DESKTOP_SESSION",
    "GNOME_SHELL_SESSION_MODE",
)


def final_save_budget(inhibit_delay_max_seconds: float) -> float:
    """Return the final save budget inside logind's delay window."""
    return max(
        1.0,
        min(
            FINAL_SAVE_BUDGET_CAP_SECONDS,
            inhibit_delay_max_seconds - FINAL_SAVE_BUDGET_RESERVE_SECONDS,
        ),
    )


class DelayInhibitor:
    """Hold a logind delay lock so a power-off waits for the final save."""

    def __init__(self, connection: Gio.DBusConnection) -> None:
        self.connection = connection
        self._fd: int | None = None

    @property
    def active(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        reply, descriptors = self.connection.call_with_unix_fd_list_sync(
            LOGIN1_BUS_NAME,
            LOGIN1_OBJECT,
            LOGIN1_MANAGER_INTERFACE,
            "Inhibit",
            GLib.Variant(
                "(ssss)",
                (
                    "shutdown",
                    "workspace-state",
                    "Saving the workspace before power-off",
                    "delay",
                ),
            ),
            GLib.VariantType.new("(h)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
            None,
        )
        handle = int(reply.unpack()[0])
        if descriptors is None:
            raise RuntimeError("logind returned no shutdown inhibitor descriptor list")
        descriptor = descriptors.get(handle)
        if descriptor < 0:
            raise RuntimeError("logind returned an invalid shutdown inhibitor descriptor")
        self._fd = descriptor

    def release(self) -> None:
        descriptor, self._fd = self._fd, None
        if descriptor is not None:
            os.close(descriptor)


class GnomeSessionClient:
    """Keep wsctl attached to one GNOME session lifecycle."""

    def __init__(
        self,
        connection: Gio.DBusConnection,
        loop: GLib.MainLoop,
        bin_dir: Path,
        delay_inhibitor: DelayInhibitor | None = None,
        system_connection: Gio.DBusConnection | None = None,
    ) -> None:
        self.connection = connection
        self.loop = loop
        self.bin_dir = bin_dir
        self._delay_inhibitor = delay_inhibitor
        self._system_connection = system_connection
        self.client_path: str | None = None
        self.subscription_id: int | None = None
        self._prepare_subscription_id: int | None = None
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        # Final save state for the current shutdown attempt.
        self._final_started = False
        self._final_process: subprocess.Popen[bytes] | None = None
        self._final_started_at = 0.0
        self._final_deadline_source: int | None = None
        self._final_kill_source: int | None = None
        self._final_timed_out = False
        self._end_session_pending = False
        self._rearm_after_final = False
        self._quit_after_final = False
        self._startup_completed = False
        self._startup_quiescence_pending = False
        self._operation_context: operations.OperationContext | None = None
        self._placement_progress_pid: int | None = None
        self._placement_progress_next = 0.0
        self._placement_progress_finished = False
        self._login_generation: str | None = None
        self._environment_deadline = time.monotonic() + 120
        self._environment_wait_reason: str | None = None
        self._graphical_target_path: str | None = None
        self._desktop_ready_since: float | None = None
        self._desktop_signature: str | None = None
        self._status_gnome_ready = False
        self.exit_code = 0

    def register(self) -> None:
        # A new agent is a new login; an earlier end-of-session marker is stale.
        clear_marker()
        self._acquire_delay_inhibitor()
        startup_id = os.environ.get("DESKTOP_AUTOSTART_ID", "")
        result = self.connection.call_sync(
            SESSION_BUS_NAME,
            SESSION_OBJECT,
            SESSION_INTERFACE,
            "RegisterClient",
            GLib.Variant("(ss)", (APP_ID, startup_id)),
            GLib.VariantType.new("(o)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        self.client_path = str(result.unpack()[0])
        self.subscription_id = self.connection.signal_subscribe(
            SESSION_BUS_NAME,
            CLIENT_INTERFACE,
            None,
            self.client_path,
            None,
            Gio.DBusSignalFlags.NONE,
            self._on_signal,
            None,
        )
        if self._system_connection is not None:
            self._prepare_subscription_id = self._system_connection.signal_subscribe(
                LOGIN1_BUS_NAME,
                LOGIN1_MANAGER_INTERFACE,
                "PrepareForShutdown",
                LOGIN1_OBJECT,
                None,
                Gio.DBusSignalFlags.NONE,
                self._on_prepare_for_shutdown,
                None,
            )
        owner_result = self.connection.call_sync(
            DBUS_BUS_NAME,
            DBUS_OBJECT,
            DBUS_INTERFACE,
            "GetNameOwner",
            GLib.Variant("(s)", (SESSION_BUS_NAME,)),
            GLib.VariantType.new("(s)"),
            Gio.DBusCallFlags.NONE,
            2000,
            None,
        )
        owner = str(owner_result.unpack()[0])
        generation = self._record_login_generation(owner)
        self._login_generation = generation
        try:
            from .deployment import record_coordinator_build
            record_coordinator_build(generation)
        except (OSError, ValueError) as error:
            append_diagnostic("coordinator build identity", str(error))
        if self._reattach_completed_startup(generation):
            print("wsctl: reattached to completed login; final save is armed", flush=True)
            return
        show_startup_hud = claim_startup_hud(generation)
        initialize_login_status(
            generation,
            show_startup_hud=show_startup_hud,
        )
        self._operation_context = operations.current()
        if not show_startup_hud:
            print(
                "wsctl: startup HUD already shown during this OS boot; "
                "restoring this login in the background",
                flush=True,
            )
        update_stage("gnome", "running", "Waiting for GNOME Wayland session")
        update_stage("displays", "waiting", "Waiting for compositor display readiness")
        print(f"wsctl: registered GNOME session client {self.client_path}", flush=True)

    def _acquire_delay_inhibitor(self) -> None:
        if self._delay_inhibitor is None:
            return
        try:
            self._delay_inhibitor.acquire()
        except (GLib.Error, OSError, RuntimeError) as error:
            # Without the lock the periodic checkpoint remains the floor.
            print(f"wsctl: could not take the logind delay lock: {error}",
                  file=sys.stderr, flush=True)

    def _release_delay_inhibitor(self, *, announce: bool = False) -> None:
        inhibitor = self._delay_inhibitor
        if inhibitor is None:
            return
        was_active = inhibitor.active
        inhibitor.release()
        if announce and was_active:
            print("wsctl: logind delay lock released", flush=True)

    def _reattach_completed_startup(self, session_id: str) -> bool:
        """Keep a coordinator restart from replaying this login's completed work."""
        try:
            descriptor = os.open(status_path(), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                document = json.load(stream)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
                or not isinstance(document, dict)
                or document.get("schema_version") != 1
                or document.get("mode") != "startup"
                or document.get("session_id") != session_id
                or document.get("operation_state") != "completed"
            ):
                return False
            context = operations.OperationContext.from_dict(document.get("operation_context"))
            if context.boot_id != operations.boot_id() or not context.matches(document):
                return False
        except (OSError, TypeError, ValueError):
            return False
        # Completion is durable for this login even after its original work
        # deadline. Bind it for identity only; new shutdowns get new authority.
        self._operation_context = context
        operations.bind(context)
        self._startup_completed = True
        self._placement_progress_finished = True
        return True

    @staticmethod
    def _record_login_generation(session_manager_owner: str) -> str:
        generation = hashlib.sha256(session_manager_owner.encode()).hexdigest()[:16]
        runtime_root = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        ) / "workspace-state"
        runtime_root.mkdir(parents=True, exist_ok=True)
        runtime_root.chmod(0o700)
        temporary = runtime_root / f".login-generation.{os.getpid()}"
        temporary.write_text(f"{generation}\n")
        temporary.chmod(0o600)
        temporary.replace(runtime_root / "login-generation")
        return generation

    def unregister(self) -> None:
        self._release_delay_inhibitor()
        if self._prepare_subscription_id is not None and self._system_connection is not None:
            self._system_connection.signal_unsubscribe(self._prepare_subscription_id)
            self._prepare_subscription_id = None
        if self.subscription_id is not None:
            self.connection.signal_unsubscribe(self.subscription_id)
            self.subscription_id = None
        if self.client_path is None:
            return
        try:
            self.connection.call_sync(
                SESSION_BUS_NAME,
                SESSION_OBJECT,
                SESSION_INTERFACE,
                "UnregisterClient",
                GLib.Variant("(o)", (self.client_path,)),
                None,
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            # The session manager may already have left the bus during logout.
            pass
        self.client_path = None

    def _spawn(self, command: list[str], finished: Callable[[int], None]) -> int | None:
        callback_context = copy_context()
        callback = lambda code: callback_context.run(finished, code)
        try:
            process = subprocess.Popen(command, start_new_session=True,
                                       env=operations.child_environment())
        except OSError as error:
            print(f"wsctl: could not start {command[0]}: {error}", file=sys.stderr, flush=True)
            callback(127)
            return None
        self._children[process.pid] = process
        GLib.child_watch_add(
            GLib.PRIORITY_DEFAULT,
            process.pid,
            self._child_finished,
            callback,
        )
        return process.pid

    @staticmethod
    def _transient_service(command: list[str], name: str) -> list[str]:
        """Move restored GUI descendants out of the coordinator cgroup."""
        unit = f"wsctl-{name}-{os.getpid()}-{secrets.token_hex(4)}.service"
        context = operations.current()
        environment = ([f"--setenv={operations.CONTEXT_ENV}=" + json.dumps(context.to_dict(), separators=(",", ":"))]
                       if context else [])
        runtime = []
        if context is not None:
            context.check()
            runtime = [f"--property=RuntimeMaxSec={context.remaining(7 * 60):.3f}s"]
        return [
            "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
            "--service-type=exec", "--property=TimeoutStopSec=10s",
            "--property=ExitType=cgroup",
            "--property=PartOf=graphical-session.target",
            "--property=After=graphical-session.target",
            "--property=KillMode=mixed",
            f"--unit={unit}", *runtime, *environment, "--", *command,
        ]

    def _child_finished(
        self,
        pid: int,
        status: int,
        finished: Callable[[int], None],
    ) -> None:
        process = self._children.pop(pid, None)
        returncode = os.waitstatus_to_exitcode(status)
        if process is not None:
            # GLib owns waitpid for a child watch; recording the status keeps
            # subprocess.Popen from attempting to reap the same child later.
            process.returncode = returncode
        finished(returncode)

    def _systemd_environment(self) -> dict[str, str]:
        result = self.connection.call_sync(
            SYSTEMD_BUS_NAME,
            SYSTEMD_OBJECT,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (SYSTEMD_INTERFACE, "Environment")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        values = result.unpack()[0]
        if isinstance(values, GLib.Variant):
            values = values.unpack()
        environment: dict[str, str] = {}
        for item in values:
            name, separator, value = str(item).partition("=")
            if separator and name in GRAPHICAL_ENVIRONMENT:
                environment[name] = value
        return environment

    def _graphical_session_ready(self) -> bool:
        if self._graphical_target_path is None:
            result = self.connection.call_sync(
                SYSTEMD_BUS_NAME,
                SYSTEMD_OBJECT,
                SYSTEMD_INTERFACE,
                "GetUnit",
                GLib.Variant("(s)", (GRAPHICAL_SESSION_TARGET,)),
                GLib.VariantType.new("(o)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            self._graphical_target_path = str(result.unpack()[0])
        result = self.connection.call_sync(
            SYSTEMD_BUS_NAME,
            self._graphical_target_path,
            "org.freedesktop.DBus.Properties",
            "Get",
            GLib.Variant("(ss)", (SYSTEMD_UNIT_INTERFACE, "ActiveState")),
            GLib.VariantType.new("(v)"),
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )
        state = result.unpack()[0]
        if isinstance(state, GLib.Variant):
            state = state.unpack()
        return state == "active"

    @staticmethod
    def _wayland_socket_ready(environment: dict[str, str]) -> bool:
        display = environment.get("WAYLAND_DISPLAY")
        runtime = environment.get("XDG_RUNTIME_DIR")
        if not display or not runtime:
            return False
        path = Path(display)
        if not path.is_absolute():
            path = Path(runtime) / path
        try:
            return stat.S_ISSOCK(path.stat().st_mode)
        except OSError:
            return False

    def wait_for_graphical_environment(self) -> bool:
        if self._startup_completed:
            return GLib.SOURCE_REMOVE
        try:
            environment = self._systemd_environment()
        except (GLib.Error, TypeError, ValueError) as error:
            environment = {}
            reason = f"GNOME graphical environment: {error}"
        else:
            reason = "GNOME Wayland environment"
        wayland_ready = self._wayland_socket_ready(environment)
        if wayland_ready:
            for name, value in environment.items():
                os.environ[name] = value
            try:
                graphical_session_ready = self._graphical_session_ready()
            except (GLib.Error, TypeError, ValueError) as error:
                graphical_session_ready = False
                reason = f"GNOME graphical-session target: {error}"
            else:
                reason = "GNOME graphical-session target"
        else:
            graphical_session_ready = False

        desktop_ready = False
        desktop_signature = None
        if wayland_ready and graphical_session_ready:
            if not self._status_gnome_ready:
                update_stage(
                    "gnome", "ready",
                    f"Wayland session is active ({environment['WAYLAND_DISPLAY']})",
                    current=1, total=1,
                )
                self._status_gnome_ready = True
            shell = capture_shell()
            desktop_ready, reason = desktop_readiness(
                shell,
                DESKTOP_REQUIRED_CAPABILITIES,
            )
            if desktop_ready:
                desktop_signature = desktop_topology_signature(shell)

        now = time.monotonic()
        if wayland_ready and graphical_session_ready and desktop_ready:
            if (
                self._desktop_ready_since is None
                or desktop_signature != self._desktop_signature
            ):
                self._desktop_ready_since = now
                self._desktop_signature = desktop_signature
                self._environment_wait_reason = None
                print(
                    "wsctl: GNOME displays and workspaces are ready; "
                    f"settling topology for {DESKTOP_SETTLE_SECONDS:g} seconds",
                    flush=True,
                )
                update_stage(
                    "displays", "running",
                    f"Display topology detected; stabilizing for {DESKTOP_SETTLE_SECONDS:g}s",
                )
            if now - self._desktop_ready_since < DESKTOP_SETTLE_SECONDS:
                return GLib.SOURCE_CONTINUE
            print(
                f"wsctl: GNOME display/workspace topology is stable "
                f"({environment['WAYLAND_DISPLAY']}); "
                "starting restore",
                flush=True,
            )
            update_stage(
                "displays", "ready", "Display and workspace topology are stable",
                current=1, total=1,
            )
            update_stage("workspace", "running", "Starting workspace restoration")
            self.start_restore()
            return GLib.SOURCE_REMOVE
        self._desktop_ready_since = None
        self._desktop_signature = None
        if reason != self._environment_wait_reason:
            print(f"wsctl: waiting for {reason}", flush=True)
            self._environment_wait_reason = reason
            if not self._status_gnome_ready:
                update_stage("gnome", "waiting", f"Waiting for {reason}")
            else:
                update_stage("displays", "waiting", f"Waiting for {reason}")
        if now >= self._environment_deadline:
            print(
                "wsctl: GNOME desktop did not become ready within 120 seconds",
                file=sys.stderr,
                flush=True,
            )
            fail_active("GNOME desktop did not become ready within 120 seconds")
            self.exit_code = 2
            self.loop.quit()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE

    def start_restore(self) -> bool:
        if self._startup_completed:
            return GLib.SOURCE_REMOVE
        command = [str(self.bin_dir / "wsctl-startup-launch")]

        def claimed(returncode: int) -> None:
            if self._startup_completed:
                return
            if returncode == 0:
                # The launcher has scheduled the bounded wsctl worker, which
                # alone restores tmux and the desktop categories.
                print("wsctl: claimed automatic login restore", flush=True)
            elif returncode == 2:
                print("wsctl: another terminal launch owns login restore", flush=True)
            elif returncode == 1:
                # A service restart or an earlier partial attempt must retry
                # missing categories.
                self._run_direct_restore()
            else:
                print(
                    f"wsctl: startup launcher failed with exit status {returncode}; retrying directly",
                    file=sys.stderr,
                    flush=True,
                )
                self._run_direct_restore()

        self._spawn(command, claimed)
        return GLib.SOURCE_REMOVE

    def _run_direct_restore(self) -> None:
        if self._startup_completed:
            return
        command = self._transient_service(
            [str(self.bin_dir / "wsctl"), "startup"],
            "restore-worker",
        )

        def finished(returncode: int) -> None:
            if returncode == 0:
                print("wsctl: login restore completed", flush=True)
            else:
                fail_active(f"Workspace restore failed with exit status {returncode}")
                print(
                    f"wsctl: login restore failed with exit status {returncode}",
                    file=sys.stderr,
                    flush=True,
                )

        self._spawn(command, finished)

    def _respond(self, success: bool, reason: str = "") -> None:
        if self.client_path is None:
            return
        try:
            self.connection.call_sync(
                SESSION_BUS_NAME,
                self.client_path,
                CLIENT_INTERFACE,
                "EndSessionResponse",
                GLib.Variant("(bs)", (success, reason)),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
        except GLib.Error as error:
            print(f"wsctl: could not answer GNOME end-session request: {error}", file=sys.stderr, flush=True)

    def _poll_placement_progress(self) -> None:
        """Advance owned content-gated handoffs and observe pending receipts.

        Reuse the existing coordinator wakeup, at most once per two seconds
        while startup can still publish results. The finite child owns the
        finite native calls, so compositor stalls cannot block cancellation.
        """
        context = self._operation_context
        if (context is None or context.mode != "startup"
                or self._placement_progress_finished or self._placement_progress_pid is not None
                or time.monotonic() < self._placement_progress_next):
            return
        self._placement_progress_next = time.monotonic() + 2.0
        try:
            document = json.loads(status_path().read_text())
            if not context.matches(document):
                self._placement_progress_finished = True
                return
            if document.get("operation_state") in {"completed", "cancelled", "failed"}:
                # The child cannot reopen a terminal operation. Stale waiting
                # evidence must not keep spawning no-op observers until expiry.
                self._placement_progress_finished = True
                return
            from .provider_progress import has_pending
            pending = has_pending(document)
            if not pending and context.remaining() > 0:
                stages = document.get("stages", [])
                self._placement_progress_finished = bool(stages) and all(
                    isinstance(stage, dict) and stage.get("state") in {"ready", "failed", "degraded", "skipped"}
                    for stage in stages)
                return
        except (OSError, ValueError, TypeError):
            return

        def finished(_returncode: int) -> None:
            self._placement_progress_pid = None
            if _returncode == 0 and context.remaining() <= 0:
                from .provider_progress import has_native_handoff
                try:
                    self._placement_progress_finished = not has_native_handoff(json.loads(status_path().read_text()))
                except (OSError, ValueError):
                    self._placement_progress_finished = True

        self._placement_progress_pid = self._spawn(
            [str(self.bin_dir / "wsctl"), "placement-progress"], finished)

    def _inhibit_delay_seconds(self) -> float:
        """Read logind's InhibitDelayMaxUSec; fall back to the systemd default."""
        connection = self._system_connection
        if connection is None:
            return DEFAULT_INHIBIT_DELAY_SECONDS
        try:
            reply = connection.call_sync(
                LOGIN1_BUS_NAME,
                LOGIN1_OBJECT,
                LOGIN1_PROPERTIES_INTERFACE,
                "Get",
                GLib.Variant("(ss)", (LOGIN1_MANAGER_INTERFACE, "InhibitDelayMaxUSec")),
                GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE,
                1000,
                None,
            )
            value = reply.unpack()[0]
            if isinstance(value, GLib.Variant):
                value = value.unpack()
            return float(value) / 1_000_000
        except (GLib.Error, TypeError, ValueError):
            return DEFAULT_INHIBIT_DELAY_SECONDS

    def _start_final_save(self, trigger: str) -> None:
        """Start the one bounded final save of this shutdown attempt."""
        if self._final_started:
            return
        self._final_started = True
        if not write_marker(trigger):
            print("wsctl: could not record the session end; periodic checkpoints are not paused",
                  file=sys.stderr, flush=True)
        budget = final_save_budget(self._inhibit_delay_seconds())
        self._final_started_at = time.monotonic()
        self._final_timed_out = False
        print(
            f"wsctl: final save started; trigger={trigger}; budget={budget:.1f}s",
            flush=True,
        )
        try:
            process = subprocess.Popen(
                [str(self.bin_dir / "wsctl"), "save", "--auto", "--final"],
                start_new_session=True,
                # The final save is its own operation, not part of the login.
                env={name: value for name, value in os.environ.items()
                     if name != operations.CONTEXT_ENV},
            )
        except OSError as error:
            print(f"wsctl: could not start the final save: {error}", file=sys.stderr, flush=True)
            print("wsctl: final save finished; rc=127; elapsed=0.0s", flush=True)
            self._final_finished_cleanup()
            return
        self._final_process = process
        GLib.child_watch_add(
            GLib.PRIORITY_DEFAULT, process.pid, self._final_child_exited, None,
        )
        self._final_deadline_source = GLib.timeout_add(
            int(budget * 1000), self._final_deadline,
        )

    def _final_elapsed(self) -> float:
        return time.monotonic() - self._final_started_at

    def _final_child_exited(self, pid: int, status: int, _data: object = None) -> None:
        process = self._final_process
        if process is None or process.pid != pid:
            return
        returncode = os.waitstatus_to_exitcode(status)
        process.returncode = returncode
        if not self._final_timed_out:
            print(
                f"wsctl: final save finished; rc={returncode}; "
                f"elapsed={self._final_elapsed():.1f}s",
                flush=True,
            )
        self._final_finished_cleanup()

    def _final_deadline(self) -> bool:
        """Stop a final save that outlived its budget, without blocking GLib."""
        self._final_deadline_source = None
        process = self._final_process
        if process is None:
            return GLib.SOURCE_REMOVE
        self._final_timed_out = True
        print(
            f"wsctl: final save stopped at deadline; elapsed={self._final_elapsed():.1f}s",
            flush=True,
        )
        self._signal_final_group(signal.SIGTERM)
        self._final_kill_source = GLib.timeout_add(
            int(FINAL_SAVE_KILL_GRACE_SECONDS * 1000), self._final_kill,
        )
        return GLib.SOURCE_REMOVE

    def _final_kill(self) -> bool:
        self._final_kill_source = None
        self._signal_final_group(signal.SIGKILL)
        self._final_finished_cleanup()
        return GLib.SOURCE_REMOVE

    def _signal_final_group(self, signum: int) -> None:
        process = self._final_process
        if process is None:
            return
        try:
            os.killpg(process.pid, signum)
        except (ProcessLookupError, PermissionError):
            pass

    def _final_finished_cleanup(self) -> None:
        """Release the delay lock and answer GNOME once the save is over."""
        for source in (self._final_deadline_source, self._final_kill_source):
            if source is not None:
                GLib.source_remove(source)
        self._final_deadline_source = None
        self._final_kill_source = None
        self._final_process = None
        self._release_delay_inhibitor(announce=True)
        if self._rearm_after_final:
            # The shutdown was cancelled while the save ran.
            self._rearm_after_final = False
            self._final_started = False
            clear_marker()
            self._acquire_delay_inhibitor()
        if self._end_session_pending:
            self._end_session_pending = False
            self._respond(True)
        if self._quit_after_final:
            self.loop.quit()

    @property
    def _final_running(self) -> bool:
        return self._final_started and (
            self._final_process is not None or self._final_kill_source is not None
        )

    def _reset_final_save(self) -> None:
        """Forget a finished final save after a cancelled shutdown."""
        self._end_session_pending = False
        if self._final_running:
            self._rearm_after_final = True
            return
        self._final_started = False
        clear_marker()
        self._acquire_delay_inhibitor()

    def _on_prepare_for_shutdown(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        _signal_name: str,
        parameters: GLib.Variant,
        _user_data: object,
    ) -> None:
        self.handle_prepare_for_shutdown(bool(parameters.unpack()[0]))

    def handle_prepare_for_shutdown(self, active: bool) -> None:
        print(f"wsctl: logind PrepareForShutdown({str(active).lower()})", flush=True)
        if active:
            self._start_final_save("prepare-for-shutdown")
        else:
            self._reset_final_save()

    def poll_progress(self) -> bool:
        """Poll startup abort and placement progress for this login."""
        if self._operation_context is not None:
            operations.bind(self._operation_context)
        if self._startup_quiescence_pending:
            return GLib.SOURCE_CONTINUE
        context = self._operation_context
        if context is not None and context.mode == "startup":
            from .startup_control import abort_requested
            try:
                document = json.loads(status_path().read_text())
                stopping = (context.matches(document) and
                            document.get("operation_state") in {"running", "cancelling"} and
                            abort_requested(context))
            except (OSError, ValueError):
                stopping = False
            if stopping:
                if (not getattr(self, "_startup_abort_pid", None)
                        and time.monotonic() >= getattr(self, "_startup_abort_next", 0)):
                    self._startup_abort_next = time.monotonic() + 2.0
                    def stopped(_code):
                        self._startup_abort_pid = None
                    self._startup_abort_pid = self._spawn(
                        [str(Path(__file__).resolve().parents[2] / "bin/wsctl-python"),
                         "workspace_state.startup_control"], stopped)
                return GLib.SOURCE_CONTINUE
        self._poll_placement_progress()
        return GLib.SOURCE_CONTINUE

    def _on_signal(
        self,
        _connection: Gio.DBusConnection,
        _sender: str,
        _object_path: str,
        _interface_name: str,
        signal_name: str,
        _parameters: GLib.Variant,
        _user_data: object,
    ) -> None:
        self.handle_signal(signal_name)

    def handle_signal(self, signal_name: str) -> None:
        """Answer GNOME session signals; never refuse or cancel a shutdown."""
        if signal_name not in {"QueryEndSession", "EndSession", "CancelEndSession", "Stop"}:
            return
        print(f"wsctl: GNOME {signal_name}", flush=True)
        if signal_name == "QueryEndSession":
            self._respond(True)
        elif signal_name == "EndSession":
            # Covers a logout and any order of the GNOME and logind signals.
            self._start_final_save("end-session")
            if self._final_running:
                self._end_session_pending = True
            else:
                self._respond(True)
        elif signal_name == "CancelEndSession":
            self._reset_final_save()
        elif signal_name == "Stop":
            # Do not cut a running final save short.
            self.stop()

    def stop(self) -> bool:
        if self._final_running:
            # Let the bounded final save finish; its cleanup quits the loop.
            self._quit_after_final = True
            return GLib.SOURCE_REMOVE
        self.loop.quit()
        return GLib.SOURCE_REMOVE


def _bin_dir() -> Path:
    configured = os.environ.get("WSCTL_BIN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "bin"


def _run_coordinator() -> int:
    loop = GLib.MainLoop()
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    system_connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    client = GnomeSessionClient(
        connection,
        loop,
        _bin_dir(),
        delay_inhibitor=DelayInhibitor(system_connection),
        system_connection=system_connection,
    )
    try:
        client.register()
    except (GLib.Error, OSError, RuntimeError) as error:
        print(f"wsctl: could not register with GNOME Session Manager: {error}", file=sys.stderr)
        client.unregister()
        return 2
    GLib.timeout_add(250, client.wait_for_graphical_environment)
    GLib.timeout_add(100, client.poll_progress)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, client.stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, client.stop)
    try:
        loop.run()
    finally:
        client.unregister()
    return client.exit_code


def main() -> int:
    try:
        with operations.coordinator_lock(status_path().parent / "coordinator.lock"):
            return _run_coordinator()
    except (OSError, RuntimeError) as error:
        print(f"wsctl: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
