import contextlib
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from workspace_state import alerts
from workspace_state.cli import main
from workspace_state.util import CommandError


class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(base / "config"),
            "XDG_STATE_HOME": str(base / "state"), "XDG_RUNTIME_DIR": str(base / "run")})
        self.env.start()
        self.addCleanup(self.env.stop)
        alerts.config_root().mkdir(parents=True)
        self.config = alerts.config_root() / "owned.toml"
        self.write_config()
        self.source = alerts.inventory()[0]

    def write_config(self, extra="", ownership="first-party"):
        # Temporary fixtures only; never use the real incident database/services.
        self.config.write_text('schema_version = 1\n[[sources]]\nid = "own-bot"\n'
            'label = "Own bot"\nkind = "systemd"\nunit = "own-bot.service"\n'
            f'ownership = "{ownership}"\nsource_ref = "~/Projects/own-bot"\n' + extra)
        self.config.chmod(0o600)

    @contextlib.contextmanager
    def db(self):
        with alerts.database() as db:
            alerts.register(db, [self.source])
            yield db

    def row(self, db):
        return db.execute("SELECT * FROM incidents").fetchone()

    def reply(self, stdout, code=0):
        return subprocess.CompletedProcess([], code, stdout, "")

    def state(self, **changes):
        data = dict(LoadState="loaded", ActiveState="active", Result="success", UnitFileState="enabled",
                    InvocationID="a", ExecMainStatus="0", NRestarts="0", ConditionResult="yes")
        data.update(changes)
        return self.reply("\n".join(f"{key}={value}" for key, value in data.items()))

    def probe(self, source=None, state=None, journal=None, since=None):
        with patch.object(alerts, "command", side_effect=[state or self.state(), journal or self.reply(""), journal or self.reply("")]) as run:
            outcome = alerts.probe(source or self.source, since)
        return outcome, run

    def test_inventory_rejects_third_party_and_arbitrary_commands(self):
        self.write_config(ownership="third-party")
        with self.assertRaises(CommandError):
            alerts.inventory()
        self.write_config('command = "systemctl stop own-bot"\n')
        with self.assertRaises(CommandError):
            alerts.inventory()

    def test_inventory_rejects_wildcards_host_injection_and_duplicates(self):
        for extra in ['host = "--evil"\n', 'host = "server; reboot"\n']:
            self.write_config(extra)
            with self.assertRaises(CommandError):
                alerts.inventory()
        self.write_config()
        self.config.write_text(self.config.read_text().replace('own-bot.service', '*.service'))
        with self.assertRaises(CommandError):
            alerts.inventory()
        self.write_config()
        other = self.config.with_name("duplicate.toml")
        other.write_text(self.config.read_text())
        with self.assertRaises(CommandError):
            alerts.inventory()

    def test_inventory_requires_private_owned_regular_files(self):
        self.config.chmod(0o666)
        with self.assertRaises(CommandError):
            alerts.inventory()
        self.config.chmod(0o600)
        self.config.with_name("link.toml").symlink_to(self.config)
        with self.assertRaises(CommandError):
            alerts.inventory()

    def test_durable_dedup_ack_and_recurrence(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "auth-required", "Login required", token="one")
            alerts.record(db, "own-bot", "auth-required", "Login required", token="one")
            self.assertEqual(self.row(db)["occurrences"], 1)
            db.execute("UPDATE incidents SET acknowledged=1")
            self.assertEqual(alerts.publish(db, [self.source])["active_count"], 1)
            alerts.resolve(db, "own-bot", "auth-required")
        with self.db() as db:
            alerts.record(db, "own-bot", "auth-required", "Replayed log", token="one")
            self.assertEqual(self.row(db)["active"], 0)
            alerts.record(db, "own-bot", "auth-required", "New failure", token="two")
            self.assertEqual(self.row(db)["acknowledged"], 0)
            self.assertEqual(self.row(db)["episode"], 2)

    def test_incident_severity_is_stored_and_legacy_schema_migrates(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "auth-required", "Login required")
            self.assertEqual(self.row(db)["severity"], "blocker")
        path = alerts.root() / "incidents.sqlite3"
        with sqlite3.connect(path) as old:
            old.execute("CREATE TABLE IF NOT EXISTS legacy(x INTEGER)")
        with self.db() as db:
            self.assertIn("severity", {row["name"] for row in db.execute("PRAGMA table_info(incidents)")})

    def test_unknown_legacy_code_migrates_to_warning(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "old-code", "Old report")
            self.assertEqual(self.row(db)["severity"], "warning")

    def test_condition_can_recur_with_same_state_token_after_recovery(self):
        with self.db() as db:
            for _ in range(2):
                alerts.record(db, "own-bot", "plugin-failed", "Failure", token="same-boot-error", condition=True)
            self.assertEqual(self.row(db)["occurrences"], 1)
            alerts.resolve(db, "own-bot", "plugin-failed")
            alerts.record(db, "own-bot", "plugin-failed", "Failure", token="same-boot-error", condition=True)
            self.assertEqual(self.row(db)["episode"], 2)

    def test_resolved_unread_survives_next_session(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "auth-required", "Login required")
            alerts.resolve(db, "own-bot", "auth-required")
        with self.db() as db, patch.object(alerts, "boot_id", return_value="next-boot"):
            payload = alerts.publish(db, [self.source])
            self.assertEqual(payload["unread_count"], 1)
            self.assertEqual(payload["active_count"], 0)
            self.assertEqual(payload["boot_id"], "next-boot")
            self.assertEqual(alerts.snapshot_path().stat().st_mode & 0o777, 0o600)
            self.assertEqual((alerts.root() / "incidents.sqlite3").stat().st_mode & 0o777, 0o600)

    def test_opted_out_sources_never_appear(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "failure", "Failure")
            self.assertEqual(alerts.publish(db, [])["incidents"], [])

    def test_secrets_are_redacted_and_journal_messages_never_persist(self):
        entry = dict(MESSAGE="invalid_grant SECRET_VALUE", PRIORITY="6", __CURSOR="cursor")
        outcome, run = self.probe(journal=self.reply(json.dumps(entry)))
        self.assertEqual(outcome["issues"][0][0], "auth-required")
        self.assertNotIn("SECRET_VALUE", str(outcome))
        self.assertEqual(outcome["health"], "blocked")
        self.assertIn("--unit=own-bot.service", run.call_args_list[1].args[1])
        with self.db() as db:
            alerts.record(db, "own-bot", "failure", "password=PASSWORD_VALUE", "Authorization: Bearer SECRET_VALUE")
            data = json.dumps(alerts.publish(db, [self.source]))
            self.assertNotIn("PASSWORD_VALUE", data)
            self.assertNotIn("SECRET_VALUE", data)

    def test_unknown_host_never_resolves_previous_incident(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "service-failed", "Failure")
            db.execute("UPDATE sources SET since=123")
        with patch.object(alerts, "command", side_effect=subprocess.TimeoutExpired("ssh", 8)):
            result = alerts.scan()
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["sources"][0]["coverage"], "unknown")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT since FROM sources").fetchone()[0], 123)

    def test_disabled_daemon_is_not_a_failure(self):
        outcome, _ = self.probe(state=self.state(ActiveState="inactive", UnitFileState="disabled"))
        self.assertEqual(outcome["health"], "disabled")
        self.assertEqual(outcome["issues"], [])

    def test_oneshot_success_is_not_a_missing_daemon(self):
        outcome, _ = self.probe(source=self.source | {"expected": "on-demand"}, state=self.state(ActiveState="inactive"))
        self.assertEqual(outcome["health"], "healthy")
        self.assertEqual(outcome["issues"], [])

    def test_never_run_oneshot_does_not_resolve_an_older_failure(self):
        outcome, _ = self.probe(source=self.source | {"expected": "on-demand"}, state=self.state(ActiveState="inactive", InvocationID=""))
        self.assertEqual(outcome["health"], "unknown")
        self.assertEqual(outcome["resolved"], [])

    def test_missing_service_is_not_reported_healthy(self):
        outcome, _ = self.probe(state=self.reply("LoadState=not-found", 1))
        self.assertEqual(outcome["health"], "not-installed")

    def test_journal_permission_error_preserves_cursor(self):
        outcome, _ = self.probe(journal=self.reply("", 1), since=123)
        self.assertEqual(outcome["health"], "unknown")
        self.assertEqual(outcome["since"], 123)

    def test_bounded_history_is_explicit(self):
        outcome, _ = self.probe(journal=self.reply("\n".join(['{"MESSAGE":"ok"}'] * alerts.JOURNAL_LIMIT)))
        self.assertEqual(outcome["coverage"], "bounded-history")

    def test_journal_filters_before_limit_and_no_match_is_not_failure(self):
        with patch.object(alerts, "command", side_effect=[self.state(), self.reply(""), self.reply("", 1)]) as run:
            outcome = alerts.probe(self.source, None)
        self.assertEqual(outcome["health"], "healthy")
        self.assertIn("--priority=0..2", run.call_args_list[1].args[1])
        self.assertTrue(any(arg.startswith("--grep=") for arg in run.call_args_list[2].args[1]))

    def test_owned_plugin_error_ledger_is_scoped_and_not_stored_raw(self):
        try:
            from gi.repository import GLib  # noqa: F401
        except ImportError:
            self.skipTest("GNOME GVariant parser is not installed")
        with patch.object(alerts, "command", side_effect=[self.reply("State: ACTIVE"), self.reply("(['Sensitive plugin stack SECRET_VALUE'],)")]) as run:
            outcome = alerts.probe(self.source | {"kind": "gnome-extension", "uuid": "own@sagecat.local"}, None)
        self.assertEqual(outcome["health"], "healthy")
        self.assertNotIn("SECRET_VALUE", str(outcome))
        self.assertEqual(run.call_args_list[1].args[1][-1], "own@sagecat.local")

    def test_only_critical_patterns_not_every_error(self):
        self.assertIsNone(alerts.classify({"MESSAGE": "temporary error, retry succeeded", "PRIORITY": "6"}))
        self.assertIsNone(alerts.classify({"MESSAGE": "rejected unauthorized user command", "PRIORITY": "6"}))
        self.assertEqual(alerts.classify({"MESSAGE": "HTTP error 401 Unauthorized", "PRIORITY": "6"}), "auth-required")
        self.assertEqual(alerts.classify({"MESSAGE": "failure", "PRIORITY": "2"}), "critical-log")
        self.assertIsNone(alerts.classify({"MESSAGE": "Traceback (most recent call last):"}))

    def test_config_error_is_visible_and_preserves_incidents(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "failure", "Failure", severity="critical")
        self.config.chmod(0o666)
        with self.assertRaises(CommandError):
            alerts.scan()
        result = json.loads(alerts.snapshot_path().read_text())
        self.assertTrue(result["scan_error"])
        self.assertEqual(result["active_count"], 1)

    def test_runtime_transport_excludes_healthy_noncritical_and_resolved(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "minor", "Ordinary warning", severity="warning")
            alerts.record(db, "own-bot", "fixed", "Fixed failure", severity="critical")
            alerts.resolve(db, "own-bot", "fixed")
            cli = alerts.publish(db, [self.source])
            self.assertEqual(len(cli["sources"]), 1)
            self.assertEqual(len(cli["incidents"]), 2)
            self.assertEqual(json.loads(alerts.snapshot_path().read_text())["sources"], [])
            self.assertEqual(json.loads(alerts.snapshot_path().read_text())["incidents"], [])
            alerts.record(db, "own-bot", "blocked", "Blocked", severity="blocker")
            alerts.publish(db, [self.source])
            visible = json.loads(alerts.snapshot_path().read_text())
            self.assertEqual([item["code"] for item in visible["incidents"]], ["blocked"])

    def test_explicit_critical_journal_priority_is_never_downgraded(self):
        code = alerts.classify({"MESSAGE": "uncaught exception", "PRIORITY": "2"})
        self.assertEqual(code, "critical-log")
        self.assertEqual(alerts.severity_for(code), "critical")

    def test_severity_escalation_of_same_event_becomes_unread(self):
        with self.db() as db:
            alerts.record(db, "own-bot", "failure", "Failure", token="event", severity="error")
            db.execute("UPDATE incidents SET acknowledged=1")
            alerts.record(db, "own-bot", "failure", "Failure", token="event", severity="critical")
            self.assertEqual(self.row(db)["severity"], "critical")
            self.assertEqual(self.row(db)["acknowledged"], 0)

    def test_cli_report_ack_resolve_with_real_private_database(self):
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["alerts", "report", "own-bot", "auth-required", "Reauthenticate", "--event-id", "event1"]), 0)
            self.assertEqual(main(["alerts", "ack", "own-bot", "auth-required"]), 0)
            self.assertEqual(main(["alerts", "report", "third-party", "failure", "Denied"]), 2)
        result = json.loads(alerts.snapshot_path().read_text())
        self.assertEqual(result["active_count"], 1)
        self.assertEqual(result["unread_count"], 0)

    def test_ssh_uses_fixed_read_only_argv_and_strict_host_keys(self):
        with patch.object(alerts.subprocess, "run", return_value=self.reply("")) as run:
            alerts.command(self.source | {"host": "example-server"}, ["/usr/bin/systemctl", "--user", "show", "own-bot.service"])
        cmd = run.call_args.args[0]
        self.assertIn("-oStrictHostKeyChecking=yes", cmd)
        self.assertEqual(cmd[-1], "env LC_ALL=C /usr/bin/systemctl --user show own-bot.service")
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_browser_companion_only_pings_without_launching_or_reading_tabs(self):
        from workspace_state import browser
        good = dict(protocol_version=browser.BROWSER_PROTOCOL_VERSION, capabilities=list(browser.BROWSER_REQUIRED_CAPABILITIES))
        with patch.object(browser, "_host_paths", return_value=[Path("fake.sock")]), patch.object(browser, "_request_path", return_value=good) as call:
            outcome = alerts.probe(self.source | {"kind": "browser-companion"}, None)
        self.assertEqual(outcome["health"], "healthy")
        call.assert_called_once_with(Path("fake.sock"), "ping", timeout=1)

    def test_builtin_registry_only_contains_explicit_owned_code(self):
        self.config.write_text((Path(__file__).parents[1] / "config/owned-systems.toml").read_text())
        sources = alerts.inventory()
        self.assertEqual(len(sources), 10)
        self.assertTrue(all(source["host"] == "local" for source in sources))
        self.assertTrue(all(source["ownership"] == "first-party" for source in sources))
        self.assertFalse(any(source.get("unit", "").startswith(("rclone-", "docker", "sshfs", "snap.", "sagecat-nextcloud.service")) for source in sources))

    def test_collector_cannot_delay_workspace_target_or_drive_startup(self):
        unit = (Path(__file__).parents[1] / "systemd/wsctl-alerts.service").read_text()
        self.assertIn("DefaultDependencies=no", unit)
        self.assertIn("After=wsctl-workspace-restored.target", unit)
        self.assertIn("Before=shutdown.target", unit)
        self.assertNotIn("Before=wsctl-workspace-restored.target", unit)
        self.assertNotIn("ExecStop=", unit)

    def test_scanning_metadata_survives_ack_snapshot(self):
        with self.db() as db:
            alerts.publish(db, [self.source], scanning=True)
            self.assertTrue(alerts.publish(db, [self.source])["scanning"])

    def test_global_scan_budget_marks_unchecked_sources_unknown(self):
        original = alerts.safe_probe
        def slow(source, since):
            time.sleep(0.04)
            return original(source | {"kind": "events"}, since)
        with patch.object(alerts, "safe_probe", side_effect=slow), patch.object(alerts, "SCAN_BUDGET", 0.001):
            result = alerts.scan()
        self.assertFalse(result["scanning"])
        self.assertEqual(result["sources"][0]["coverage"], "scan-timeout")


if __name__ == "__main__":
    unittest.main()
