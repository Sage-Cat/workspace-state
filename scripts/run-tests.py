#!/usr/bin/env python3
"""Run unit/process tests with no access to the live desktop's mutable state."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

project = Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory(prefix="wsctl-unit-tests-") as temporary:
    root = Path(temporary)
    environment = dict(os.environ)
    for key, directory in {"XDG_RUNTIME_DIR": "runtime", "XDG_STATE_HOME": "state",
                           "XDG_DATA_HOME": "data", "XDG_CONFIG_HOME": "config",
                           "XDG_CACHE_HOME": "cache"}.items():
        path = root / directory
        path.mkdir(mode=0o700)
        environment[key] = str(path)
    environment["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={root}/runtime/no-live-bus"
    environment["PYTHONPATH"] = str(project / "src")
    environment.pop("WSCTL_OPERATION_CONTEXT", None)
    # HOME stays unchanged. Individual fixtures own every native-profile input;
    # actual application integration is opt-in in the private desktop harness.
    result = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                             *(sys.argv[1:] or ["-v"])], cwd=project, env=environment)
    raise SystemExit(result.returncode)
