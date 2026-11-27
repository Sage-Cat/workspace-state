"""Refuse to run with the live desktop's runtime directory.

scripts/run-tests.py gives every run private XDG directories. A bare
`unittest` run would otherwise write markers, sockets and status files into
the real /run/user/<uid>/workspace-state.
"""
import os
from pathlib import Path
import sys


def live_runtime_directory(environment=None):
    """The real per-user runtime directory in use, else None."""
    value = (os.environ if environment is None else environment).get("XDG_RUNTIME_DIR")
    if not value:
        return "(XDG_RUNTIME_DIR is unset)"
    path = Path(value).resolve()
    live = {Path(f"/run/user/{os.getuid()}").resolve()}
    return str(path) if path in live or path.parent == Path("/run/user") else None


def isolation_message(environment=None):
    live = live_runtime_directory(environment)
    if live is None:
        return None
    return (f"Refusing to run tests against the live runtime directory {live}.\n"
            "Run them through scripts/run-tests.py (or `make check`), which provides private "
            "XDG_RUNTIME_DIR, XDG_STATE_HOME and XDG_DATA_HOME directories.")


def require_isolated_runtime():
    """End the whole process: unittest's loader would turn an exception into one failed test."""
    message = isolation_message()
    if message is not None:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()
        os._exit(2)
