import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


compat = load_script("livepatch_stop_check", ROOT / "system-integration/wsctl-livepatch-stop-check")
installer = load_script("shutdown_compat_installer", ROOT / "scripts/install-shutdown-compat.py")


class LivepatchShutdownResultTests(unittest.TestCase):
    invocation = "a" * 32

    def record(self):
        return {
            "_SYSTEMD_UNIT": compat.UNIT,
            "_SYSTEMD_INVOCATION_ID": self.invocation,
            "SYSLOG_IDENTIFIER": compat.IDENTIFIER,
            "MESSAGE": compat.COMPLETION,
            "__MONOTONIC_TIMESTAMP": "100000000",
        }

    def run_check(self, *, stopping=False, completion=False, code="exited", status="1", invocation=None):
        env = {"EXIT_CODE": code, "EXIT_STATUS": status, "INVOCATION_ID": invocation or self.invocation}
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(compat, "system_is_stopping", return_value=stopping), mock.patch.object(compat, "has_completion", return_value=completion), mock.patch.object(compat.time, "sleep"), mock.patch("builtins.print"):
            return compat.main()

    def test_proven_shutdown_is_clean(self):
        self.assertEqual(self.run_check(stopping=True, completion=True), 0)

    def test_runtime_auth_or_crash_exit_one_stays_failed(self):
        self.assertEqual(self.run_check(completion=True), 2)

    def test_missing_completion_during_shutdown_stays_failed(self):
        self.assertEqual(self.run_check(stopping=True), 2)

    def test_invalid_invocation_stays_failed(self):
        self.assertEqual(self.run_check(stopping=True, completion=True, invocation="invalid"), 2)

    def test_other_exit_statuses_do_not_override_systemd_result(self):
        for code, status in [("exited", "0"), ("exited", "2"), ("killed", "SEGV"), ("dumped", "ABRT")]:
            with self.subTest(code=code, status=status):
                self.assertEqual(self.run_check(code=code, status=status), 0)

    def test_only_exact_current_invocation_completion_is_accepted(self):
        self.assertTrue(compat.completion_record_matches(self.record(), self.invocation, 101))
        for key, value in [
            ("_SYSTEMD_UNIT", "another.service"), ("_SYSTEMD_INVOCATION_ID", "b" * 32),
            ("SYSLOG_IDENTIFIER", "untrusted"), ("MESSAGE", "not daemon shutting down"),
            ("__MONOTONIC_TIMESTAMP", "0"), ("__MONOTONIC_TIMESTAMP", "999000000"),
            ("__MONOTONIC_TIMESTAMP", None),
        ]:
            with self.subTest(key=key, value=value):
                record = self.record()
                record[key] = value
                self.assertFalse(compat.completion_record_matches(record, self.invocation, 101))

    def test_system_state_fail_closed(self):
        for stdout, code in [("running\n", 0), ("degraded\n", 1), ("unknown\n", 1), ("stopping\n", 0), ("stopping\n", 1)]:
            with self.subTest(stdout=stdout, code=code), mock.patch.object(compat.subprocess, "run", return_value=subprocess.CompletedProcess([], code, stdout, "")):
                self.assertEqual(compat.system_is_stopping(), (stdout, code) == ("stopping\n", 1))
        with mock.patch.object(compat.subprocess, "run", side_effect=subprocess.TimeoutExpired("systemctl", 2)):
            self.assertFalse(compat.system_is_stopping())

    def test_journal_matching_and_query_are_bounded(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(self.record()) + "\n", "")
        with mock.patch.object(compat.subprocess, "run", return_value=result) as run, mock.patch.object(compat.time, "monotonic", return_value=101):
            self.assertTrue(compat.has_completion(self.invocation))
        self.assertIn(f"_SYSTEMD_INVOCATION_ID={self.invocation}", run.call_args.args[0])
        self.assertIn("--boot=0", run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_journal_errors_and_malformed_records_fail_closed(self):
        for output, code in [("invalid", 0), ("[]", 0), ("", 0), (json.dumps(self.record()), 1)]:
            with self.subTest(output=output, code=code), mock.patch.object(compat.subprocess, "run", return_value=subprocess.CompletedProcess([], code, output, "")):
                self.assertFalse(compat.has_completion(self.invocation))
        with mock.patch.object(compat.subprocess, "run", side_effect=OSError("unavailable")):
            self.assertFalse(compat.has_completion(self.invocation))


class ShutdownCompatInstallTests(unittest.TestCase):
    def test_install_sources_exist_and_do_not_disable_services(self):
        sources = [installer.SOURCE / source for source, _, _ in installer.SYSTEM_FILES]
        sources += [installer.SOURCE / "user" / name for name in installer.USER_FILES]
        for source in sources:
            self.assertTrue(source.is_file(), source)
            content = source.read_text()
            self.assertNotIn("systemctl mask", content)
            self.assertNotIn("SuccessExitStatus=1 2", content)

    def test_backup_and_idempotent_install(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.write_text("new")
            target.write_text("old")
            with mock.patch("builtins.print"):
                installer.install_files([(source, target, 0o644)], root / "backups")
                before = target.stat().st_ino
                installer.install_files([(source, target, 0o644)], root / "backups")
            self.assertEqual(target.read_text(), "new")
            self.assertEqual(target.stat().st_ino, before)
            self.assertEqual((root / "backups" / target.relative_to(target.anchor)).read_text(), "old")

    def test_refuses_symlink_destination_before_any_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.write_text("data")
            target = root / "link"
            target.symlink_to(source)
            with self.assertRaises(RuntimeError):
                installer.install_files([(source, root / "new", 0o644), (source, target, 0o644)], root / "backups")
            self.assertFalse((root / "new").exists())

    def test_livepatch_allowance_always_has_mandatory_validator(self):
        content = (installer.SOURCE / "system/snap.canonical-livepatch.canonical-livepatchd.service.d/70-wsctl-shutdown-result.conf").read_text()
        self.assertIn("SuccessExitStatus=1\n", content)
        self.assertIn("ExecStopPost=/bin/sh -c '/usr/local/libexec/wsctl-livepatch-stop-check || exit 2'\n", content)
        self.assertNotIn("ExecStopPost=-", content)

    def test_gpg_cleanup_is_ordered_bounded_and_rearmed_by_socket(self):
        helper = (installer.SOURCE / "user/wsctl-gpg-ssh-environment.service").read_text()
        socket = (installer.SOURCE / "user/gpg-agent-ssh.socket.d/70-wsctl-environment-cleanup.conf").read_text()
        for setting in ["After=dbus.service gpg-agent-ssh.socket", "PartOf=gpg-agent-ssh.socket", "TimeoutStopSec=2s", "DefaultDependencies=no"]:
            self.assertIn(setting, helper)
        self.assertIn("Wants=wsctl-gpg-ssh-environment.service", socket)
        self.assertNotIn("ExecStartPre=", socket)
        self.assertNotIn("ExecStartPost=", socket)
        self.assertEqual(installer.USER_FILES[0], "wsctl-gpg-ssh-environment.service")


if __name__ == "__main__":
    unittest.main()
