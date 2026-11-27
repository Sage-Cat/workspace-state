from argparse import Namespace
from copy import deepcopy
import stat
import time
from unittest.mock import patch

from workspace_state import cli, operations, pending, storage
from workspace_state.provider_results import (
    EvidenceState as State, PhaseEvidence, ProviderCount, ProviderItemResult,
)
from workspace_state.startup import StageMarker, write_stage_marker

from test_independent_save import Base, LOGIN, recipe


def location(workspace, x):
    rect = {"x": x, "y": 4, "width": 900, "height": 720}
    return {"workspace": x // 100, "workspace_name": workspace, "monitor": 0,
            "monitor_identity": {"connector": "DP-1"}, "state": "normal",
            "geometry": dict(rect), "geometry_relative": dict(rect)}


def project(name, placement):
    return {"kind": "folder", "workspace_file": None,
            "folders": [{"name": name, "uri": f"file:///synthetic/{name}"}],
            "profile": {"id": "default", "name": "Default"},
            "user_data_dir": "/synthetic/code", "editor_uris": [], "placement": placement}


def app(*windows):
    return {"running": bool(windows), "mode": "windowed" if windows else "stopped",
            "windows": list(windows)}


def evidence(category, item, state, request=None):
    verified = PhaseEvidence(State.VERIFIED)
    return ProviderItemResult(category, item, verified, verified,
                              PhaseEvidence(state, request_id=request))


class PlacementGuardTests(Base):
    def setUp(self):
        super().setUp()
        operations.bind(None)
        self.addCleanup(operations.bind, None)
        previous = recipe()
        previous["vscode"]["windows"] = [project("one", location("Writing", 100)),
                                          project("two", location("Review", 200))]
        previous["social_apps"].update(steam=app(location("Play", 300), location("Play", 305)),
                                       slack=app(location("Chat", 400)))
        self.previous = self.seed(previous)
        self.live = deepcopy(self.previous)
        self.live["vscode"]["windows"][0].update(placement=location("Wrong", 0),
                                                editor_uris=["file:///synthetic/one/new.py"])
        self.live["vscode"]["windows"][1]["placement"] = location("Review", 240)
        self.live["social_apps"].update(steam=app(location("Advert", 20)),
                                        slack=app(location("Chat", 450)))
        self.patch(cli, "_boot_id", return_value=LOGIN["boot_id"])
        self.generation = self.patch(cli, "_login_generation_file", return_value=LOGIN["login_generation"])
        self.key = self.patch(cli, "_login_key_file", return_value=LOGIN["login_key"])
        self.patch(cli, "_graphical_session_ending", return_value=None)
        self.patch(cli, "update_stage")
        self.patch(cli, "publish_provider_results")
        self.capture = self.patch(cli, "_capture_all", side_effect=lambda **_: deepcopy(self.live))
        self.context = operations.OperationContext(LOGIN["boot_id"], LOGIN["login_generation"],
                                                  operation_id="restore", mode="startup",
                                                  attempt=1, deadline=time.monotonic() + 60)
        self.directory = cli._seen_directory(LOGIN)

    def publish(self, category, *results, **kwargs):
        with operations.publisher(self.context):
            return cli._publish_category_outcome(category, self.previous, count=ProviderCount(1, results),
                                                 **kwargs)

    def save(self, *, final=False, manual=False):
        return cli.cmd_save(Namespace(auto=not manual, final=final, allow_partial=False,
                                      verify_idle_codex=False)), storage.load()

    def test_auto_and_final_preserve_only_unverified_windows_and_apps(self):
        self.assertEqual(self.publish("vscode", evidence("vscode", "1", State.FAILED),
                                     evidence("vscode", "2", State.VERIFIED)), "degraded")
        self.assertEqual(self.publish("social-apps", evidence("social-apps", "steam:1", State.WAITING, "request"),
                                     evidence("social-apps", "steam:2", State.VERIFIED),
                                     evidence("social-apps", "slack:1", State.VERIFIED)), "waiting")
        for final in (False, True):
            with self.subTest(final=final):
                code, saved = self.save(final=final)
                self.assertEqual(code, 0)
                old = self.previous["vscode"]["windows"][0]
                observed = self.live["vscode"]["windows"][0]
                windows = saved["vscode"]["windows"]
                self.assertEqual(windows[0], {**observed, "placement": old["placement"]})
                self.assertEqual(windows[1], self.live["vscode"]["windows"][1])
                self.assertEqual(saved["social_apps"]["steam"], self.previous["social_apps"]["steam"])
                self.assertEqual(saved["social_apps"]["slack"], self.live["social_apps"]["slack"])
                records = saved["category_provenance"]
                self.assertEqual(records["social-apps"]["retained_placements"], ["social-apps:steam"])
                self.assertEqual(records["vscode"]["retained_placements"], [pending.vscode_key(old)])
                self.assertEqual(records["vscode"]["source"], "final-save" if final else "auto-save")
                self.assertNotIn("adoption", records["vscode"])
        guard = self.directory / "vscode-placement-guards.json"
        self.assertEqual(stat.S_IMODE(guard.stat().st_mode), 0o600)

    def test_absent_but_seen_targets_stay_pending(self):
        self.publish("vscode", evidence("vscode", "1", State.FAILED))
        self.publish("social-apps", evidence("social-apps", "steam:1", State.FAILED))
        del self.live["vscode"]["windows"][0]
        self.live["social_apps"]["steam"] = app()
        code, saved = self.save()
        self.assertEqual(code, 0)
        old = self.previous["vscode"]["windows"][0]
        self.assertIn(old, saved["vscode"]["windows"])
        self.assertEqual(saved["social_apps"]["steam"], self.previous["social_apps"]["steam"])
        self.assertEqual(set(saved["pending"]), {pending.vscode_key(old), "social-apps:steam"})

    def test_absent_same_project_preserves_the_unverified_destinations(self):
        first, second = self.previous["vscode"]["windows"]
        second = {**deepcopy(first), "placement": second["placement"]}
        third = {**deepcopy(first), "placement": location("IRC", 337)}
        self.previous["vscode"]["windows"] = [first, second, third]
        self.previous = self.seed(self.previous)
        self.live["vscode"]["windows"] = []
        self.publish("vscode", evidence("vscode", "1", State.VERIFIED),
                     evidence("vscode", "2", State.FAILED),
                     evidence("vscode", "3", State.WAITING, "request"))
        key = pending.vscode_key(first)
        for final in (False, True):
            with self.subTest(final=final):
                with patch.object(pending, "record_seen", wraps=pending.record_seen) as seen:
                    code, saved = self.save(final=final)
                self.assertEqual(code, 0)
                self.assertNotIn(key, seen.call_args.args[1])
                self.assertEqual(saved["vscode"]["windows"], [second, third])
                self.assertEqual(set(saved["pending"]), {key})
                self.assertEqual(saved["category_provenance"]["vscode"]["retained_placements"], [key])

    def test_capacity_limit_retains_vscode_and_allows_the_other_captures(self):
        from workspace_state.vscode import MAX_WINDOWS
        self.publish("vscode", evidence("vscode", "1", State.FAILED))
        self.live["vscode"]["windows"] = [project(f"fresh-{index}", location("Review", 201 + index))
                                         for index in range(MAX_WINDOWS)]
        code, saved = self.save()
        self.assertEqual(code, 0)
        self.assertEqual(saved["vscode"], self.previous["vscode"])
        self.assertEqual(saved["category_provenance"]["vscode"]["state"], "retained")
        self.assertEqual(saved["social_apps"], self.live["social_apps"])

    def test_async_release_requires_the_same_request_and_operation(self):
        expected = evidence("vscode", "1", State.VERIFIED, "compositor-request")
        self.publish("vscode", evidence("vscode", "1", State.WAITING, "compositor-request"))
        path = self.directory / "vscode.done"
        for item, context in (
            (evidence("vscode", "1", State.VERIFIED, "other-request"), self.context.to_dict()),
            (expected, {**self.context.to_dict(), "operation_id": "another-restore"}),
            (expected, self.context.to_dict()),
        ):
            write_stage_marker(path, StageMarker("vscode", "ready", self.previous["created_at"],
                                                operation_context=context, provider_results=(item.to_dict(),)))
            _, saved = self.save()
            observed = saved["vscode"]["windows"][0]["placement"]
            if item == expected and context == self.context.to_dict():
                self.assertEqual(observed, self.live["vscode"]["windows"][0]["placement"])
                self.assertNotIn("retained_placements", saved["category_provenance"]["vscode"])
            else:
                self.assertEqual(observed, self.previous["vscode"]["windows"][0]["placement"])

    def test_async_verification_during_capture_does_not_accept_an_earlier_sample(self):
        request = "compositor-request"
        self.publish("vscode", evidence("vscode", "1", State.WAITING, request))

        def capture(**_):
            sampled = deepcopy(self.live)
            item = evidence("vscode", "1", State.VERIFIED, request)
            write_stage_marker(self.directory / "vscode.done", StageMarker(
                "vscode", "ready", self.previous["created_at"],
                operation_context=self.context.to_dict(), provider_results=(item.to_dict(),)))
            return sampled

        self.capture.side_effect = capture
        self.assertEqual(self.save()[1]["vscode"]["windows"][0]["placement"],
                         self.previous["vscode"]["windows"][0]["placement"])
        self.capture.side_effect = lambda **_: deepcopy(self.live)
        self.assertEqual(self.save()[1]["vscode"], self.live["vscode"])

    def test_successful_manual_save_releases_only_accepted_categories(self):
        self.publish("vscode", evidence("vscode", "1", State.FAILED))
        self.publish("social-apps", evidence("social-apps", "steam:1", State.FAILED))
        steam_live = self.live["social_apps"].pop("steam")
        self.live["capture_errors"] = {"social_apps": ["Capture failed"]}
        self.live["vscode"] = deepcopy(self.previous["vscode"])
        code, saved = self.save(manual=True)
        self.assertEqual(code, 3)
        self.assertIn("adoption", saved["category_provenance"]["vscode"])
        del self.live["capture_errors"]
        self.live["social_apps"]["steam"] = steam_live
        for x in (250, 260):
            self.live["vscode"]["windows"][0]["placement"] = location("New", x)
            _, saved = self.save()
            self.assertEqual(saved["social_apps"]["steam"], self.previous["social_apps"]["steam"])
            self.assertEqual(saved["vscode"], self.live["vscode"])
            self.assertNotIn("adoption", saved["category_provenance"]["vscode"])

    def test_scoped_retry_does_not_clear_another_window_or_an_unplaced_retry(self):
        self.publish("vscode", evidence("vscode", "1", State.FAILED), evidence("vscode", "2", State.FAILED))
        verified = {category: 0 for category in cli.CATEGORIES}
        verified["vscode"] = ProviderCount(1, [evidence("vscode", "1", State.VERIFIED)])
        verified["codex_total"] = 0
        args = Namespace(category="vscode", workspace="Review", no_place=True, dry_run=False)
        with patch.object(cli, "_restore", return_value=verified):
            self.assertEqual(cli.cmd_restore(args), 0)
            self.assertEqual(self.save()[1]["vscode"], self.previous["vscode"] | {
                "windows": [{**self.live["vscode"]["windows"][0],
                             "placement": self.previous["vscode"]["windows"][0]["placement"]},
                            self.previous["vscode"]["windows"][1]],
            })
            args.no_place = False
            self.assertEqual(cli.cmd_restore(args), 0)
        _, saved = self.save()
        self.assertEqual(saved["vscode"]["windows"][0]["placement"],
                         self.previous["vscode"]["windows"][0]["placement"])
        self.assertEqual(saved["vscode"]["windows"][1], self.live["vscode"]["windows"][1])

    def test_a_new_login_does_not_inherit_the_guard(self):
        self.publish("vscode", evidence("vscode", "1", State.FAILED))
        self.generation.return_value = "b" * 16
        self.key.return_value = "e" * 16
        self.assertEqual(self.save()[1]["vscode"], self.live["vscode"])
