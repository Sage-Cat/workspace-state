#!/usr/bin/env python3
"""Harmless I/O-wait benchmark; never loads a recipe or invokes a desktop app."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_state.concurrency import completed_jobs


def benchmark(serial: bool) -> float:
    started = time.monotonic()
    jobs = {f"simulated-job-{i}": lambda: time.sleep(.4) for i in range(4)}
    results = list(completed_jobs(jobs, serial=serial))
    if any(error is not None for _, _, error in results):
        raise RuntimeError("Simulated job failed")
    return time.monotonic() - started


if __name__ == "__main__":
    serial = benchmark(True)
    parallel = benchmark(False)
    print(json.dumps({
        "simulation_only": True, "jobs": 4, "wait_per_job_seconds": .4,
        "serial_seconds": round(serial, 3), "parallel_seconds": round(parallel, 3),
        "speedup": round(serial / parallel, 2),
        "actual_desktop_apps_or_power_actions": False,
    }, indent=2))
