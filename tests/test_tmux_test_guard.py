"""The test-run guard refuses tmux clients outside the isolated socket directory."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "guard"
ALLOWED = "/nonexistent/allowed-tmux-dir"


def attempt(snippet, *, with_dir=False):
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(GUARD),
                   "WSCTL_TEST_TMUX_TMPDIR": ALLOWED}
    if with_dir:
        environment["TMUX_TMPDIR"] = ALLOWED
    program = ("import os, subprocess\n"
               "try:\n"
               + "".join("    " + line + "\n" for line in snippet.strip().splitlines())
               + "    print('ALLOWED')\n"
               "except RuntimeError as error:\n"
               "    print('REFUSED' if 'isolated test socket' in str(error) else 'OTHER ' + str(error))\n"
               "except Exception as error:\n"
               "    print('ALLOWED-BUT-FAILED')\n")
    return subprocess.run([sys.executable, "-c", program], env=environment, capture_output=True,
                          text=True, timeout=30).stdout.strip().splitlines()[-1]


class TmuxTestGuardTests(unittest.TestCase):
    PATHS = {
        "direct": "subprocess.run(['tmux', '-V'], env={'PATH': '/usr/bin'})",
        "no env at all": "subprocess.run(['tmux', '-V'])",
        "env wrapper": "subprocess.run(['env', 'A=1', 'tmux', '-V'], env={'PATH': '/usr/bin'})",
        "systemd-run": "subprocess.Popen(['systemd-run', '--user', '--', '/usr/bin/tmux', '-V'], env={})",
        "shell string": "subprocess.run('tmux -V', shell=True, env={})",
        "shell in argv": "subprocess.run(['sh', '-c', 'echo; /usr/bin/tmux ls'], env={})",
        "os.system": "os.system('tmux -V')",
        "execvp": "os.execvp('tmux', ['tmux', '-V'])",
        "execv": "os.execv('/usr/bin/tmux', ['/usr/bin/tmux', '-V'])",
        "execve": "os.execve('/usr/bin/tmux', ['tmux', '-V'], {'PATH': '/usr/bin'})",
        "execl": "os.execl('/usr/bin/tmux', 'tmux', '-V')",
        "posix_spawn": "os.posix_spawn('/usr/bin/tmux', ['tmux', '-V'], {'PATH': '/usr/bin'})",
        "posix_spawnp": "os.posix_spawnp('tmux', ['tmux', '-V'], {})",
    }

    def test_every_path_to_tmux_without_the_isolated_directory_is_refused(self):
        for label, snippet in self.PATHS.items():
            with self.subTest(label):
                self.assertEqual(attempt(snippet), "REFUSED")

    def test_the_isolated_directory_in_the_environment_is_accepted(self):
        for label, snippet in (
                ("inherited", "subprocess.run(['tmux', '-V'])"),
                ("explicit", f"subprocess.run(['env', 'tmux', '-V'], env={{'TMUX_TMPDIR': {ALLOWED!r}}})"),
                ("system", "os.system('tmux -V >/dev/null 2>&1')")):
            with self.subTest(label):
                self.assertEqual(attempt(snippet, with_dir=True), "ALLOWED")
        # A wrong directory is refused even when the variable is set.
        self.assertEqual(attempt("subprocess.run(['tmux', '-V'], env={'TMUX_TMPDIR': '/live'})", with_dir=True),
                         "REFUSED")

    def test_commands_that_only_mention_a_longer_name_are_not_tmux(self):
        for snippet in ("subprocess.run(['echo', 'tmux-resurrect'])", "subprocess.run('echo atmuxb', shell=True)",
                        "os.system('true')"):
            with self.subTest(snippet):
                self.assertEqual(attempt(snippet), "ALLOWED")

    def test_the_test_runner_installs_the_guard(self):
        self.assertTrue(os.environ.get("WSCTL_TEST_TMUX_TMPDIR"))
        self.assertEqual(os.environ["WSCTL_TEST_TMUX_TMPDIR"], os.environ["TMUX_TMPDIR"])
        self.assertIn("wsctl-unit-tests-", os.environ["TMUX_TMPDIR"])
        with self.assertRaisesRegex(RuntimeError, "isolated test socket"):
            subprocess.run(["tmux", "-V"], env={"PATH": "/usr/bin"})


if __name__ == "__main__":
    unittest.main()
