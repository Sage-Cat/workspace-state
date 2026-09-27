import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

path = Path(__file__).resolve().parents[1] / 'host-integration/bin/gnome-kms-recover'
loader = importlib.machinery.SourceFileLoader('kms_recovery', str(path))
spec = importlib.util.spec_from_loader(loader.name, loader)
kms = importlib.util.module_from_spec(spec)
loader.exec_module(kms)


class KmsRecoveryTests(unittest.TestCase):
    def test_unarmed_hook_has_no_effect(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory}), patch.object(kms.subprocess, 'run') as run:
            self.assertEqual(kms.main([]), 0)
            run.assert_not_called()

    def test_later_user_edit_is_preserved_then_exact_trial_can_recover(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory + '/state', 'XDG_CONFIG_HOME': directory + '/config'}), patch.object(kms.subprocess, 'run') as run:
            root = Path(directory)
            trial, fallback = root / 'trial', root / 'fallback'
            trial.write_text('[Service]\nEnvironment=TRIAL=yes\n')
            fallback.write_text('[Service]\nEnvironment=FALLBACK=yes\n')
            kms.main(['--arm', '--trial', str(trial), '--fallback', str(fallback)])
            target = root / 'config/systemd/user/org.gnome.Shell@wayland.service.d/50-nvidia-kms-compat.conf'
            target.parent.mkdir(parents=True)
            target.write_text('later edit')
            kms.main([])
            self.assertEqual(target.read_text(), 'later edit'); run.assert_not_called()
            target.write_bytes(trial.read_bytes())
            kms.main([])
            self.assertEqual(target.read_bytes(), fallback.read_bytes())
            self.assertEqual(run.call_count, 2)
            self.assertFalse((root / 'state/workspace-state/kms-recovery/armed.json').exists())

    def test_changed_fallback_is_rejected_before_target_mutation(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_STATE_HOME': directory + '/state', 'XDG_CONFIG_HOME': directory + '/config'}):
            root = Path(directory)
            trial, fallback = root / 'trial', root / 'fallback'
            trial.write_text('trial'); fallback.write_text('fallback')
            kms.main(['--arm', '--trial', str(trial), '--fallback', str(fallback)])
            target = root / 'config/systemd/user/org.gnome.Shell@wayland.service.d/50-nvidia-kms-compat.conf'
            target.parent.mkdir(parents=True); target.write_text('trial')
            (root / 'state/workspace-state/kms-recovery/fallback.conf').write_text('changed')
            with self.assertRaises(ValueError):kms.main([])
            self.assertEqual(target.read_text(), 'trial')


if __name__ == '__main__':unittest.main()
