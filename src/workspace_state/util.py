from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Iterable


class CommandError(RuntimeError):
    pass


def launch_graphical_service(args: Iterable[str], purpose: str) -> str:
    """Launch one GUI process tree outside the finite restore-worker cgroup.

    ExitType=cgroup is essential for Chromium: GNOME may move the browser's
    main process into its application scope after it starts, while renderer
    children remain in the original unit. The transient unit must stay alive
    until that remaining cgroup is empty instead of killing those children
    when the original main PID moves away.
    """
    command = list(args)
    component = re.sub(r"[^A-Za-z0-9_.-]+", "-", purpose).strip("-.")[:40] or "gui"
    unit = f"wsctl-app-{component}-{uuid.uuid4().hex[:8]}.service"
    invocation = [
        "/usr/bin/systemd-run", "--user", "--quiet", "--collect",
        "--service-type=exec", f"--unit={unit}",
        "--property=ExitType=cgroup",
        "--property=PartOf=graphical-session.target",
        "--property=After=graphical-session.target",
        "--property=TimeoutStopSec=10s",
        "--property=KillMode=mixed",
        "--", *command,
    ]
    try:
        result = subprocess.run(
            invocation, text=True, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CommandError(f"could not launch {purpose} as a graphical service: {error}") from error
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CommandError(f"could not launch {purpose} as a graphical service: {detail}")
    return unit


def run(args: Iterable[str], *, check: bool = True, timeout: float = 10) -> str:
    command = list(args)
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise CommandError(f"{' '.join(command)}: timed out after {timeout:g} seconds") from error
    if check and result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise CommandError(f"{' '.join(command)}: {detail}")
    return result.stdout


def data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "workspace-state"


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        path.chmod(0o600)
        # Persist the directory entry as well as the JSON contents. Shutdown
        # authorization files must survive an immediate power transition once
        # their atomic rename becomes visible.
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
