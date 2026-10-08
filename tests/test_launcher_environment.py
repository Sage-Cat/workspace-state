import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class LauncherEnvironmentTests(unittest.TestCase):
    def test_finalizer_finds_companion_with_minimal_systemd_path(self):
        source = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / 'home'
            bin_dir = root / 'package/bin'
            modules = root / 'package/src/workspace_state'
            tools = home / '.local/bin'
            for path in (bin_dir, modules, tools):
                path.mkdir(parents=True)
            for name in ('wsctl-login-finalize', 'wsctl-python'):
                shutil.copy2(source / 'bin' / name, bin_dir / name)
            (modules / '__init__.py').write_text('')
            (modules / 'login_finalize.py').write_text(
                'import json, os, shutil, subprocess\n'
                'tool = shutil.which("gnome-winctl")\n'
                'assert tool is not None, "Missing desktop companion"\n'
                'print(json.dumps({"tool": tool, "reply": subprocess.check_output([tool], text=True).strip(), '
                '"bin": os.environ["WSCTL_BIN_DIR"]}))\n')
            companion = tools / 'gnome-winctl'
            companion.write_text('#!/bin/sh\nprintf \'desktop-ready\\n\'\n')
            companion.chmod(0o755)
            result = subprocess.run([str(bin_dir / 'wsctl-login-finalize')], check=True,
                capture_output=True, text=True, env={'HOME': str(home), 'PATH': '/usr/bin:/bin'})
            observed = json.loads(result.stdout)
            self.assertEqual(observed, {'tool': str(companion), 'reply': 'desktop-ready', 'bin': str(bin_dir)})


if __name__ == '__main__':
    unittest.main()
