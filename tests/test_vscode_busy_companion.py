import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_state import vscode
from workspace_state.provider_results import EvidenceState, PlacementPending, ProviderRestoreError
from workspace_state.util import CommandError

PLACEMENT = {
    "workspace": 0, "workspace_name": "Work", "monitor": 0,
    "monitor_intent": {"connector": "DP-1"},
    "geometry": {"x": 0, "y": 0, "width": 800, "height": 600},
    "geometry_relative": {"x": 0, "y": 0, "width": 800, "height": 600},
    "state": "normal",
}
BUSY = vscode.CompanionNotReady("VS Code companion is busy or still starting")


def project(name):
    return {
        "kind": "folder", "workspace_file": None,
        "folders": [{"uri": f"file:///tmp/{name}", "name": name}],
        "editor_uris": [], "dirty_count": 0,
        "profile": {"id": "default", "name": "Default"},
        "user_data_dir": "/tmp/code-data", "remote_name": None,
        "window_key": name, "hot_exit": "on", "placement": dict(PLACEMENT),
    }


def live_window(item, name, wid):
    return {**item, "instance": name, "endpoint": Path(f"/tmp/{name}.sock"),
            "shell": {"id": wid, "workspace": 1, "monitor": 0, "state": "normal"}}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += max(seconds, 0.01)


class BusyCompanionTests(unittest.TestCase):
    def run_restore(self, items, request, place=None, **kwargs):
        clock = self.clock = _Clock()
        events = []
        windows = [live_window(item, item["window_key"], index + 1) for index, item in enumerate(items)]

        def fake_request(endpoint, method, *args, **options):
            events.append((endpoint.stem, method))
            clock.now += 0.05
            return request(endpoint.stem, method, clock)

        def fake_place(wid, target, deadline):
            events.append((wid, "place"))
            if place:
                place(wid)

        with patch.object(vscode.time, "monotonic", side_effect=clock.monotonic), patch.object(
            vscode.time, "sleep", side_effect=clock.sleep), patch.object(
            vscode, "_LiveWindows") as live, patch.object(
            vscode, "_target", return_value=PLACEMENT), patch.object(
            vscode, "_place", side_effect=fake_place) as placed, patch.object(
            vscode, "_request", side_effect=fake_request), patch.object(
            vscode, "launch_graphical_service") as launch:
            live.return_value.get.return_value = windows
            try:
                result = vscode.restore_vscode({"provider": "vscode", "version": 1, "windows": items},
                                               timeout=30, **kwargs)
                error = None
            except ProviderRestoreError as caught:
                result, error = None, caught
        launch.assert_not_called()
        return result, error, events, placed

    @staticmethod
    def state_of(items):
        by_name = {item["window_key"]: item for item in items}
        return lambda name: by_name[name]

    def test_probe_timeout_then_ready_places_once_before_content(self):
        item = project("a")
        probes = []

        def request(name, method, clock):
            if method == "probe":
                probes.append(1)
                if len(probes) == 1:
                    raise BUSY
                return {"ready": True}
            return item
        result, error, events, placed = self.run_restore([item], request)
        self.assertIsNone(error)
        self.assertEqual(int(result), 1)
        placed.assert_called_once()
        self.assertLess(events.index((1, "place")), events.index(("a", "probe")))
        self.assertEqual(result.results[0].placement.state, EvidenceState.VERIFIED)

    def test_ready_window_is_processed_before_busy_window_retries(self):
        a, b = project("a"), project("b")
        busy = {"a": 3}

        def request(name, method, clock):
            if method == "probe":
                if name == "a" and busy["a"]:
                    busy["a"] -= 1
                    raise BUSY
                return {"ready": True}
            return {"a": a, "b": b}[name]
        result, error, events, _ = self.run_restore([a, b], request)
        self.assertIsNone(error)
        self.assertEqual(int(result), 2)
        self.assertLess(events.index(("b", "probe")), events.index(("a", "probe"), events.index(("a", "probe")) + 1))
        self.assertEqual([r.item_id for r in result.results], ["1", "2"])
        self.assertTrue(all(r.content.state == EvidenceState.VERIFIED for r in result.results))

    def test_never_ready_ends_retryable_failure_with_placement_outcome(self):
        item = project("a")

        def request(name, method, clock):
            if method == "probe":
                return {"ready": False}
            return item

        def place(wid):
            raise PlacementPending("pending", "native-handoff:x")
        result, error, events, _ = self.run_restore([item], request, place)
        self.assertIsNone(result)
        evidence = error.results[0]
        self.assertEqual(evidence.identity.state, EvidenceState.VERIFIED)
        self.assertEqual(evidence.placement.state, EvidenceState.WAITING)
        self.assertEqual(evidence.placement.request_id, "native-handoff:x")
        self.assertEqual(evidence.content.state, EvidenceState.FAILED)
        self.assertTrue(evidence.content.retryable)
        self.assertIn("not ready", evidence.content.detail)
        self.assertEqual(events.count((1, "place")), 1)

    def test_first_state_busy_then_ok_succeeds(self):
        item = project("a")
        states = []

        def request(name, method, clock):
            if method == "state":
                states.append(1)
                if len(states) == 1:
                    raise BUSY
                return item
            return {"ready": True}
        result, error, events, placed = self.run_restore([item], request)
        self.assertIsNone(error)
        placed.assert_called_once()

    def test_first_state_never_ok_is_retryable_identity_failure_without_placement(self):
        item = project("a")

        def request(name, method, clock):
            raise BUSY
        result, error, events, placed = self.run_restore([item], request)
        evidence = error.results[0]
        self.assertEqual(evidence.identity.state, EvidenceState.FAILED)
        self.assertTrue(evidence.identity.retryable)
        self.assertIn("busy", evidence.identity.detail)
        placed.assert_not_called()

    def test_identity_mismatch_after_selection_is_fatal_without_retry(self):
        item = project("a")
        other = project("other")

        def request(name, method, clock):
            return other
        result, error, events, placed = self.run_restore([item], request)
        evidence = error.results[0]
        self.assertEqual(evidence.identity.state, EvidenceState.FAILED)
        self.assertIn("changed before placement", str(error))
        self.assertEqual(events, [("a", "state")])
        placed.assert_not_called()

    def test_pending_native_placement_with_verified_content_stays_waiting(self):
        item = project("a")

        def request(name, method, clock):
            return {"ready": True} if method == "probe" else item

        def place(wid):
            raise PlacementPending("Saved VS Code placement is owned by GNOME; awaiting compositor verification",
                                   "native-handoff:a")
        result, error, events, placed = self.run_restore([item], request, place)
        evidence = error.results[0]
        self.assertEqual(evidence.identity.state, EvidenceState.VERIFIED)
        self.assertEqual(evidence.content.state, EvidenceState.VERIFIED)
        self.assertEqual(evidence.placement.state, EvidenceState.WAITING)
        self.assertEqual(evidence.placement.request_id, "native-handoff:a")
        self.assertTrue(vscode.waiting_only(error.results))
        self.assertIn("awaiting compositor verification", str(error))

    def test_deadline_during_content_checks_names_the_deadline(self):
        item = project("a")

        def request(name, method, clock):
            return {"ready": True} if method == "probe" else item

        def place(wid):
            self.clock.now = 40  # Placement used up the whole category budget.
        result, error, events, placed = self.run_restore([item], request, place)
        evidence = error.results[0]
        self.assertEqual(evidence.placement.state, EvidenceState.VERIFIED)
        self.assertEqual(evidence.content.state, EvidenceState.FAILED)
        self.assertTrue(evidence.content.retryable)
        self.assertIn("deadline exceeded", evidence.content.detail)
        self.assertNotIn(("a", "probe"), events)

    def test_missing_editor_keeps_attention_path(self):
        item = project("a")
        item["editor_uris"] = ["file:///tmp/a/x.txt"]
        live_state = project("a")

        def request(name, method, clock):
            return {"ready": True} if method == "probe" else live_state
        result, error, events, _ = self.run_restore([item], request)
        evidence = error.results[0]
        self.assertEqual(evidence.content.state, EvidenceState.UNKNOWN)
        self.assertEqual(evidence.attention, (evidence.content.detail,))
        self.assertEqual(events.count(("a", "probe")), 1)


if __name__ == "__main__":
    unittest.main()
