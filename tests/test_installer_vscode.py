import importlib.util
import json
from unittest.mock import patch
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('install_vscode', ROOT / 'scripts/install-vscode-extension.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

class VscodeInstallerTests(unittest.TestCase):
    def test_named_profiles_use_the_actual_code_registry(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / 'Code'
            (data / 'User/profiles/abc123').mkdir(parents=True)
            registry = data / 'User/globalStorage/storage.json'
            registry.parent.mkdir()
            registry.write_text(json.dumps({'userDataProfiles': [
                {'name': 'Research', 'location': {'scheme': 'file', 'path': str(data / 'User/profiles/abc123')}},
                {'name': 'Deleted', 'location': {'scheme': 'file', 'path': str(data / 'User/profiles/deleted')}},
            ]}))
            self.assertEqual(installer.existing_profiles(str(data)), ['Research'])
            with patch.dict(installer.os.environ, {'XDG_CONFIG_HOME': d}):
                self.assertEqual(installer.existing_profiles(None), ['Research'])

    def test_missing_profile_cannot_be_created_during_installation(self):
        with tempfile.TemporaryDirectory() as d, patch.object(installer.subprocess, 'run') as run:
            with self.assertRaises(SystemExit):
                installer.main(['--output', str(Path(d) / 'companion.vsix'),
                                '--user-data-dir', d, '--profile', 'Missing'])
            run.assert_not_called()

    def test_vsix_contains_complete_extension(self):
        with tempfile.TemporaryDirectory() as d:
            out = installer.make_vsix(Path(d) / 'companion.vsix')
            with zipfile.ZipFile(out) as z:
                names = set(z.namelist())
                self.assertEqual(names, {'[Content_Types].xml', 'extension.vsixmanifest', 'extension/package.json', 'extension/extension.js'})
                manifest = json.loads(z.read('extension/package.json'))
                self.assertEqual(manifest['main'], './extension.js')
                self.assertEqual(manifest['extensionKind'], ['ui'])
                self.assertEqual(manifest['activationEvents'], ['onStartupFinished'])
                xml = z.read('extension.vsixmanifest').decode()
                self.assertIn('Id="workspace-state-companion"', xml)
                self.assertIn('Version="0.1.0"', xml)

    def test_package_only_does_not_run_code(self):
        with tempfile.TemporaryDirectory() as d:
            out = installer.make_vsix(Path(d) / 'companion.vsix')
            self.assertTrue(out.exists())

    def test_install_passes_isolated_directories(self):
        with tempfile.TemporaryDirectory() as d, patch.object(installer.subprocess, 'run') as run:
            output = Path(d) / 'companion.vsix'
            installer.main(['--output', str(output), '--code', '/bin/code-test', '--force',
                            '--user-data-dir', '/tmp/user-data', '--extensions-dir', '/tmp/extensions'])
            run.assert_called_once_with(['/bin/code-test', '--install-extension', str(output), '--force',
                                         '--user-data-dir', '/tmp/user-data', '--extensions-dir', '/tmp/extensions'], check=True, timeout=90)

    def test_package_only_main_does_not_call_code(self):
        with tempfile.TemporaryDirectory() as d, patch.object(installer.subprocess, 'run') as run:
            installer.main(['--package-only', '--output', str(Path(d) / 'x.vsix')])
            run.assert_not_called()

if __name__ == '__main__':
    unittest.main()
