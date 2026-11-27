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
                           'unit = "example-vm.service"\nmountpoint = "/home/example/Drives/vm/Desktop"\nstart = false\n')
        self.assertIsNone(config.error)
        self.assertEqual([drive.stage for drive in config.drives], ["drive-a", "drive-b", "drive-c", "vm-desktop"])
        self.assertEqual([drive.stage for drive in config.started], ["drive-a", "drive-b", "drive-c"])
        self.assertEqual(config.drives[0].mountpoint, Path("/home/example/Drives/drive-a"))
        self.assertEqual(host_config.mounts()[Path("/home/example/Drives/vm/Desktop")], "example-vm.service")
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

    def test_mountpoints_that_could_break_a_unit_file_are_refused(self):
        for bad in ("/home/example/Drives/a b", "/home/example/Drives/a%h", '/home/example/Drives/a\\"b'):
            with self.subTest(bad=bad):
                text = synthetic_drives.toml((("drive-a", "A", "example-a.service", bad),))
                self.assertIn("without spaces", self.read(text).error)

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


class DropinTests(unittest.TestCase):
    ROOT = Path(__file__).resolve().parents[1]

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dest = Path(directory.name) / "user"
        self.dest.mkdir()

    def install(self, text):
        synthetic_drives.install(self, text)
        return host_config.install_dropins(self.ROOT / "systemd", self.dest)

    def test_each_started_drive_gets_a_restore_gate_and_its_own_unmount(self):
        units = self.install(synthetic_drives.toml()
                             + '\n[[drives]]\nstage = "vm-desktop"\nlabel = "VM"\nunit = "example-vm.service"\n'
                             'mountpoint = "/home/example/Drives/vm/Desktop"\nstart = false\n')
        self.assertEqual(units, ["example-drive-a.service", "example-drive-b.service", "example-drive-c.service"])
        gate = (self.ROOT / "systemd/post-workspace-drive.conf").read_text()
        for stage, _label, unit, mountpoint in synthetic_drives.DRIVES:
            directory = self.dest / f"{unit}.d"
            self.assertEqual((directory / "50-workspace-restore.conf").read_text(), gate)
            self.assertIn(f"ExecStop=-/usr/bin/fusermount3 -uz {mountpoint}\n",
                          (directory / "60-shutdown-unmount.conf").read_text())
        self.assertFalse((self.dest / "example-vm.service.d").exists())

    def test_without_a_host_file_nothing_is_installed_or_removed(self):
        stale = self.dest / "old.service.d"
        stale.mkdir()
        (stale / "50-workspace-restore.conf").write_text("keep")
        self.assertEqual(self.install(False), [])
        self.assertEqual((stale / "50-workspace-restore.conf").read_text(), "keep")

    def test_a_present_file_without_drives_removes_only_our_stale_dropins(self):
        self.install(None)
        other = self.dest / "example-drive-b.service.d/40-other.conf"
        other.write_text("[Service]\n")
        self.assertEqual(self.install(synthetic_drives.toml(synthetic_drives.DRIVES[:1])), ["example-drive-a.service"])
        self.assertFalse((self.dest / "example-drive-c.service.d").exists())
        self.assertTrue(other.exists())
        self.assertFalse((self.dest / "example-drive-b.service.d/50-workspace-restore.conf").exists())

    def test_an_invalid_file_installs_nothing_and_says_why(self):
        synthetic_drives.install(self, "[[drives]]\nstage = 1\n")
        with self.assertRaisesRegex(ValueError, "non-empty text stage"):
            host_config.install_dropins(self.ROOT / "systemd", self.dest)
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_removal_goes_by_file_name_without_any_host_file(self):
        self.install(None)
        synthetic_drives.install(self, False)
        self.assertEqual(sorted(host_config.remove_dropins(self.dest)),
                         ["example-drive-a.service", "example-drive-b.service", "example-drive-c.service"])
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_the_warm_up_timer_dropin_survives_installing_the_drive_dropins(self):
        timer = self.dest / "cloud-drives-warmup.timer.d/50-workspace-restore.conf"
        timer.parent.mkdir()
        timer.write_text("[Unit]\n")
        self.install(None)
        self.assertEqual(timer.read_text(), "[Unit]\n")
        self.install(synthetic_drives.toml(synthetic_drives.DRIVES[:1]))
        self.assertTrue(timer.exists())
        host_config.remove_dropins(self.dest)
        self.assertTrue(timer.exists())  # only service drop-ins are ours to remove

    def test_the_command_line_prints_mountpoints_and_units(self):
        synthetic_drives.install(self)
        from io import StringIO
        for command, first in (("mountpoints", "/home/example/Drives/drive-a"), ("units", "example-drive-a.service")):
            with patch("sys.stdout", new=StringIO()) as out:
                self.assertEqual(host_config.main([command]), 0)
            self.assertEqual(out.getvalue().splitlines()[0], first)
        synthetic_drives.install(self, False)
        with patch("sys.stdout", new=StringIO()) as out:
            self.assertEqual(host_config.main(["mountpoints"]), 0)
        self.assertEqual(out.getvalue(), "")
        synthetic_drives.install(self, "nope = 1")
        with patch("sys.stderr", new=StringIO()) as err:
            self.assertEqual(host_config.main(["mountpoints"]), 1)
        self.assertIn("unknown top-level key", err.getvalue())

    def test_the_template_names_no_real_drive(self):
        text = (self.ROOT / "systemd/drive-shutdown.conf.in").read_text()
        self.assertIn("@MOUNTPOINT@", text)
        self.assertNotIn("/home/", text)


class StorageRootTests(unittest.TestCase):
    def test_a_mountpoint_directly_in_the_home_directory_is_its_own_root(self):
        home = Path("/home/example")
        self.assertEqual(host_config.storage_root(home / "cloud", home), home / "cloud")
        self.assertEqual(host_config.storage_root(home / "Drives/cloud", home), home / "Drives")
        self.assertEqual(host_config.storage_root(Path("/mnt/cloud"), home), Path("/mnt/cloud"))
        self.assertEqual(host_config.storage_root(Path("/srv/data/cloud"), home), Path("/srv/data"))

    def test_ordinary_folders_are_not_storage_when_a_drive_sits_in_the_home_directory(self):
        synthetic_drives.install(self, synthetic_drives.toml((("drive-a", "A", "example-a.service", str(Path.home() / "cloud")),)))
        self.assertTrue(host_config.is_storage_path(Path.home() / "cloud/x"))
        self.assertFalse(host_config.is_storage_path(Path.home() / "Projects/demo"))
        self.assertFalse(host_config.is_storage_path(Path.home()))
        from workspace_state.codex_directories import _mount_for
        mounts = tuple(host_config.mounts())
        self.assertIsNone(_mount_for(Path.home() / "Projects/demo", Path.home(), mounts))
        self.assertEqual(_mount_for(Path.home() / "cloud/x", Path.home(), mounts), Path.home() / "cloud")


class WarmupScriptTests(unittest.TestCase):
    SCRIPT = Path(__file__).resolve().parents[1] / "bin/cloud-drives-warmup"

    def run_script(self, text, findmnt_ok=True):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            (bin_dir / "cloud-drives-warmup").write_text(self.SCRIPT.read_text())
            (bin_dir / "cloud-drives-warmup").chmod(0o755)
            # Stand-in for wsctl-python: the real module, this checkout's source.
            (bin_dir / "wsctl-python").write_text(
                f'#!/bin/sh\nPYTHONPATH={self.SCRIPT.parents[1] / "src"} exec python3 -m "$@"\n')
            (bin_dir / "wsctl-python").chmod(0o755)
            config = root / "config/workspace-state"
            config.mkdir(parents=True)
            if text is not False:
                (config / "host.toml").write_text(text)
            stubs = root / "stubs"
            stubs.mkdir()
            (stubs / "logger").write_text(f'#!/bin/sh\necho "$@" >> {root}/logger.log\n')
            (stubs / "logger").chmod(0o755)
            return subprocess.run(["/bin/sh", str(bin_dir / "cloud-drives-warmup")], capture_output=True, text=True,
                                  env={"PATH": f"{stubs}:/usr/bin:/bin", "XDG_CONFIG_HOME": str(root / "config"),
                                       "HOME": str(root), "WSCTL_TEST_RUN": "1", "XDG_RUNTIME_DIR": str(root)})

    def test_without_drives_it_succeeds(self):
        for text in (False, ""):
            with self.subTest(text=text):
                self.assertEqual(self.run_script(text).returncode, 0)

    def test_a_configured_drive_that_is_not_mounted_fails(self):
        result = self.run_script(synthetic_drives.toml(synthetic_drives.DRIVES[:1]))
        self.assertEqual(result.returncode, 1)

    def test_an_invalid_file_fails_with_its_message(self):
        result = self.run_script("nope = 1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown top-level key", result.stderr)

    def test_the_script_names_no_real_drive(self):
        self.assertNotIn("/home/", self.SCRIPT.read_text())


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
