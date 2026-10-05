from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).parent / 'integration'
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location('vm_ordinary_test_module', HERE / 'run_vm_ordinary.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)
sys.path.remove(str(HERE))


class OrdinaryReleaseOwnershipTests(unittest.TestCase):
    old = 'r-' + '1' * 24
    new = 'r-' + '2' * 24

    def test_undeclared_upgrade_or_failed_activation_cannot_pass(self):
        for before, actual in [({'installed_release': self.old}, self.new),
                               ({'installed_release': self.old, 'expected_next_release': self.new}, self.old)]:
            with self.assertRaises(RuntimeError):
                harness.require_boot_release(before, actual)

    def test_exact_predeclared_upgrade_and_ordinary_same_release_are_allowed(self):
        harness.require_boot_release({'installed_release': self.old}, self.old)
        harness.require_boot_release({'installed_release': self.old, 'expected_next_release': self.new}, self.new)

    def test_guarded_environment_component_path_compares_exact_release(self):
        root = '/home/tester/.local/share/workspace-state/desktop-releases/'
        actual = root + self.new + '/components/workspace-state'
        harness.require_boot_release({'installed_release': self.old, 'expected_next_release': self.new}, actual)
        with self.assertRaises(RuntimeError):
            harness.require_boot_release({'installed_release': root + self.old + '/components/workspace-state'}, actual)

    def test_malformed_preparation_evidence_is_rejected(self):
        for expected in [None, 'latest', 'r-' + '2' * 23, '../../current']:
            with self.assertRaises(RuntimeError):
                harness.require_boot_release({'installed_release': self.old, 'expected_next_release': expected}, expected)


if __name__ == '__main__':
    unittest.main()
