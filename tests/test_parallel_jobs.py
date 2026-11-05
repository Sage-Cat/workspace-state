from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from workspace_state import cli, concurrency, login_status

from workspace_state.startup import StageMarker, read_stage_marker, write_stage_marker


class ParallelJobsTests(unittest.TestCase):
    def test_nested_jobs_reuse_worker_without_multiplying_thread_pool(self):
        barrier = threading.Barrier(4)
        def parent():
            owner = threading.get_ident()
            barrier.wait(timeout=3)
            results = list(concurrency.completed_jobs({str(i): threading.get_ident for i in range(4)}))
            self.assertTrue(all(value == owner and error is None for _, value, error in results))
        results = list(concurrency.completed_jobs({str(i): parent for i in range(4)}))
        self.assertTrue(all(error is None for _, _, error in results))

    def test_worker_bound_and_real_overlap(self):
        lock = threading.Lock()
        barrier = threading.Barrier(4)
        active = peak = 0

        def job():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=3)
            with lock:
                active -= 1
            return True

        results = list(concurrency.completed_jobs({str(i): job for i in range(8)}))
        self.assertEqual(peak, 4)
        self.assertTrue(all(result and error is None for _, result, error in results))

    def test_failed_job_does_not_abandon_a_running_peer(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()

        def slow():
            started.set()
            self.assertTrue(release.wait(3))
            finished.set()

        def failure():
            self.assertTrue(started.wait(3))
            raise ValueError("failed job")

        results = concurrency.completed_jobs({"slow": slow, "failure": failure})
        name, _, error = next(results)
        self.assertEqual(name, "failure")
        self.assertIsInstance(error, ValueError)
        self.assertFalse(finished.is_set())
        release.set()
        list(results)
        self.assertTrue(finished.is_set())

    def test_capture_jobs_overlap_without_shared_snapshot_mutation(self):
        barrier = threading.Barrier(4)

        def capture(value):
            def job(*_, **_kwargs):
                barrier.wait(timeout=3)
                if "sessions" in value:
                    return {**value, "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
                return value
            return job

        with patch.object(cli, "capture", side_effect=capture({"sessions": []})), \
             patch.object(cli, "capture_browser", side_effect=capture({"available": True})), \
             patch.object(cli, "capture_social_apps", side_effect=capture({"social": True})), \
             patch.object(cli, "capture_file_manager", side_effect=capture({"windows": []})), \
             patch.object(cli, "capture_vscode", return_value={"provider": "vscode", "version": 1, "windows": []}), \
             patch("workspace_state.checkpoint.capture_shell", return_value={"available": True}), \
             patch("workspace_state.checkpoint.workspace_names", return_value=[]):
            result = cli._capture_all()
        self.assertEqual(result["file_manager"], {"windows": []})
        self.assertEqual(result["social_apps"], {"social": True})

    def test_startup_failure_joins_other_categories_before_publishing(self):
        barrier = threading.Barrier(4)
        finished = set()
        lock = threading.Lock()

        def restore(_snapshot, args, **_):
            if args.category not in {"vscode"}:
                barrier.wait(timeout=3)
            with lock:
                finished.add(args.category)
            if args.category == "browsers":
                raise RuntimeError("browser unavailable")
            return {**{category: 0 for category in cli.CATEGORIES}, "codex_verified": 1}

        def publish():
            self.assertEqual(finished, set(cli.CATEGORIES))

        args = argparse.Namespace(category=None, dry_run=False, owns_tmux_restore=True,
                                  force=False, wait=0, no_place=False, workspace=None,
                                  session=None, select=False)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(cli, "_startup_directory", return_value=Path(directory)), \
             patch.object(cli, "load", return_value={"sessions": []}), \
             patch.object(cli, "_restore", side_effect=restore), \
             patch.object(cli, "_wait_for_shell"), \
             patch.object(cli, "_publish_workspace_restored", side_effect=publish) as publisher, \
             patch.object(cli, "set_overall"), patch.object(cli, "update_stage"):
            self.assertEqual(cli.cmd_startup(args), 0)  # a failed category is a result
            self.assertEqual(read_stage_marker(Path(directory) / "browsers.done").state, "failed")
        publisher.assert_called_once()

    def test_concurrent_hud_updates_do_not_lose_stages_or_errors(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(os.environ, {"XDG_RUNTIME_DIR": directory, "XDG_STATE_HOME": directory}):
            login_status.initialize("test-parallel")
            def update(stage):
                for i in range(5):
                    self.assertTrue(login_status.update_stage(stage, "running", f"step {i}", current=i, total=5))
                self.assertTrue(login_status.update_stage(stage, "failed" if stage == "browsers" else "ready", "finished"))
            jobs = {stage: lambda stage=stage: update(stage) for stage in cli.CATEGORIES}
            self.assertTrue(all(error is None for _, _, error in concurrency.completed_jobs(jobs)))
            document = json.loads(login_status.status_path().read_text())
            states = {stage["id"]: stage["state"] for stage in document["stages"]}
            self.assertEqual(states["browsers"], "failed")
            self.assertTrue(all(states[stage] == "ready" for stage in cli.CATEGORIES if stage != "browsers"))


if __name__ == "__main__":
    unittest.main()
