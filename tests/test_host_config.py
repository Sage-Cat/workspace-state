import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import synthetic_drives
from workspace_state import file_manager, host_config, vscode


class HostConfigTests(unittest.TestCase):
    def read(self, text):
        path = synthetic_drives.install(self, text)
        return host_config.load(path)

    def test_a_missing_file_means_no_drives_and_is_not_an_error(self):
        path = synthetic_drives.install(self, False)
        config = host_config.load(path)
        self.assertEqual((config.present, config.drives, config.error), (False, (), None))
        self.assertEqual((host_config.mounts(), host_config.storage_roots()), ({}, ()))
        self.assertFalse(host_config.is_storage_path(Path("/home/example/Drives/drive-a/x")))

    def test_the_default_location_follows_xdg_config_home(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"XDG_CONFIG_HOME": directory}):
            self.assertEqual(host_config.config_path(), Path(directory) / "workspace-state/host.toml")
            self.assertFalse(host_config.load().present)

    def test_drives_are_read_in_order_with_start_defaulting_to_true(self):
        config = self.read(synthetic_drives.toml() + '\n[[drives]]\nstage = "vm-desktop"\nlabel = "VM"\n'
                           'unit = "example-vm.mount"\nmountpoint = "/home/example/Drives/vm/Desktop"\nstart = false\n')
        self.assertIsNone(config.error)
        self.assertEqual([drive.stage for drive in config.drives], ["drive-a", "drive-b", "drive-c", "vm-desktop"])
        self.assertEqual([drive.stage for drive in config.started], ["drive-a", "drive-b", "drive-c"])
        self.assertEqual(config.drives[0].mountpoint, Path("/home/example/Drives/drive-a"))
        self.assertEqual(host_config.mounts()[Path("/home/example/Drives/vm/Desktop")], "example-vm.mount")
        self.assertEqual(host_config.storage_roots(),
                         (Path("/home/example/Drives"), Path("/home/example/Drives/vm")))
        self.assertTrue(host_config.is_storage_path(Path("/home/example/Drives/anything/else")))
        self.assertFalse(host_config.is_storage_path(Path("/home/example/Projects")))

    def test_an_invalid_file_gives_no_drives_and_one_exact_message(self):
        good = 'stage = "drive-a"\nlabel = "A"\nunit = "example-a.service"\nmountpoint = "/home/example/Drives/a"\n'
        cases = {
            "x = ": "",
            "unknown = 1": "unknown top-level key: unknown",
            'drives = "no"': "array of tables",
            "[[drives]]\n" + good + "extra = 1": "unknown key: extra",
            "[[drives]]\n" + good.replace('stage = "drive-a"', 'stage = "Bad Stage"'): "invalid or reserved stage",
            "[[drives]]\n" + good.replace('"drive-a"', '"warmup"'): "invalid or reserved stage: warmup",
            "[[drives]]\n" + good.replace("example-a.service", "a b"): "invalid unit name",
            "[[drives]]\n" + good.replace("/home/example/Drives/a", "Drives/a"): "absolute mountpoint",
            "[[drives]]\n" + good.replace("/home/example/Drives/a", "/mnt"): "absolute mountpoint",
            "[[drives]]\n" + good + "start = 'yes'": "start must be true or false",
            "[[drives]]\n" + good + "[[drives]]\n" + good: "share a stage",
        }
        for text, message in cases.items():
            with self.subTest(message=message or "toml syntax"):
                config = self.read(text)
                self.assertEqual(config.drives, ())
                self.assertTrue(config.present)
                self.assertIn(message, config.error)
                self.assertEqual(host_config.mounts(), {})

    def test_a_changed_file_is_read_again(self):
        path = synthetic_drives.install(self, synthetic_drives.toml(synthetic_drives.DRIVES[:1]))
        self.assertEqual(len(host_config.load(path).drives), 1)
        path.write_text(synthetic_drives.toml() + "# longer, so the size differs\n")
        self.assertEqual(len(host_config.load(path).drives), 3)

    def test_an_unreadable_file_is_an_error_message_not_an_exception(self):
        path = synthetic_drives.install(self)
        with patch.object(Path, "read_text", side_effect=PermissionError(13, "Permission denied")):
            host_config._cache.clear()
            self.assertIn("Permission denied", host_config.load(path).error)


class StorageGateTests(unittest.TestCase):
    def test_storage_paths_need_a_configured_drive_for_the_gates(self):
        synthetic_drives.install(self)
        record = {"windows": [{"locations": ["file:///home/example/Drives/drive-b/x"]}]}
        self.assertTrue(file_manager.needs_storage(record))
        self.assertFalse(file_manager.needs_storage({"windows": [{"locations": ["file:///home/example/Documents"]}]}))

    def test_without_a_configuration_no_path_is_storage_backed(self):
        synthetic_drives.install(self, False)
        self.assertFalse(file_manager.needs_storage({"windows": [{"locations": ["file:///home/example/Drives/x"]}]}))

    def test_an_unmanaged_sibling_of_a_configured_drive_is_refused(self):
        synthetic_drives.install(self)
        with self.assertRaisesRegex(file_manager.CommandError, "unmanaged"):
            file_manager._check_folder("file:///home/example/Drives/other/x")
        with self.assertRaisesRegex(vscode.CommandError, "unmanaged"):
            vscode._check_resource("file:///home/example/Drives/other/x", folder=True)


if __name__ == "__main__":
    unittest.main()
