"""A clock for the module under test only.

`patch.object(cli.time, "sleep")` replaces the function in the global `time`
module, so every thread and module sees the mock and consumes its side effects.
Replacing the module's own `time` name affects only that module.
"""
import time
from unittest.mock import Mock


def clock(*, monotonic=None, sleep=True):
    """A stand-in for a module's `time`: real except the mocked monotonic and sleep."""
    stand_in = Mock(wraps=time)
    if monotonic is not None:
        stand_in.monotonic = Mock(side_effect=monotonic)
    if sleep:
        stand_in.sleep = Mock()
    return stand_in
