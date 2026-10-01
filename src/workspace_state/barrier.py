"""Finite startup-worker join before shutdown checkpoint capture.

Stopping worker units does not close their GUI applications. Companion mutations
can outlive a cancelled RPC, so they must also become observably quiescent.
Cleanup uses inherited authority but never adopts or renews an operation.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from . import browser, operations
from .login_status import append_diagnostic

STOP_COMMAND = (
    "systemctl", "--user", "stop",
    "wsctl-startup-restore-*.service",
    "wsctl-restore-worker-*.service",
    "wsctl-login-finalize.service",
)
STOP_SECONDS = 25.0
COMPANION_SECONDS = 10.0
TOTAL_SECONDS = 35.0
REAP_SECONDS = .25
COMPANION_FAILURE = 3


def _kill_stop_group(process: subprocess.Popen, deadline: float) -> None:
    # This group contains our systemctl client, never units owned by systemd.
    # Kill the group even if its leader exited but a descendant holds a pipe.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=max(.001, min(REAP_SECONDS, deadline - time.monotonic())))
    except subprocess.TimeoutExpired:
        # Kernel-uninterruptible children cannot justify an unbounded join.
        # Failure prevents checkpointing; no process authority is adopted here.
        pass


def stop_startup_workers(*, timeout: float = STOP_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(
        STOP_COMMAND, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True, start_new_session=True, env=operations.child_environment(),
    )
    try:
        # Reserve cleanup time inside the stop budget, not after it.
        _, error = process.communicate(timeout=max(.001, deadline - time.monotonic() - REAP_SECONDS))
    except subprocess.TimeoutExpired as error:
        _kill_stop_group(process, deadline)
        raise RuntimeError("startup workers did not stop within the allotted time") from error
    except BaseException:
        _kill_stop_group(process, deadline)
        raise
    if process.returncode:
        detail = " ".join((error or "").split())[:500]
        raise RuntimeError(f"startup worker stop failed ({process.returncode})" + (f": {detail}" if detail else ""))


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv:
        print("usage: wsctl-startup-barrier", file=sys.stderr)
        return 2
    deadline = time.monotonic() + TOTAL_SECONDS
    phase = 'startup workers'
    try:
        stop_startup_workers(timeout=min(STOP_SECONDS, max(0, deadline - time.monotonic())))
        phase = 'Chrome companion'
        remaining = min(COMPANION_SECONDS, deadline - time.monotonic())
        if remaining <= 0:
            raise RuntimeError("startup quiescence deadline expired")
        result = browser.wait_for_quiescence(timeout=remaining, release_expired=True)
        if result is False:
            raise RuntimeError("Chrome companion mutations remain active")
    except (OSError, ValueError, RuntimeError) as error:
        # A busy telemetry lock must not extend the finite shutdown barrier.
        append_diagnostic(f'Shutdown barrier: {phase}', str(error), blocking=False)
        print(f"wsctl: startup barrier failed: {error}", file=sys.stderr)
        return COMPANION_FAILURE if phase == 'Chrome companion' else 1
    print("Startup workers stopped; Chrome companion mutations are quiescent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
