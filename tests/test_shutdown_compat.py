import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from workspace_state import deployment as release


ROOT = Path(__file__).resolve().parents[1]


def load_script(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


compat = load_script("livepatch_stop_check", ROOT / "system-integration/wsctl-livepatch-stop-check")
installer = load_script("shutdown_compat_installer", ROOT / "scripts/install-shutdown-compat.py")
checker = load_script("shutdown_compat_checker", ROOT / "scripts/check-shutdown-compat.py")
TMUX_DROP_IN = "tmux-spawn-.scope.d/70-wsctl-terminal-hangup.conf"


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
    def test_legacy_install_preserves_exact_matching_release_owned_policy_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            managed = root / "data/workspace-state/desktop-releases/current/components/workspace-state/system-integration/user" / TMUX_DROP_IN
            managed.parent.mkdir(parents=True)
            managed.write_bytes((installer.SOURCE / "user" / TMUX_DROP_IN).read_bytes())
            target = root / "config/systemd/user" / TMUX_DROP_IN
            target.parent.mkdir(parents=True)
            target.symlink_to(managed)
            with mock.patch("builtins.print"):
                files = installer.user_files(root / "config", root / "data")
                installer.install_files(files, root / "backups")
            self.assertNotIn(target, [entry[1] for entry in files])
            self.assertEqual(os.readlink(target), str(managed))
            managed.write_text("a different release policy")
            with self.assertRaisesRegex(RuntimeError, "different release"):
                installer.user_files(root / "config", root / "data")
            self.assertEqual(managed.read_text(), "a different release policy")

    def test_legacy_install_still_refuses_arbitrary_scope_policy_link(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arbitrary = root / "arbitrary"
            arbitrary.write_bytes((installer.SOURCE / "user" / TMUX_DROP_IN).read_bytes())
            target = root / "config/systemd/user" / TMUX_DROP_IN
            target.parent.mkdir(parents=True)
            target.symlink_to(arbitrary)
            files = installer.user_files(root / "config", root / "data")
            with self.assertRaisesRegex(RuntimeError, "non-regular destination"):
                installer.install_files(files, root / "backups")
            self.assertFalse((root / "config/systemd/user/wsctl-gpg-ssh-environment.service").exists())

    def test_tmux_scope_policy_is_installed_without_starting_or_stopping_panes(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            os.environ, {"XDG_CONFIG_HOME": directory + "/config", "XDG_STATE_HOME": directory + "/state"},
        ), mock.patch.object(sys, "argv", ["installer", "--user"]), mock.patch.object(
            installer.os, "geteuid", return_value=1000,
        ), mock.patch.object(installer, "install_files") as install, mock.patch.object(
            installer.subprocess, "run", return_value=subprocess.CompletedProcess([], 3),
        ) as run, mock.patch("builtins.print"):
            self.assertEqual(installer.main(), 0)
            self.assertIn((installer.SOURCE / "user" / TMUX_DROP_IN,
                           Path(directory) / "config/systemd/user" / TMUX_DROP_IN, 0o644),
                          install.call_args.args[0])
        self.assertEqual(run.call_args_list, [
            mock.call(["/usr/bin/systemctl", "--user", "daemon-reload"], check=True, timeout=30),
            mock.call(["/usr/bin/systemctl", "--user", "is-active", "--quiet", "gpg-agent-ssh.socket"], check=False, timeout=5),
        ])

    def test_remmina_only_installs_both_fixed_files_and_only_reloads_definitions(self):
        with mock.patch.object(sys, "argv", ["installer", "--system", "--component", "remmina"]), mock.patch.object(
            installer.os, "geteuid", return_value=0,
        ), mock.patch.object(installer, "install_files") as install, mock.patch.object(
            installer.subprocess, "run",
        ) as run, mock.patch("builtins.print"):
            self.assertEqual(installer.main(), 0)
        expected = [(installer.SOURCE / source, Path(target), mode) for source, target, mode in installer.REMMINA_FILES]
        self.assertEqual(install.call_args.args[0], expected)
        self.assertEqual(len(expected), 2)
        self.assertTrue(all("remmina" in str(target) for _, target, _ in expected))
        run.assert_called_once_with(["/usr/bin/systemctl", "daemon-reload"], check=True, timeout=30)

    def test_remmina_filter_rejects_user_scope_and_unknown_component_before_writes(self):
        for arguments in (["--user", "--component", "remmina"], ["--system", "--component", "arbitrary"]):
            with self.subTest(arguments=arguments), mock.patch.object(
                sys, "argv", ["installer", *arguments],
            ), mock.patch.object(installer, "install_files") as install, mock.patch.object(
                installer.subprocess, "run",
            ) as run, mock.patch.object(sys, "stderr"):
                with self.assertRaises(SystemExit) as error:
                    installer.main()
                self.assertEqual(error.exception.code, 2)
                install.assert_not_called()
                run.assert_not_called()

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


class TmuxScopePolicyTests(unittest.TestCase):
    def test_prefix_policy_is_verified_without_querying_a_nonexistent_literal_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            drop_in = source / "user" / TMUX_DROP_IN
            drop_in.parent.mkdir(parents=True)
            drop_in.write_text((installer.SOURCE / "user" / TMUX_DROP_IN).read_text())
            with mock.patch.object(checker, "SOURCE", source), mock.patch.object(
                checker.subprocess, "check_output",
            ) as query, mock.patch.object(checker.subprocess, "run") as run, mock.patch("builtins.print"):
                self.assertEqual(checker.verify("user"), 0)
            query.assert_not_called()
            run.assert_not_called()

    def test_scope_verifier_rejects_timeout_changes_and_broader_policy(self):
        for name, content in [
            ("tmux-spawn-.scope.d", "[Scope]\nSendSIGHUP=no\n"),
            ("tmux-spawn-.scope.d", "[Scope]\nSendSIGHUP=yes\nTimeoutStopSec=30s\n"),
            ("tmux-spawn-.scope.d", "[Unit]\nBefore=shutdown.target\n[Scope]\nSendSIGHUP=yes\n"),
            ("scope.d", "[Scope]\nSendSIGHUP=yes\n"),
        ]:
            with self.subTest(name=name, content=content), tempfile.TemporaryDirectory() as directory:
                entry = Path(directory) / name
                entry.mkdir()
                (entry / Path(TMUX_DROP_IN).name).write_text(content)
                with self.assertRaises(RuntimeError):
                    checker.verify_scope_prefix(entry)

    @unittest.skipUnless(Path("/bin/bash").is_file(), "requires an isolated interactive bash")
    def test_interactive_shell_ignores_term_but_scope_hangup_exits_promptly(self):
        # No desktop tmux socket or systemd manager: this exact child is the only
        # signal recipient. The VM integration separately proves scope loading.
        checker.verify_scope_prefix(installer.SOURCE / "user" / Path(TMUX_DROP_IN).parent)
        master, slave = pty.openpty()
        process = subprocess.Popen(
            ["/bin/bash", "--noprofile", "--norc", "-i", "-c",
             "printf WSCTL_SHELL_READY; while true; do read -r; done"],
            stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
        )
        os.close(slave)
        try:
            output = b""
            deadline = time.monotonic() + 2
            while b"WSCTL_SHELL_READY" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 4096)
            self.assertIn(b"WSCTL_SHELL_READY", output)
            process.send_signal(signal.SIGTERM)
            with self.assertRaises(subprocess.TimeoutExpired):
                process.wait(timeout=0.1)
            process.send_signal(signal.SIGHUP)
            self.assertEqual(process.wait(timeout=2), -signal.SIGHUP)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            os.close(master)

    def test_real_release_mapping_seals_and_rolls_back_scope_policy_with_code(self):
        manifest = release.load_manifest(ROOT / "config/desktop-release.toml")
        component = next(item for item in manifest["components"] if item["name"] == "workspace-state")
        source_name = "system-integration/user/" + TMUX_DROP_IN
        binding = next(item for item in component["install"] if item["source"] == source_name)
        self.assertEqual(binding["target"], "{config}/systemd/user/" + TMUX_DROP_IN)
        self.assertIn("system-integration/user/**/*.conf", component["files"])
        chrome = next(item for item in component["probes"] if item["name"] == "chrome")
        self.assertIn("unclaimed_original_guard", chrome["capabilities"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locations = release.Locations(*(root / name for name in ("home", "data", "config", "state", "prefix", "runtime")))
            checkout = root / "checkout/workspace-state"
            source = checkout / source_name
            source.parent.mkdir(parents=True)
            first_policy = (ROOT / source_name).read_text()
            source.write_text(first_policy)
            binary = checkout / "bin/wsctl"
            binary.parent.mkdir()
            binary.write_text("first code\n")
            # Use the actual sealed glob, exact policy binding and Chrome probe
            # in a small fixture; unrelated desktop components are not installed.
            fixture = {"schema_version": 1, "components": [{
                "name": component["name"], "source": component["source"],
                "files": ["system-integration/user/**/*.conf", "bin/*"],
                "install": [binding, next(item for item in component["install"] if item["source"] == "bin/*")],
                "probes": [chrome],
            }]}
            with mock.patch.object(release, "load_manifest", return_value=fixture):
                first = release.stage(root / "unused.toml", checkout.parent, locations)
                self.assertIn("components/workspace-state/" + source_name, first["files"])
                release.install(first["revision"], locations)
                target = locations.config / "systemd/user" / TMUX_DROP_IN
                self.assertTrue(target.is_symlink())
                self.assertEqual(target.read_text(), first_policy)
                source.write_text(first_policy + "# A later sealed revision.\n")
                binary.write_text("second code\n")
                second = release.stage(root / "unused.toml", checkout.parent, locations)
                release.install(second["revision"], locations)
                self.assertIn("later sealed", target.read_text())
                self.assertEqual((locations.prefix / "bin/wsctl").read_text(), "second code\n")
                release.rollback(locations)
                self.assertEqual(target.read_text(), first_policy)
                self.assertEqual((locations.prefix / "bin/wsctl").read_text(), "first code\n")
                info = release.doctor(root / "unused.toml", checkout.parent, locations, runtime_reader=lambda _: {
                    "capabilities": [value for value in chrome["capabilities"] if value != "unclaimed_original_guard"],
                })
                probe = next(item for item in info["components"] if item["component"] == "workspace-state/chrome")
                self.assertEqual(probe["missing_capabilities"], ["unclaimed_original_guard"])


if __name__ == "__main__":
    unittest.main()
