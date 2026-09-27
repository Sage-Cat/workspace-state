"""Bounded independent jobs, joined before their coordinator may proceed."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
import threading
from typing import Callable, Iterator, TypeVar

T = TypeVar("T")
MAX_WORKERS = 4
_context = threading.local()


def completed_jobs(jobs: dict[str, Callable[[], T]], *, workers: int = MAX_WORKERS,
                   serial: bool = False) -> Iterator[tuple[str, T | None, Exception | None]]:
    """Yield completions, not submission order; never abandon running workers.

    Jobs return independent values; callers merge/persist on the coordinator.
    One job's failure does not cancel unrelated restoration jobs. Dry runs can
    use the serial path to keep output deterministic and avoid worker threads.
    """
    # A social-app category can use this helper too. Nested pools would multiply
    # the limit (or deadlock if sharing one executor); let its existing worker
    # run those subjobs while the other category workers continue concurrently.
    if serial or len(jobs) < 2 or getattr(_context, "worker", False):
        for name, job in jobs.items():
            try:
                result = job()
            except Exception as error:
                yield name, None, error
            else:
                yield name, result, None
        return
    def execute(job):
        _context.worker = True
        try:
            return job()
        finally:
            _context.worker = False

    with ThreadPoolExecutor(max_workers=max(1, min(workers, MAX_WORKERS)),
                            thread_name_prefix="wsctl") as pool:
        futures = {pool.submit(copy_context().run, execute, job): name for name, job in jobs.items()}
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                yield futures[future], None, error
            else:
                yield futures[future], result, None
