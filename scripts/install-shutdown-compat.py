#!/usr/bin/python3 -I
"""Install opt-in Ubuntu shutdown compatibility, without restarting services."""

import argparse
import datetime
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


SOURCE = Path(__file__).resolve().parents[1] / "system-integration"
REMMINA_FILES = (
    ("wsctl-remmina-agent-prepare", "/usr/local/libexec/wsctl-remmina-agent-prepare", 0o755),
    ("system/snap.remmina.ssh-agent.service.d/70-wsctl-stale-agent-socket.conf", "/etc/systemd/system/snap.remmina.ssh-agent.service.d/70-wsctl-stale-agent-socket.conf", 0o644),
)
SYSTEM_FILES = REMMINA_FILES + (
    ("wsctl-livepatch-stop-check", "/usr/local/libexec/wsctl-livepatch-stop-check", 0o755),
    ("system/gdm.service.d/70-wsctl-shutdown-order.conf", "/etc/systemd/system/gdm.service.d/70-wsctl-shutdown-order.conf", 0o644),
    ("system/snap.cups.cups-browsed.service.d/70-wsctl-shutdown-order.conf", "/etc/systemd/system/snap.cups.cups-browsed.service.d/70-wsctl-shutdown-order.conf", 0o644),
    ("system/snap.canonical-livepatch.canonical-livepatchd.service.d/70-wsctl-shutdown-result.conf", "/etc/systemd/system/snap.canonical-livepatch.canonical-livepatchd.service.d/70-wsctl-shutdown-result.conf", 0o644),
)
USER_FILES = (
    "wsctl-gpg-ssh-environment.service",
    "xdg-desktop-portal-gtk.service.d/70-wsctl-shutdown-order.conf",
    "snap.snapd-desktop-integration.snapd-desktop-integration.service.d/70-wsctl-shutdown-order.conf",
    "gpg-agent-ssh.socket.d/70-wsctl-environment-cleanup.conf",
    "tmux-spawn-.scope.d/70-wsctl-terminal-hangup.conf",
)
TMUX_SCOPE_POLICY = "tmux-spawn-.scope.d/70-wsctl-terminal-hangup.conf"


def user_files(config, data):
    files = []
    for name in USER_FILES:
        source = SOURCE / "user" / name
        target = config / "systemd/user" / name
        managed = data / "workspace-state/desktop-releases/current/components/workspace-state/system-integration/user" / name
        if name == TMUX_SCOPE_POLICY and target.is_symlink() and os.readlink(target) == str(managed):
            # Preserve ownership by the immutable release and its rollback. The
            # legacy installer must neither replace that link nor update code
            # inside the sealed release through it.
            if target.read_bytes() != source.read_bytes():
                raise RuntimeError(f"Scope policy belongs to a different release; deploy it at next login: {target}")
            print(f"Unchanged release-owned policy: {target}")
            continue
        files.append((source, target, 0o644))
    return files


def atomic_copy(source, target, mode):
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise RuntimeError(f"Refusing non-regular destination: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(source.read_bytes())
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def install_files(files, backup):
    # Validate every source/destination before changing any of them.
    for source, target, mode in files:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError(f"Missing regular source: {source}")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeError(f"Refusing non-regular destination: {target}")
    for source, target, mode in files:
        if target.exists() and source.read_bytes() == target.read_bytes() and target.stat().st_mode & 0o777 == mode:
            print(f"Unchanged: {target}")
            continue
        if target.exists():
            saved = backup / target.relative_to(target.anchor)
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            print(f"Previous file saved: {saved}")
        atomic_copy(source, target, mode)
        print(f"Installed: {target}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--user", action="store_true")
    scope.add_argument("--system", action="store_true")
    parser.add_argument("--component", choices=("all", "remmina"), default="all",
                        help="install only a fixed component's helper and drop-in (system scope only)")
    # Accept a fixed scope, never arbitrary commands or root paths.
    args = parser.parse_args()
    system = args.system
    if not system and args.component != "all":
        parser.error("--component remmina requires --system")
    if system != (os.geteuid() == 0):
        raise SystemExit("Use --user as the desktop user; use pkexec for --system.")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    command = ["/usr/bin/systemctl"] + ([] if system else ["--user"])
    if system:
        selected = REMMINA_FILES if args.component == "remmina" else SYSTEM_FILES
        files = [(SOURCE / source, Path(target), mode) for source, target, mode in selected]
        backup = Path("/var/backups/wsctl-shutdown-compat") / stamp
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        data = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
        state = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
        if not all(path.is_absolute() for path in (config, data, state)):
            raise SystemExit("XDG config/data/state paths must be absolute.")
        files = user_files(config, data)
        backup = state / "workspace-state/shutdown-compat-backups" / stamp
    install_files(files, backup)
    subprocess.run(command + ["daemon-reload"], check=True, timeout=30)
    if not system:
        active = subprocess.run(command + ["is-active", "--quiet", "gpg-agent-ssh.socket"], check=False, timeout=5)
        if active.returncode == 0:
            # Inert ExecStart=true. Do not restart the existing SSH socket/agent.
            subprocess.run(command + ["start", "wsctl-gpg-ssh-environment.service"], check=True, timeout=10)
    print("Loaded shutdown corrections. No desktop, application or system service was restarted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
