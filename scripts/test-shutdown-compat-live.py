#!/usr/bin/env python3
"""Exercise the installed shutdown-status compatibility helper safely.

Only disposable per-user transient units are created.  This deliberately does
not attempt to emulate system shutdown: the helper must reject exit 1 while
the real system is running, so the first unit must remain failed.
"""

from __future__ import annotations

import subprocess
import sys
import time
import uuid


HELPER = "/usr/local/libexec/wsctl-livepatch-stop-check"
PREFIX = "wsctl-shutdown-compat-probe-"
TIMEOUT = 15


def run(command: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def show_result(unit: str) -> str:
    result = run(["systemctl", "--user", "show", unit, "-p", "Result", "--value"], check=True)
    return result.stdout.strip()


def cleanup(units: list[str]) -> None:
    for unit in units:
        run(["systemctl", "--user", "stop", unit])
        run(["systemctl", "--user", "reset-failed", unit])


def fail(message: str) -> None:
    raise RuntimeError(message)


def main() -> int:
    if run(["test", "-x", HELPER]).returncode != 0:
        fail(f"missing executable helper: {HELPER}")

    units: list[str] = []
    try:
        first = PREFIX + uuid.uuid4().hex + ".service"
        units.append(first)
        command = [
            "systemd-run",
            "--user",
            "--unit=" + first.removesuffix(".service"),
            "--property=Type=simple",
            "--property=CollectMode=inactive",
            "--wait",
            "--property=Restart=no",
            "--property=SuccessExitStatus=1",
            f"--property=ExecStopPost=/bin/sh -c '{HELPER} || exit 2'",
            "/usr/bin/false",
        ]
        completed = run(command)
        # No stop request: exercise a spontaneous main-process exit, exactly
        # as an unexpected runtime failure in the real Type=simple service.
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            state = run(["systemctl", "--user", "show", first, "-p", "ActiveState", "--value"])
            if state.stdout.strip() in {"failed", "inactive"}:
                break
            time.sleep(0.05)
        else:
            fail("runtime-failure probe did not finish before deadline")
        first_result = show_result(first)
        if first_result != "exit-code":
            fail(f"runtime exit 1 was hidden: {first} Result={first_result!r}")
        print(f"PASS runtime exit 1 preserved: Result={first_result}")
        if completed.returncode == 0:
            fail("systemd-run unexpectedly reported success for rejected helper")

        second = PREFIX + uuid.uuid4().hex + ".service"
        units.append(second)
        # RemainAfterExit keeps a oneshot active, so --wait would wait forever;
        # start it asynchronously, then inspect its retained result.
        completed = run([
            "systemd-run",
            "--user",
            "--unit=" + second.removesuffix(".service"),
            "--property=Type=oneshot",
            "--property=RemainAfterExit=yes",
            "--property=SuccessExitStatus=143",
            "/bin/sh",
            "-c",
            "exit 143",
        ])
        if completed.returncode != 0:
            fail("systemd-run could not start exit-143 probe")
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            state = run(["systemctl", "--user", "show", second, "-p", "ActiveState", "--value"])
            if state.stdout.strip() == "active":
                break
            time.sleep(0.1)
        else:
            fail("exit-143 probe did not reach active/exited before deadline")
        second_result = show_result(second)
        if second_result != "success":
            fail(f"exit 143 was not accepted: {second} Result={second_result!r}")
        print(f"PASS CUPS-style exit 143 accepted: Result={second_result}")
        return 0
    finally:
        cleanup(units)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
