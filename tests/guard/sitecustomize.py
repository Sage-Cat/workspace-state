"""Refuse any tmux client outside the isolated socket directory of a test run.

`scripts/run-tests.py` puts this directory first on PYTHONPATH and names the
run's private directory in WSCTL_TEST_TMUX_TMPDIR. A test that replaces the
environment of a tmux client and drops TMUX_TMPDIR would reach the desktop's
own server; here it fails instead.
"""
import os
import subprocess

_ALLOWED = os.environ.get("WSCTL_TEST_TMUX_TMPDIR")

if _ALLOWED:
    _real_init = subprocess.Popen.__init__

    def _guarded_init(self, args, *positional, **keywords):
        command = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args)
        if command and os.path.basename(os.fsdecode(command[0])) == "tmux":
            environment = keywords.get("env")
            environment = os.environ if environment is None else environment
            if environment.get("TMUX_TMPDIR") != _ALLOWED:
                raise RuntimeError(
                    "tmux client outside the isolated test socket directory (TMUX_TMPDIR): "
                    + " ".join(map(os.fsdecode, command[:4])))
        _real_init(self, args, *positional, **keywords)

    subprocess.Popen.__init__ = _guarded_init
