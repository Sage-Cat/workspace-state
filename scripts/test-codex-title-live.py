#!/usr/bin/env python3
"""Opt-in native CLI binding check; isolated home, daemon and tmux socket.

The native CLI renders against a loopback empty-model catalog. No authentication
or inference is performed. This verifies terminal/thread binding, not a model
response or recovery of an authenticated conversation.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
capture = importlib.import_module("workspace_state.capture")
from workspace_state import codex_resume, codex_title


class Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"object":"list","data":[]}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        # Persist a synthetic user turn without calling an inference service.
        # The intentional local error returns the real renderer to its composer.
        body = b'{"error":{"message":"Synthetic local provider; no inference"}}'
        self.send_response(400)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    args = parser.parse_args()
    if not args.codex:
        parser.error("a real native Codex installation is required")
    version = subprocess.run([args.codex, "--version"], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    if not version.startswith("codex-cli "):
        raise RuntimeError("Refusing a synthetic conversation stand-in")
    original_run = subprocess.run
    with tempfile.TemporaryDirectory(prefix="wsctl-native-title.") as temporary:
        root = Path(temporary)
        home, work = root / "codex", root / "work"
        home.mkdir()
        work.mkdir()
        socket = root / "tmux.sock"
        server = ThreadingHTTPServer(("127.0.0.1", 0), Catalog)
        Thread(target=server.serve_forever, daemon=True).start()
        (home / "config.toml").write_text(f'''model_provider = "render-test"
model = "gpt-6-astra"
[model_providers.render-test]
name = "Render fixture, no inference"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[projects."{work}"]
trust_level = "trusted"
[tui]
terminal_title = ["thread-id"]
screen_reader_detection_done = true
[tui.model_availability_nux]
"gpt-6-astra" = 4
"gpt-6.1-sol" = 4
''')
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("CODEX_") and key not in {
                           "OPENAI_API_KEY", "OPENAI_ACCESS_TOKEN", "OPENAI_ORG_ID",
                           "OPENAI_PROJECT_ID", "TMUX", "TMUX_PANE"}}
        environment.update(CODEX_HOME=str(home), TMUX="")

        def routed(command, **options):
            if command[0] == "tmux":
                command = ["tmux", "-S", str(socket), "-f", "/dev/null", *command[1:]]
                options.setdefault("env", environment)
            return original_run(command, **options)

        def tmux(*arguments):
            result = routed(["tmux", *arguments], check=True, text=True,
                            capture_output=True, timeout=10)
            return result.stdout.strip()

        def await_binding(previous=None):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                pane_pid = int(tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
                found = capture.codex_for_pane(pane_pid, str(work), pane_id=pane)
                pid = found["pid"] if found else pane_pid
                proof = codex_title.native_title_proof(pid, pane, home)
                if proof and proof.session_id != previous:
                    assert found and found["session_id"] == proof.session_id, found
                    assert found["confidence"] == "native-thread-title", found
                    assert codex_resume.resumed_session(pid, proof.session_id, pane)
                    return proof
                time.sleep(0.1)
            client = codex_resume._terminal_client(pid, pane)
            title = codex_title._pane_title(pane)
            loaded = codex_title.loaded_thread_ids(home)
            identity = codex_title._matched_title(title[2], loaded) if title else None
            bounds = codex_title._start_bounds(client.chain[pid].start_ticks) if client else None
            raise RuntimeError("Native binding did not become observable: " + json.dumps({
                "capture": found, "client": client is not None, "title": title,
                "loaded_count": len(loaded or []), "matched": identity,
                "created_ms": codex_title._created_at(home, identity) if identity else None,
                "start_bounds": bounds,
                "explicit_mode": codex_title._explicit_title_mode(pid, home, client.argv, bounds[0])
                if client and bounds else False,
                "composer": codex_resume._composer_layout(client, codex_resume.pane_text(pane))
                if client else False,
                "fixture_config_mtime_ns": (home / "config.toml").stat().st_mtime_ns,
                "fixture_tui": __import__("tomllib").loads((home / "config.toml").read_text()).get("tui"),
            }))

        try:
            # Separate configuration creation from the truncated Linux start
            # tick, so the launch-time guard can prove it predates this client.
            time.sleep(0.05)
            pane = tmux("new-session", "-d", "-s", "fixture", "-x", "120", "-y", "35",
                        "-c", str(work), "-P", "-F", "#{pane_id}", shlex.join([
                            args.codex, "--no-alt-screen"]))
            with patch.object(subprocess, "run", side_effect=routed), patch.dict(
                os.environ, {"CODEX_HOME": str(home)},
            ):
                time.sleep(2)
                tmux("send-keys", "-t", pane, "-l", "Synthetic binding test.")
                time.sleep(0.4)
                tmux("send-keys", "-t", pane, "Enter")
                first = await_binding()
                tmux("send-keys", "-t", pane, "-l", "/new")
                time.sleep(0.4)  # Let native bracketed-paste handling settle.
                tmux("send-keys", "-t", pane, "Enter")
                time.sleep(1)
                tmux("send-keys", "-t", pane, "-l", "Synthetic new thread test.")
                time.sleep(0.4)
                tmux("send-keys", "-t", pane, "Enter")
                second = await_binding(first.session_id)
                assert second.pid == first.pid
                assert not codex_resume.resumed_session(second.pid, first.session_id, pane)
                assert first.title != second.title
                print(json.dumps({"passed": True, "native_cli": version,
                                  "fresh_thread_capture": True, "new_thread_capture": True,
                                  "old_thread_not_ready": True, "same_client": True,
                                  "authentication": False, "inference": False,
                                  "provider_catalog": "loopback fixture"}))
        finally:
            original_run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
            # The shared daemon belongs only to this temporary home. Its exact
            # executable/root are checked before stopping a leftover process.
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    argv = (entry / "cmdline").read_bytes().split(b"\0")
                    controls = (entry / "environ").read_bytes().split(b"\0")
                    if b"app-server" in argv and os.fsencode("CODEX_HOME=" + str(home)) in controls:
                        os.kill(int(entry.name), 15)
                except (OSError, ProcessLookupError):
                    pass
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
