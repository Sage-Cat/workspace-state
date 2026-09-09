"""GNOME login restore and end-session checkpoint integration."""

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
    cancel_shutdown,
    claim_startup_hud,
    consume_shutdown_cancel,
    fail_active,
    finish,
    initialize as initialize_login_status,
    initialize_shutdown,
    shutdown_commit_path,
    shutdown_rendered_path,
    shutdown_request_path,
    shutdown_worker_complete_path,
    status_path,
    update_stage,
)
from .shutdown_profiles import (
    ShutdownProfileError,
    capture_shutdown_profile_preflight,
    disarm_transaction,
    load_profiles,
    profile_preflight_path,
    transaction_exists,
)
from .util import atomic_json


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
SHUTDOWN_PREPARED_MAX_AGE_SECONDS = 15 * 60
SHUTDOWN_REQUEST_MAX_AGE_SECONDS = 30
PREFLIGHT_HANDOFF_TIMEOUT_SECONDS = 10
HUD_ACK_TIMEOUT_SECONDS = 30
HUD_READY_MINIMUM_SECONDS = 3.0
SHUTDOWN_COMPLETION_MAX_AGE_SECONDS = 15 * 60
SHUTDOWN_SERVICE_START_GRACE_SECONDS = 5.0
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


class ShutdownInhibitor:
    """Hold logind's block lock until a verified HUD handoff is ready."""

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
                    "Waiting for the verified workspace shutdown HUD checkpoint",
                    "block",
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
        shutdown_close_delay_ms: int = 0,
        shutdown_inhibitor: ShutdownInhibitor | None = None,
    ) -> None:
        self.connection = connection
        self.loop = loop
        self.bin_dir = bin_dir
        self._shutdown_inhibitor = shutdown_inhibitor
        self.client_path: str | None = None
        self.subscription_id: int | None = None
        self._children: dict[int, subprocess.Popen[bytes]] = {}
        self._checkpoint_active = False
        self._end_session_pending = False
        self._shutdown_operation_id: str | None = None
        self._shutdown_unit: str | None = None
        self._shutdown_origin: str | None = None
        self._shutdown_action: str | None = None
        self._shutdown_handoff_accepted = False
        self._shutdown_released = False
        self._prepared_operation_id: str | None = None
        self._preflight_prepared_since: float | None = None
        self._verified_worker_completion: dict[str, object] | None = None
        self._hud_ready_since: float | None = None
        self._hud_ack_deadline: float | None = None
        self._shutdown_start_deadline: float | None = None
        self._shutdown_recovery_pending = False
        self._startup_blocked_by_shutdown = False
        self._login_generation: str | None = None
        # Kept as a compatibility-only constructor argument. Completion is
        # controlled by the managed worker's operation-bound marker.
        _ = shutdown_close_delay_ms
        self._environment_deadline = time.monotonic() + 120
        self._environment_wait_reason: str | None = None
        self._graphical_target_path: str | None = None
        self._desktop_ready_since: float | None = None
        self._desktop_signature: str | None = None
        self._status_gnome_ready = False
        self.exit_code = 0

    def register(self) -> None:
        self._acquire_shutdown_inhibitor()
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
        if self._reattach_shutdown_transaction(generation):
            print(
                "wsctl: reattached to the current shutdown transaction; "
                "startup restoration remains suspended",
                flush=True,
            )
            return
        show_startup_hud = claim_startup_hud(generation)
        initialize_login_status(
            generation,
            show_startup_hud=show_startup_hud,
        )
        if not show_startup_hud:
            print(
                "wsctl: startup HUD already shown during this OS boot; "
                "restoring this login in the background",
                flush=True,
            )
        update_stage("gnome", "running", "Waiting for GNOME Wayland session")
        update_stage("displays", "waiting", "Waiting for compositor display readiness")
        print(f"wsctl: registered GNOME session client {self.client_path}", flush=True)

    def _acquire_shutdown_inhibitor(self) -> None:
        if self._shutdown_inhibitor is None:
            return
        self._shutdown_inhibitor.acquire()

    def _release_shutdown_inhibitor(self) -> None:
        if self._shutdown_inhibitor is None:
            return
        self._shutdown_inhibitor.release()

    def _read_current_shutdown_status(self, session_id: str) -> dict[str, object] | None:
        """Read a private shutdown status belonging to this exact login."""
        try:
            descriptor = os.open(status_path(), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                status = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        operation_id = status.get("operation_id") if isinstance(status, dict) else None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not isinstance(status, dict)
            or status.get("schema_version") != 1
            or status.get("mode") != "shutdown"
            or status.get("session_id") != session_id
            or not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
            or status.get("shutdown_action") not in {"poweroff", "restart"}
            or status.get("shutdown_origin") != "preflight"
        ):
            return None
        return status

    def _reattach_shutdown_transaction(self, session_id: str) -> bool:
        status = self._read_current_shutdown_status(session_id)
        if status is None:
            return False
        operation_id = str(status["operation_id"])
        self._startup_blocked_by_shutdown = True
        if status.get("cancelled") is True or status.get("overall_state") == "failed":
            if transaction_exists(operation_id):
                self._shutdown_operation_id = operation_id
                self._shutdown_unit = (
                    f"wsctl-shutdown-finalize@{operation_id}.service"
                )
                self._checkpoint_active = True
                if status.get("cancelled") is True:
                    self._cancel_verified_preflight(
                        "Resuming interrupted shutdown cancellation recovery"
                    )
                else:
                    self._fail_shutdown_coordination(
                        "Resuming rollback after an interrupted shutdown failure"
                    )
                return True
            # Keep the original request as a display-only binding for the
            # terminal failure HUD. _consume_preflight_request() refuses to
            # replay a request whose matching operation is already terminal.
            self._clear_shutdown_coordination(keep_request=True)
            return True
        self._shutdown_operation_id = operation_id
        self._shutdown_unit = f"wsctl-shutdown-finalize@{operation_id}.service"
        self._shutdown_origin = str(status["shutdown_origin"])
        self._shutdown_action = str(status["shutdown_action"])
        if status.get("cancelled") is not True and status.get("overall_state") != "failed":
            self._checkpoint_active = True
            self._shutdown_handoff_accepted = True
            self._shutdown_start_deadline = (
                time.monotonic() + SHUTDOWN_SERVICE_START_GRACE_SECONDS
            )
            if self._prepared_operation_is_current():
                self._release_shutdown_inhibitor()
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
        self._release_shutdown_inhibitor()
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
        try:
            process = subprocess.Popen(command, start_new_session=True)
        except OSError as error:
            print(f"wsctl: could not start {command[0]}: {error}", file=sys.stderr, flush=True)
            finished(127)
            return None
        self._children[process.pid] = process
        GLib.child_watch_add(
            GLib.PRIORITY_DEFAULT,
            process.pid,
            self._child_finished,
            finished,
        )
        return process.pid

    @staticmethod
    def _transient_service(command: list[str], name: str) -> list[str]:
        """Move restored GUI descendants out of the coordinator cgroup."""
        unit = f"wsctl-{name}-{os.getpid()}-{secrets.token_hex(4)}.service"
        return [
            "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
            "--service-type=exec", "--property=TimeoutStopSec=10s",
            "--property=ExitType=cgroup",
            "--property=PartOf=graphical-session.target",
            "--property=After=graphical-session.target",
            "--property=KillMode=mixed",
            f"--unit={unit}", "--", *command,
        ]

    @staticmethod
    def _bootstrap_terminal_service() -> list[str]:
        """Launch Alacritty without adopting its long-lived tmux server."""
        unit = (
            f"wsctl-bootstrap-terminal-{os.getpid()}-"
            f"{secrets.token_hex(4)}.service"
        )
        return [
            "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
            "--service-type=exec", "--property=TimeoutStopSec=10s",
            "--property=ExitType=main",
            "--property=PartOf=graphical-session.target",
            "--property=After=graphical-session.target",
            "--property=KillMode=process",
            f"--unit={unit}", "--", "/usr/bin/alacritty",
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
        if self._startup_blocked_by_shutdown:
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
        command = [str(self.bin_dir / "wsctl-startup-launch")]

        def claimed(returncode: int) -> None:
            if returncode == 0:
                # The launcher has scheduled the normal bounded wsctl worker.
                # Starting one plain Alacritty gives zsh/tmux-continuum the
                # first chance to resurrect tmux before that worker restores
                # and places the desktop categories.
                print("wsctl: claimed automatic login restore", flush=True)
                update_stage("tmux", "running", "Launching bootstrap terminal for tmux-resurrect")
                self._spawn(
                    self._bootstrap_terminal_service(),
                    lambda status: print(
                        f"wsctl: bootstrap Alacritty exited with status {status}",
                        file=sys.stderr,
                        flush=True,
                    ) if status else None,
                )
            elif returncode == 2:
                print("wsctl: another terminal launch owns login restore", flush=True)
                update_stage("tmux", "running", "Another terminal owns tmux restoration")
            elif returncode == 1:
                # A service restart or an earlier partial attempt must retry
                # missing categories without opening another bootstrap window.
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
        command = self._transient_service(
            [str(self.bin_dir / "wsctl"), "startup", "--await-tmux"],
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

    def _begin_checkpoint(
        self,
        *,
        operation_id: str | None = None,
        origin: str = "preflight",
        action: str = "poweroff",
    ) -> None:
        if origin != "preflight" or operation_id is None:
            raise ValueError("shutdown checkpoints require a confirmed Shell preflight")
        self._checkpoint_active = True
        self._shutdown_operation_id = operation_id
        self._shutdown_unit = (
            f"wsctl-shutdown-finalize@{self._shutdown_operation_id}.service"
        )
        self._shutdown_origin = origin
        self._shutdown_action = action
        self._shutdown_handoff_accepted = False
        self._shutdown_released = False
        self._prepared_operation_id = None
        self._preflight_prepared_since = None
        self._verified_worker_completion = None
        self._hud_ready_since = None
        self._hud_ack_deadline = None
        self._shutdown_start_deadline = (
            time.monotonic() + SHUTDOWN_SERVICE_START_GRACE_SECONDS
        )
        self._clear_shutdown_coordination(keep_request=True)
        try:
            capture_shutdown_profile_preflight(
                load_profiles(),
                operation_id=self._shutdown_operation_id,
                session_id=self._login_generation or f"session-{os.getpid()}",
                action=action,
            )
        except ShutdownProfileError as error:
            reason = f"pre-HUD shutdown profile capture failed: {error}"
            if initialize_shutdown(
                self._login_generation or f"session-{os.getpid()}",
                self._shutdown_operation_id,
                action=action,
                origin=origin,
            ):
                update_stage(
                    "checkpoint-proof", "failed", reason, error=reason,
                )
                fail_active(reason)
            append_diagnostic("shutdown pre-HUD profile capture", str(error))
            self._clear_shutdown_coordination(keep_request=True)
            self._reset_shutdown_attempt()
            return
        if not initialize_shutdown(
            self._login_generation or f"session-{os.getpid()}",
            self._shutdown_operation_id,
            action=action,
            origin=origin,
        ):
            self._checkpoint_active = False
            reason = "could not initialize the private shutdown transaction"
            append_diagnostic("shutdown initialization", reason)
            try:
                profile_preflight_path().unlink(missing_ok=True)
            except OSError as error:
                append_diagnostic("shutdown pre-HUD profile cleanup", str(error))
            return
        print(
            f"wsctl: GNOME {action} confirmed; handing the workspace "
            "checkpoint to a managed user service",
            flush=True,
        )

        # The Shell has retained the user's final Confirmed* action, so this
        # worker runs before GNOME's final QueryEndSession/EndSession phase.
        self._end_session_pending = False

        operation_id = self._shutdown_operation_id

        def accepted(returncode: int) -> None:
            if self._shutdown_operation_id != operation_id:
                return
            if returncode:
                self._checkpoint_active = False
                reason = "shutdown checkpoint service did not accept ownership"
                self._append_shutdown_journal("shutdown service handoff", self._shutdown_unit)
                update_stage("checkpoint-proof", "failed", reason, error=reason)
                fail_active(reason)
                self._clear_shutdown_coordination(keep_request=True)
                self._reset_shutdown_attempt()
            else:
                self._shutdown_handoff_accepted = True

        self._spawn(
            [
                "/usr/bin/systemctl", "--user", "start", "--no-block",
                self._shutdown_unit,
            ],
            accepted,
        )

    @staticmethod
    def _append_shutdown_journal(label: str, unit: str | None = None) -> None:
        try:
            result = subprocess.run(
                [
                    "/usr/bin/journalctl", "--user", "--unit",
                    unit or "wsctl-gnome-session.service", "--boot", "--no-pager",
                    "--lines=200", "--output=short-precise",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            append_diagnostic(f"could not collect {label} journal", str(error))
            return
        append_diagnostic(
            f"GNOME shutdown journal after {label}",
            result.stdout or result.stderr,
        )

    def poll_cancel_request(self) -> bool:
        """Accept HUD preflight requests and release GNOME after confirmation."""
        if not self._shutdown_recovery_pending:
            self._advance_shutdown_completion()
        if (
            self._shutdown_operation_id is not None
            and self._prepared_operation_is_current()
            and consume_shutdown_cancel(self._shutdown_operation_id)
        ):
            self._cancel_verified_preflight(
                "Shutdown cancelled after GNOME rejected or cancelled the final handoff"
            )
            return GLib.SOURCE_CONTINUE
        if not self._checkpoint_active:
            request = self._consume_preflight_request()
            if request is not None:
                self._begin_checkpoint(
                    operation_id=request["operation_id"],
                    origin="preflight",
                    action=request["action"],
                )
        if (
            self._shutdown_handoff_accepted
            and self._end_session_pending
            and not self._shutdown_released
            and self._prepared_operation_is_current()
        ):
            self._checkpoint_active = False
            self._shutdown_released = True
            print(
                "wsctl: checkpoint complete; answering the pending GNOME end-session query",
                flush=True,
            )
            self._respond(True)
        self._guard_preflight_handoff()
        self._forget_finished_preflight()
        return GLib.SOURCE_CONTINUE

    def _worker_completion(self) -> dict[str, object] | None:
        path = shutdown_worker_complete_path()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                completion = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"could not read {path.name}: {error}") from error
        operation_id = completion.get("operation_id") if isinstance(completion, dict) else None
        invocation_id = completion.get("invocation_id") if isinstance(completion, dict) else None
        created_at = completion.get("created_at") if isinstance(completion, dict) else None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not 0 <= time.time() - metadata.st_mtime <= SHUTDOWN_COMPLETION_MAX_AGE_SECONDS
            or not isinstance(completion, dict)
            or completion.get("schema_version") != 1
            or not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
            or completion.get("login_generation") != self._login_generation
            or completion.get("action") not in {"poweroff", "restart"}
            or completion.get("origin") != "preflight"
            or not isinstance(invocation_id, str)
            or len(invocation_id) != 32
            or any(character not in "0123456789abcdef" for character in invocation_id)
            or not isinstance(created_at, (int, float))
            or not 0 <= time.time() - created_at <= SHUTDOWN_COMPLETION_MAX_AGE_SECONDS
        ):
            raise RuntimeError("shutdown worker completion is insecure, stale, or malformed")
        if self._shutdown_operation_id is not None and operation_id != self._shutdown_operation_id:
            raise RuntimeError("shutdown worker completion belongs to another operation")
        try:
            descriptor = os.open(status_path(), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                status_metadata = os.fstat(stream.fileno())
                status = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"could not verify shutdown status: {error}") from error
        if (
            not stat.S_ISREG(status_metadata.st_mode)
            or status_metadata.st_uid != os.getuid()
            or status_metadata.st_mode & 0o077
            or not isinstance(status, dict)
            or status.get("mode") != "shutdown"
            or status.get("session_id") != self._login_generation
            or status.get("operation_id") != operation_id
            or status.get("shutdown_action") != completion["action"]
            or status.get("shutdown_origin") != completion["origin"]
            or status.get("cancelled") is True
            or status.get("overall_state") == "failed"
        ):
            raise RuntimeError("shutdown worker completion does not match the active HUD transaction")
        return completion

    @staticmethod
    def _shutdown_unit_properties(unit: str) -> dict[str, str]:
        properties = (
            "LoadState", "ActiveState", "SubState", "Result", "ExecMainCode",
            "ExecMainStatus", "InvocationID", "Job",
            "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic",
        )
        try:
            result = subprocess.run(
                [
                    "/usr/bin/systemctl", "--user", "show", unit,
                    *(f"--property={name}" for name in properties),
                    "--no-pager",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"could not inspect {unit}: {error}") from error
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "systemctl show failed"
            raise RuntimeError(f"could not inspect {unit}: {detail}")
        return dict(
            line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
        )

    @staticmethod
    def _unit_finished_successfully(
        properties: dict[str, str],
        invocation_id: str,
    ) -> bool | None:
        active_state = properties.get("ActiveState")
        sub_state = properties.get("SubState")
        if (
            properties.get("Job", "")
            or active_state in {"activating", "deactivating"}
            or (active_state == "active" and sub_state != "exited")
        ):
            return None
        try:
            start = int(properties.get("ExecMainStartTimestampMonotonic", "0"))
            end = int(properties.get("ExecMainExitTimestampMonotonic", "0"))
        except ValueError:
            return False
        return all((
            properties.get("LoadState") == "loaded",
            (active_state, sub_state) in {
                ("active", "exited"),
                ("inactive", "dead"),
            },
            properties.get("Result") == "success",
            properties.get("Job", "") == "",
            properties.get("ExecMainCode") == "1",
            properties.get("ExecMainStatus") == "0",
            properties.get("InvocationID") == invocation_id,
            start > 0,
            end >= start,
        ))

    @staticmethod
    def _coordination_signal_matches(
        path: Path,
        operation_id: str,
        session_id: str,
    ) -> bool:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                payload = json.load(stream)
        except FileNotFoundError:
            return False
        except (OSError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"could not read {path.name}: {error}") from error
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not 0 <= time.time() - metadata.st_mtime <= HUD_ACK_TIMEOUT_SECONDS
            or not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or payload.get("operation_id") != operation_id
            or payload.get("session_id") != session_id
        ):
            raise RuntimeError(f"{path.name} is insecure, stale, malformed, or belongs to another operation")
        return True

    def _advance_shutdown_completion(self) -> None:
        try:
            completion = self._worker_completion()
            if completion is None:
                self._check_shutdown_worker_without_completion()
                return
            operation_id = str(completion["operation_id"])
            unit = f"wsctl-shutdown-finalize@{operation_id}.service"
            properties = self._shutdown_unit_properties(unit)
            unit_success = self._unit_finished_successfully(
                properties,
                str(completion["invocation_id"]),
            )
            if unit_success is None:
                return
            if not unit_success:
                raise RuntimeError(f"{unit} did not exit successfully with its recorded invocation")
            if self._verified_worker_completion is None:
                self._verified_worker_completion = completion
                self._checkpoint_active = True
                self._shutdown_handoff_accepted = True
                self._shutdown_operation_id = operation_id
                self._shutdown_unit = unit
                self._shutdown_origin = str(completion["origin"])
                self._shutdown_action = str(completion["action"])
                self._hud_ack_deadline = time.monotonic() + HUD_ACK_TIMEOUT_SECONDS
                update_stage(
                    "checkpoint-proof", "ready",
                    "Managed checkpoint exited successfully; showing the final countdown",
                    current=1, total=1,
                )
                finish(
                    "Restart preparation complete" if completion["action"] == "restart"
                    else "Power-off preparation complete"
                )
                return
            if consume_shutdown_cancel(operation_id):
                self._cancel_verified_preflight("Shutdown cancelled during the final countdown")
                return
            now = time.monotonic()
            session_id = str(completion["login_generation"])
            if self._coordination_signal_matches(
                shutdown_rendered_path(), operation_id, session_id,
            ):
                if self._hud_ready_since is None:
                    self._hud_ready_since = now
                    self._hud_ack_deadline = now + HUD_ACK_TIMEOUT_SECONDS
            commit_authorized = self._coordination_signal_matches(
                shutdown_commit_path(), operation_id, session_id,
            )
            if (
                self._hud_ready_since is not None
                and commit_authorized
                and now - self._hud_ready_since >= HUD_READY_MINIMUM_SECONDS
            ):
                # Re-verify both the closed marker and exact completed systemd
                # invocation immediately before crossing the point of no return.
                completion = self._worker_completion()
                properties = self._shutdown_unit_properties(unit)
                if completion is None or self._unit_finished_successfully(
                    properties, str(completion["invocation_id"]),
                ) is not True:
                    raise RuntimeError("shutdown worker changed before final authorization")
                finish("Shutdown handoff authorized")
                atomic_json(self._prepared_shutdown_path(), {
                    "schema_version": 1,
                    "operation_id": operation_id,
                    "login_generation": session_id,
                    "session_id": session_id,
                    "action": completion["action"],
                    "origin": completion["origin"],
                    "invocation_id": completion["invocation_id"],
                    "created_at": time.time(),
                })
                # Keep the block lock until every authorization artifact is
                # durable. The Shell cannot emit the retained GNOME action
                # before observing this marker, so releasing here closes the
                # direct `shutdown now` bypass without racing the final handoff.
                self._release_shutdown_inhibitor()
                for path in (
                    shutdown_worker_complete_path(),
                    shutdown_rendered_path(),
                    shutdown_commit_path(),
                ):
                    path.unlink(missing_ok=True)
                self._verified_worker_completion = None
                self._hud_ready_since = None
                self._hud_ack_deadline = None
                return
            if self._hud_ack_deadline is not None and now >= self._hud_ack_deadline:
                raise RuntimeError("HUD did not render and commit the shutdown countdown in time")
        except RuntimeError as error:
            self._fail_shutdown_coordination(str(error))

    def _check_shutdown_worker_without_completion(self) -> None:
        """Fail closed if the managed unit dies before publishing success."""
        if (
            not self._checkpoint_active
            or not self._shutdown_handoff_accepted
            or self._shutdown_unit is None
        ):
            return
        # Promotion deliberately consumes the worker marker. From that point
        # the authoritative prepared marker, revalidated against live status,
        # is the only success token until GNOME accepts the saved action.
        if self._prepared_operation_is_current():
            return
        properties = self._shutdown_unit_properties(self._shutdown_unit)
        active = properties.get("ActiveState")
        if (
            properties.get("Job", "")
            or active in {"activating", "deactivating"}
            or (active == "active" and properties.get("SubState") != "exited")
        ):
            return
        try:
            started = int(properties.get("ExecMainStartTimestampMonotonic", "0"))
        except ValueError:
            started = 0
        if started > 0:
            status = self._read_current_shutdown_status(self._login_generation or "")
            if status is not None and (
                status.get("cancelled") is True or status.get("overall_state") == "failed"
            ):
                return
            raise RuntimeError(
                f"{self._shutdown_unit} exited without a verified completion marker"
            )
        if (
            self._shutdown_start_deadline is not None
            and time.monotonic() >= self._shutdown_start_deadline
        ):
            self._fail_shutdown_coordination(
                f"{self._shutdown_unit} did not start",
                recovery_required=False,
            )

    def _cancel_verified_preflight(self, reason: str) -> None:
        self._acquire_shutdown_inhibitor()
        operation_id = self._shutdown_operation_id
        unit = self._shutdown_unit
        self._clear_shutdown_coordination(keep_request=True)
        self._prepared_operation_id = None
        if operation_id is None:
            cancel_shutdown(reason)
            self._reset_shutdown_attempt()
            return
        self._shutdown_recovery_pending = True
        cancel_shutdown(reason, recovery_pending=True)

        def recovered(returncode: int) -> None:
            if returncode or transaction_exists(operation_id):
                detail = (
                    f"shutdown rollback service failed with exit status {returncode}"
                    if returncode
                    else "shutdown rollback journal is still armed"
                )
                update_stage("profile-recovery", "failed", detail, error=detail)
                fail_active(detail)
                append_diagnostic("shutdown cancellation recovery", detail)
            else:
                update_stage(
                    "profile-recovery", "ready",
                    "All prepared shutdown jobs were restored",
                    current=1, total=1,
                )
                cancel_shutdown(reason)
                finish("Shutdown cancelled; prepared jobs were restored")
            self._reset_shutdown_attempt()

        self._stop_shutdown_unit(unit, recovered)

    def _fail_shutdown_coordination(
        self,
        reason: str,
        *,
        recovery_required: bool = True,
    ) -> None:
        self._acquire_shutdown_inhibitor()
        operation_id = self._shutdown_operation_id
        unit = self._shutdown_unit
        self._clear_shutdown_coordination(keep_request=True)
        self._prepared_operation_id = None
        update_stage("checkpoint-proof", "failed", reason, error=reason)
        fail_active(reason)
        if operation_id is None or not recovery_required:
            self._stop_shutdown_unit(unit)
            self._reset_shutdown_attempt()
            return
        self._shutdown_recovery_pending = True
        update_stage(
            "profile-recovery", "running",
            "Recovering jobs after shutdown preparation failure",
        )

        def recovered(returncode: int) -> None:
            if returncode or transaction_exists(operation_id):
                detail = (
                    f"shutdown rollback service failed with exit status {returncode}"
                    if returncode
                    else "shutdown rollback journal is still armed"
                )
                update_stage("profile-recovery", "failed", detail, error=detail)
                append_diagnostic("shutdown failure recovery", detail)
            else:
                update_stage(
                    "profile-recovery", "ready",
                    "Prepared shutdown jobs were restored",
                    current=1, total=1,
                )
            self._reset_shutdown_attempt()

        self._stop_shutdown_unit(unit, recovered)

    def _stop_shutdown_unit(
        self,
        unit: str | None = None,
        finished: Callable[[int], None] | None = None,
    ) -> None:
        target = unit or self._shutdown_unit
        if target is None:
            if finished is not None:
                finished(0)
            return
        self._spawn(
            [
                "/usr/bin/systemctl", "--user", "stop",
                target,
            ],
            finished or (lambda _status: None),
        )

    def _guard_preflight_handoff(self) -> None:
        """Recover if Shell commits preparation but never starts GNOME shutdown."""
        if (
            not self._checkpoint_active
            or self._shutdown_origin != "preflight"
            or self._end_session_pending
            or self._shutdown_released
            or not self._shutdown_handoff_accepted
        ):
            return
        if not self._prepared_operation_is_current():
            self._preflight_prepared_since = None
            return
        now = time.monotonic()
        if self._preflight_prepared_since is None:
            self._preflight_prepared_since = now
            return
        if now - self._preflight_prepared_since < PREFLIGHT_HANDOFF_TIMEOUT_SECONDS:
            return
        reason = "GNOME did not accept the prepared shutdown handoff"
        self._cancel_verified_preflight(reason)

    def _consume_preflight_request(self) -> dict[str, str] | None:
        """Consume one fresh, private request emitted by the Shell HUD."""
        path = shutdown_request_path()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                request = json.load(stream)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, json.JSONDecodeError) as error:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            append_diagnostic("invalid shutdown preflight request", str(error))
            return None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or not 0 <= time.time() - metadata.st_mtime <= SHUTDOWN_REQUEST_MAX_AGE_SECONDS
            or not isinstance(request, dict)
            or request.get("schema_version") != 1
            or request.get("session_id") != self._login_generation
            or request.get("action") not in {"poweroff", "restart"}
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            append_diagnostic(
                "rejected shutdown preflight request",
                "request was stale, insecure, malformed, or belonged to another login",
            )
            return None
        operation_id = request.get("operation_id")
        if (
            not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            append_diagnostic(
                "rejected shutdown preflight request",
                "operation_id was not 32 lowercase hexadecimal characters",
            )
            return None
        status = self._read_current_shutdown_status(self._login_generation or "")
        if (
            status is not None
            and status.get("operation_id") == operation_id
            and status.get("shutdown_action") == request.get("action")
            and (
                status.get("cancelled") is True
                or status.get("overall_state") == "failed"
            )
        ):
            # The request remains as an authenticated display binding so a
            # failed HUD survives recovery and coordinator restarts. It must
            # never be interpreted as permission to replay the old shutdown.
            return None
        return {"operation_id": operation_id, "action": str(request["action"])}

    def _forget_finished_preflight(self) -> None:
        """Allow a new attempt after a cancelled or failed preflight."""
        if not self._checkpoint_active:
            return
        try:
            with (
                self._prepared_shutdown_path().parent / "login-hud-status.json"
            ).open(encoding="utf-8") as stream:
                status = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return
        if (
            not isinstance(status, dict)
            or status.get("operation_id") != self._shutdown_operation_id
            or not (status.get("cancelled") is True or status.get("overall_state") == "failed")
        ):
            return
        self._clear_shutdown_coordination(keep_request=True)
        self._reset_shutdown_attempt()

    def _reset_shutdown_attempt(self) -> None:
        self._checkpoint_active = False
        self._shutdown_handoff_accepted = False
        self._shutdown_released = False
        self._end_session_pending = False
        self._shutdown_operation_id = None
        self._shutdown_unit = None
        self._shutdown_origin = None
        self._shutdown_action = None
        self._prepared_operation_id = None
        self._preflight_prepared_since = None
        self._verified_worker_completion = None
        self._hud_ready_since = None
        self._hud_ack_deadline = None
        self._shutdown_start_deadline = None
        self._shutdown_recovery_pending = False

    @staticmethod
    def _prepared_shutdown_path() -> Path:
        runtime = Path(
            os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        ) / "workspace-state"
        return runtime / "shutdown-prepared.json"

    def _clear_prepared_shutdown(self) -> None:
        try:
            self._prepared_shutdown_path().unlink(missing_ok=True)
        except OSError as error:
            append_diagnostic("could not clear stale shutdown preparation", str(error))

    def _clear_shutdown_coordination(self, *, keep_request: bool = False) -> None:
        paths = [
            self._prepared_shutdown_path(),
            shutdown_rendered_path(),
            shutdown_commit_path(),
            shutdown_worker_complete_path(),
            profile_preflight_path(),
        ]
        if not keep_request:
            paths.append(shutdown_request_path())
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError as error:
                append_diagnostic(
                    f"could not clear stale shutdown coordination file {path.name}",
                    str(error),
                )

    def _prepared_operation_is_current(self) -> bool:
        """Admit only a fresh preparation made for this GNOME login.

        The marker is deliberately checked here instead of trusting a service
        name or an old in-memory flag: the coordinator may have restarted
        between the worker finishing and the original GNOME shutdown request
        being released.
        """
        path = self._prepared_shutdown_path()
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                prepared = json.load(stream)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
            ):
                return False
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if not isinstance(prepared, dict):
            return False
        operation_id = prepared.get("operation_id")
        created_at = prepared.get("created_at")
        if (
            prepared.get("schema_version") != 1
            or not isinstance(operation_id, str)
            or len(operation_id) != 32
            or any(character not in "0123456789abcdef" for character in operation_id)
            or prepared.get("login_generation") != self._login_generation
            or prepared.get("session_id") != self._login_generation
            or prepared.get("action") not in {"poweroff", "restart"}
            or prepared.get("origin") != "preflight"
            or not isinstance(created_at, (int, float))
            or not 0 <= time.time() - created_at <= SHUTDOWN_PREPARED_MAX_AGE_SECONDS
        ):
            return False
        if (
            self._shutdown_operation_id is not None
            and operation_id != self._shutdown_operation_id
        ):
            return False
        try:
            descriptor = os.open(status_path(), os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, encoding="utf-8") as stream:
                status_metadata = os.fstat(stream.fileno())
                status = json.load(stream)
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if (
            not stat.S_ISREG(status_metadata.st_mode)
            or status_metadata.st_uid != os.getuid()
            or status_metadata.st_mode & 0o077
            or not isinstance(status, dict)
            or status.get("schema_version") != 1
            or status.get("mode") != "shutdown"
            or status.get("session_id") != self._login_generation
            or status.get("operation_id") != operation_id
            or status.get("shutdown_action") != prepared.get("action")
            or status.get("shutdown_origin") != prepared.get("origin")
            or status.get("cancelled") is True
            or status.get("overall_state") not in {"ready", "degraded"}
        ):
            return False
        self._prepared_operation_id = operation_id
        return True

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
        if signal_name == "QueryEndSession":
            if self._prepared_operation_is_current():
                self._end_session_pending = False
                self._shutdown_released = True
                self._preflight_prepared_since = None
                self._respond(True)
                return
            if self._shutdown_handoff_accepted:
                self._end_session_pending = True
                self._respond(False, "Checkpoint is already running in the shutdown HUD")
                return
            # GNOME emits its initial query while the native confirmation is
            # still open. Starting work here puts the HUD behind that dialog
            # and performs work before the user presses Power Off/Restart.
            # The Shell extension starts an explicit preflight only from the
            # final _confirm callback, so an unbound query is passive.
            self._end_session_pending = False
            self._respond(True)
        elif signal_name == "EndSession":
            already_authorized = (
                self._shutdown_released
                and self._prepared_operation_id is not None
                and self._prepared_operation_id == self._shutdown_operation_id
            )
            if already_authorized or self._prepared_operation_is_current():
                operation_id = self._shutdown_operation_id or self._prepared_operation_id
                if operation_id is not None and not disarm_transaction(
                    operation_id,
                    action=self._shutdown_action,
                    session_id=self._login_generation,
                ):
                    reason = "Could not disarm shutdown rollback before GNOME EndSession"
                    self._respond(False, reason)
                    self._fail_shutdown_coordination(reason)
                    return
                self._shutdown_released = True
                self._release_shutdown_inhibitor()
                self._respond(True)
            elif not self._checkpoint_active and self._shutdown_operation_id is None:
                # If the Shell extension is unavailable, do not break Ubuntu's
                # ordinary shutdown. No HUD transaction exists to protect.
                self._release_shutdown_inhibitor()
                self._respond(True)
            else:
                self._end_session_pending = True
                self._respond(False, "Workspace checkpoint has not been committed")
        elif signal_name == "CancelEndSession":
            self._acquire_shutdown_inhibitor()
            self._shutdown_released = False
            self._end_session_pending = False
            self._cancel_verified_preflight(
                "GNOME shutdown was cancelled; restoring prepared jobs"
            )
        elif signal_name == "Stop":
            self.loop.quit()

    def stop(self) -> bool:
        self.loop.quit()
        return GLib.SOURCE_REMOVE


def _bin_dir() -> Path:
    configured = os.environ.get("WSCTL_BIN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / ".local" / "bin"


def main() -> int:
    loop = GLib.MainLoop()
    connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    system_connection = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
    client = GnomeSessionClient(
        connection,
        loop,
        _bin_dir(),
        shutdown_inhibitor=ShutdownInhibitor(system_connection),
    )
    try:
        client.register()
    except (GLib.Error, OSError, RuntimeError) as error:
        print(f"wsctl: could not register with GNOME Session Manager: {error}", file=sys.stderr)
        client.unregister()
        return 2
    GLib.timeout_add(250, client.wait_for_graphical_environment)
    GLib.timeout_add(100, client.poll_cancel_request)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, client.stop)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, client.stop)
    try:
        loop.run()
    finally:
        client.unregister()
    return client.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
