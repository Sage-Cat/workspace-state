"""Let a proven migrated browser/editor main exit before its children stop.

Run by ExecStop inside the original graphical app unit. Chromium can move its
main PID into its own scope while leaving renderer children behind. With no
main PID left, KillMode=mixed otherwise kills those children immediately.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import select
import signal
import sys


EDITOR_UNIT = re.compile(r"wsctl-app-vscode-(?:native-recovery|project)-[0-9a-f]{8}\.service")


@dataclass(frozen=True)
class Process:
    pid: int
    parent: int
    started: int
    executable: tuple[int, int]
    uid: int
    group: str


def process(pid: int, proc: Path = Path("/proc")) -> Process | None:
    try:
        root = proc / str(pid)
        fields = (root / "stat").read_text().rsplit(") ", 1)[1].split()
        groups = [line[3:] for line in (root / "cgroup").read_text().splitlines() if line.startswith("0::/")]
        executable = (root / "exe").stat()
        if len(groups) != 1 or ".." in Path(groups[0]).parts:
            return None
        return Process(pid, int(fields[1]), int(fields[19]),
                       (executable.st_dev, executable.st_ino), root.stat().st_uid, groups[0])
    except (OSError, ValueError, IndexError):
        return None


def migrated_owner(group: str, *, proc: Path = Path("/proc"), cgroups: Path = Path("/sys/fs/cgroup")) -> Process | None:
    if not group.startswith("/") or ".." in Path(group).parts:
        raise ValueError("invalid graphical service cgroup")
    candidates = set()
    for value in (cgroups / group.lstrip("/") / "cgroup.procs").read_text().split():
        child = process(int(value), proc)
        if child is None or child.group != group or child.uid != os.getuid():
            continue
        parent = process(child.parent, proc)
        if parent is None or parent.uid != child.uid or parent.started > child.started:
            continue
        # The scope name alone is not identity: require an actual child in
        # this exact unit and the same executable inode as its external parent.
        applications = ("app-org.chromium.Chromium", "app-com.google.Chrome")
        if EDITOR_UNIT.fullmatch(Path(group).name):
            # The editor can likewise migrate its main PID into GNOME's scope
            # while leaving Electron children in its proven recovery unit.
            # Admit this scope only for the two exact editor launch purposes;
            # the same live child/parent executable and identity proof applies.
            applications += ("app-com.microsoft.VSCode",)
        expected = {
            str(Path(group).parent / f"{application}-{parent.pid}.scope")
            for application in applications
        }
        if parent.group in expected and parent.executable == child.executable:
            candidates.add(parent)
    if len(candidates) > 1:
        raise RuntimeError("multiple migrated application owners; refusing to choose")
    return next(iter(candidates), None)


def stop_migrated_owner(group: str, *, timeout: float = 4.0) -> bool:
    owner = migrated_owner(group)
    if owner is None:
        return False
    try:
        descriptor = os.pidfd_open(owner.pid)
    except ProcessLookupError:
        return True
    try:
        # Pin the PID before rereading all ownership evidence, including its
        # start ticks. Reuse, migration or a different application fails closed.
        if migrated_owner(group) != owner:
            raise RuntimeError("migrated application ownership changed before stop")
        try:
            signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        except ProcessLookupError:
            return True
        if not select.select([descriptor], [], [], timeout)[0]:
            raise RuntimeError("migrated application did not exit within the graceful stop budget")
        return True
    finally:
        os.close(descriptor)


def main() -> int:
    try:
        if len(sys.argv) != 2 or not re.fullmatch(r"wsctl-app-[A-Za-z0-9_.-]+\.service", sys.argv[1]):
            raise RuntimeError("expected this graphical app service's exact unit name")
        own = process(os.getpid())
        if own is None or Path(own.group).name != sys.argv[1]:
            raise RuntimeError("stop helper is not running in the requested application unit")
        if stop_migrated_owner(own.group):
            print("wsctl: migrated application main exited before retained children stopped")
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        print(f"wsctl: graphical application stop could not be verified: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
