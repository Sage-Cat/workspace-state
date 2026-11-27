"""Refuse any tmux client outside the isolated socket directory of a test run.

`scripts/run-tests.py` puts this directory first on PYTHONPATH and names the
run's private directory in WSCTL_TEST_TMUX_TMPDIR. A test that replaces the
environment of a tmux client and drops TMUX_TMPDIR would reach the desktop's
own server; here it fails instead. The guard covers `subprocess`, `os.system`,
`os.exec*` and `os.posix_spawn*`, and sees tmux behind wrappers (`env`,
`systemd-run -- tmux`) and in shell strings.
"""
import os
import re
import subprocess

_ALLOWED = os.environ.get("WSCTL_TEST_TMUX_TMPDIR")
_WORD = re.compile(r"(?<![\w-])tmux(?![\w-])")


def _mentions_tmux(command):
    if isinstance(command, (str, bytes, os.PathLike)):
        return bool(_WORD.search(os.fsdecode(command)))
    return any(_WORD.search(os.fsdecode(part)) for part in command if isinstance(part, (str, bytes, os.PathLike)))


def _is_tmux_argument(part):
    return os.path.basename(os.fsdecode(part)) == "tmux"


def _check(command, environment):
    if isinstance(command, (str, bytes, os.PathLike)):
        suspicious = _mentions_tmux(command)
    else:
        parts = list(command)
        suspicious = any(_is_tmux_argument(part) for part in parts if isinstance(part, (str, bytes, os.PathLike)))
        # Shell strings handed to `sh -c` and the like.
        shells = {"sh", "bash", "dash", "zsh", "fish", "ksh"}
        if parts and os.path.basename(os.fsdecode(parts[0])) in shells and "-c" in parts:
            suspicious = suspicious or any(_mentions_tmux(part) for part in parts[1:]
                                           if isinstance(part, (str, bytes)))
    if not suspicious:
        return
    environment = os.environ if environment is None else environment
    if _env_value(environment, "TMUX_TMPDIR") != _ALLOWED:
        raise RuntimeError("tmux client outside the isolated test socket directory (TMUX_TMPDIR): "
                           + (command if isinstance(command, str) else " ".join(map(os.fsdecode, list(command)[:5]))))


def _env_value(environment, name):
    if isinstance(environment, dict):
        value = environment.get(name, environment.get(name.encode()))
    else:
        value = environment[name] if name in environment else None
    return os.fsdecode(value) if value is not None else None


if _ALLOWED:
    _real_init = subprocess.Popen.__init__

    def _popen(self, args, *positional, **keywords):
        _check(args, keywords.get("env"))
        _real_init(self, args, *positional, **keywords)

    subprocess.Popen.__init__ = _popen

    _real_system = os.system

    def _system(command):
        _check(command, None)
        return _real_system(command)

    os.system = _system

    def _wrap_exec(name, env_position):
        real = getattr(os, name, None)
        if real is None:
            return

        def guarded(path, *arguments, **keywords):
            vector = arguments[0] if arguments and not name.startswith("execl") else arguments
            environment = keywords.get("env")
            if name in ("execve", "execvpe"):
                environment = arguments[1] if len(arguments) > 1 else environment
            elif name in ("execle", "execlpe"):
                environment = arguments[-1] if arguments else None
            if name.startswith("posix_spawn"):
                environment = arguments[1] if len(arguments) > 1 else keywords.get("env")
            _check([path, *(vector if isinstance(vector, (list, tuple)) else [vector])]
                   if not isinstance(vector, (str, bytes)) else [path, vector], environment)
            return real(path, *arguments, **keywords)

        setattr(os, name, guarded)

    for _name in ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe",
                  "posix_spawn", "posix_spawnp"):
        _wrap_exec(_name, None)
