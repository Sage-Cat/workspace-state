"""A synthetic private host configuration for tests: no real drive or user names."""
import tempfile
from pathlib import Path
from unittest.mock import patch

from workspace_state import host_config

HOME = "/home/example"
DRIVES = (
    ("drive-a", "Drive A", "example-drive-a.service", f"{HOME}/Drives/drive-a"),
    ("drive-b", "Drive B", "example-drive-b.service", f"{HOME}/Drives/drive-b"),
    ("drive-c", "Drive C", "example-drive-c.service", f"{HOME}/Drives/drive-c"),
)
NESTED = ("vm-desktop", "VM desktop", "example-vm-desktop.service", f"{HOME}/Drives/vm/Desktop")


def toml(drives=DRIVES, *, extra=()):
    lines = []
    for stage, label, unit, mountpoint in drives:
        lines += ["[[drives]]", f'stage = "{stage}"', f'label = "{label}"', f'unit = "{unit}"',
                  f'mountpoint = "{mountpoint}"', ""]
    return "\n".join([*lines, *extra])


def install(testcase, text=None):
    """Point host_config at a private file holding `text` (the synthetic drives by default)."""
    directory = tempfile.TemporaryDirectory()
    testcase.addCleanup(directory.cleanup)
    path = Path(directory.name) / "host.toml"
    if text is not False:
        path.write_text(toml() if text is None else text)
    patcher = patch.object(host_config, "config_path", return_value=path)
    patcher.start()
    testcase.addCleanup(patcher.stop)
    return path
