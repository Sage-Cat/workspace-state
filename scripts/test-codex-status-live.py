#!/usr/bin/env python3
"""Opt-in native /status checks with a private home, tmux socket and provider.

No account or inference service is used. The deliberately held loopback error
request creates a real busy renderer; /status itself must make zero requests.
The alternate-screen control seeds each new thread with one private native
/status command before using the helper. Its initial welcome-logo redraw is
unsupported by the helper's strict visible-output freshness guard.
Passive reuse is checked in a private runtime with the real current boot and a
synthetic login generation. A fixture with 76 rows also verifies an explicitly
owned height restoration to 37 rows without additional terminal input.
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
import secrets
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from workspace_state import codex_status, native_status_evidence
from workspace_state.codex_readiness import loaded_thread_ids
capture = importlib.import_module("workspace_state.capture")


class Provider(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Catalog)
        self.started = Event()
        self.release = Event()
        self.lock = Lock()
        self.requests = 0
        self.posts = 0

    def record(self, *, post=False):
        with self.lock:
            self.requests += 1
            self.posts += int(post)

    def counts(self):
        with self.lock:
            return self.requests, self.posts


class Catalog(BaseHTTPRequestHandler):
    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def do_GET(self):
        self.server.record()
        self.reply(200, b'{"object":"list","data":[]}')

    def do_POST(self):
        self.server.record(post=True)
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self.server.started.set()
        self.server.release.wait(20)
        self.reply(400, b'{"error":{"message":"Synthetic local provider; no inference"}}')

    def log_message(self, *args):
        pass


def descendants(pid):
    found = {pid}
    try:
        children = Path(f"/proc/{pid}/task/{pid}/children").read_text().split()
    except OSError:
        return found
    for child in children:
        found.update(descendants(int(child)))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--width", type=int, default=159)
    parser.add_argument("--height", type=int, default=37)
    parser.add_argument("--alternate-screen", action="store_true")
    parser.add_argument("--expect-identical-repeat-refusal", action="store_true",
                        help="check the known conservative refusal when repeated status fills an alternate viewport")
    args = parser.parse_args()
    if not args.codex:
        parser.error("a real native Codex installation is required")
    if not 80 <= args.width <= 400 or not 37 <= args.height <= 80:
        parser.error("supported fixture dimensions are width 80..400, height 37..80")
    if args.expect_identical_repeat_refusal and not args.alternate_screen:
        parser.error("identical-repeat refusal is an alternate-screen control")
    executable = shutil.which(args.codex)
    if not executable:
        parser.error("the requested Codex executable was not found")
    version = subprocess.run([executable, "--version"], check=True, capture_output=True,
                             text=True, timeout=10).stdout.strip()
    if not version.startswith("codex-cli "):
        raise RuntimeError("Refusing a synthetic conversation stand-in")
    original_run = subprocess.run
    with tempfile.TemporaryDirectory(prefix="wsctl-native-status.") as temporary:
        root = Path(temporary)
        home, work = root / "codex", root / "work"
        home.mkdir()
        work.mkdir()
        runtime = root / "runtime"
        runtime.mkdir(mode=0o700)
        runtime_state = runtime / "workspace-state"
        runtime_state.mkdir(mode=0o700)
        generation = secrets.token_hex(16)
        (runtime_state / "login-generation").write_text(generation)
        (runtime_state / "login-generation").chmod(0o600)
        scope = {"boot_id": native_status_evidence.operations.boot_id(), "login_generation": generation}
        socket = root / "tmux.sock"
        server = Provider()
        Thread(target=server.serve_forever, daemon=True).start()
        (home / "config.toml").write_text(f'''model_provider = "render-test"
model = "gpt-6-astra"
[model_providers.render-test]
name = "Status fixture, no inference"
base_url = "http://127.0.0.1:{server.server_port}/v1"
wire_api = "responses"
requires_openai_auth = false
[projects."{work}"]
trust_level = "trusted"
[tui]
screen_reader_detection_done = true
[tui.model_availability_nux]
"gpt-6-astra" = 4
"gpt-6.1-sol" = 4
''')
        # Start from an isolated environment rather than inheriting account,
        # provider, proxy, tmux or user configuration controls.
        environment = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(root),
                       "LANG": "C.UTF-8", "TERM": "xterm-256color",
                       "CODEX_HOME": str(home), "XDG_RUNTIME_DIR": str(runtime), "TMUX": ""}
        terminal_inputs = []

        def routed(command, **options):
            if command[0] == "tmux":
                if command[1] == "send-keys":
                    terminal_inputs.append(command[2:])
                command = ["tmux", "-S", str(socket), "-f", "/dev/null", *command[1:]]
                options.setdefault("env", environment)
            return original_run(command, **options)

        def tmux(*arguments):
            return routed(["tmux", *arguments], check=True, text=True,
                          capture_output=True, timeout=10).stdout.strip()

        def wait_for(check, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result = check()
                if result:
                    return result
                time.sleep(0.1)
            raise RuntimeError("Private native renderer did not reach the required state")

        def observation():
            pane_pid = int(tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
            candidates = []
            for candidate in descendants(pane_pid):
                try:
                    if Path(f"/proc/{candidate}/comm").read_text().strip() == "codex":
                        state = codex_status._snapshot(candidate, pane, time.monotonic() + 1)
                        candidates.append((candidate, state))
                except (OSError, codex_status.StatusRefused):
                    pass
            if len(candidates) != 1:
                return None
            return candidates[0]

        def idle():
            first = observation()
            if first is None or not codex_status._safe_composer(first[1]):
                return None
            time.sleep(0.25)
            second = observation()
            return second if first == second and codex_status._safe_composer(second[1]) else None

        def composer(state):
            return state.screen.splitlines()[state.client.row].strip()

        def submit_owned_literal(text):
            # This is private test input, independently guarded before each
            # submission. It is never used to operate a user's client.
            pid, before = wait_for(idle)
            tmux("send-keys", "-t", pane, "-l", text)
            typed = wait_for(lambda: current if (current := observation())
                             and composer(current[1]) in {"› " + text, "» " + text} else None)
            time.sleep(0.4)  # Let native bracketed-paste handling settle.
            latest = observation()
            assert latest and latest[0] == pid
            assert codex_status._same_owner(before, latest[1])
            assert latest == typed, "Private literal changed before Enter"
            tmux("send-keys", "-t", pane, "Enter")

        proofs = []
        passive_output_invalidated = False

        def passive(proof):
            inputs = list(terminal_inputs)
            value = native_status_evidence.passive_identity(proof.pid, pane)
            assert terminal_inputs == inputs, "Passive lease verification sent input"
            return value

        def status():
            nonlocal passive_output_invalidated
            pid, before = wait_for(idle)
            catalog = loaded_thread_ids(home)
            requests = server.counts()
            proof = codex_status.probe(pid, pane)
            assert codex_status.revalidate(proof), "Fresh proof failed immediate revalidation"
            assert server.counts() == requests, "/status issued a provider request"
            assert loaded_thread_ids(home) == catalog, "/status changed the private loaded catalog"
            assert proof.client.chain == before.client.chain, "/status changed its terminal owner"
            assert proof.capture_record()["confidence"] == "native-status"
            if proofs and proofs[-1].screen != proof.screen:
                assert passive(proofs[-1]) is None, "Changed output reused the previous passive view"
                passive_output_invalidated = True
            inputs = list(terminal_inputs)
            native_status_evidence.remember([proof], scope)
            expected = {**proof.capture_record(), "confidence": "native-status-view"}
            assert passive(proof) == expected, "Exact native view was not reusable"
            pane_pid = int(tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
            assert capture.codex_for_pane(pane_pid, str(work), pane_id=pane) == expected, "Real pane capture did not use the passive proof"
            assert terminal_inputs == inputs, "Remember/capture sent additional input"
            lease = runtime_state / "native-status-evidence" / f"{proof.pid}.json"
            assert lease.stat().st_mode & 0o777 == 0o600
            assert lease.parent.stat().st_mode & 0o777 == 0o700
            assert not any(field in lease.read_text() for field in ("Server:", "Model:", "Session:", "Account:"))
            assert server.counts() == requests, "Passive proof used the provider"
            proofs.append(proof)
            return proof

        def refuse_without_input(*, running_turn=False):
            pid, before = wait_for(observation)
            requests = server.counts()
            inputs = list(terminal_inputs)
            try:
                codex_status.probe(pid, pane)
            except codex_status.StatusRefused:
                pass
            else:
                raise AssertionError("Helper accepted a draft or running turn")
            after = wait_for(observation)
            assert after[0] == pid and after[1].client.chain == before.client.chain
            assert after[1].client.argv == before.client.argv
            assert (after[1].width, after[1].height) == (before.width, before.height)
            if not running_turn:
                assert after[1].title == before.title
            assert composer(after[1]) == composer(before), "Refusal altered pending input"
            assert terminal_inputs == inputs, "Refusal sent terminal input"
            # A running native turn can start another already-owned provider
            # request concurrently. Do not attribute that traffic to a refused
            # helper which sent no input; idle status/draft controls check the
            # provider counter exactly, before any turn has started.
            if not running_turn:
                assert server.counts() == requests, "Refusal issued a provider request"

        def repeat_status():
            if not args.expect_identical_repeat_refusal:
                return status()
            pid, before = wait_for(idle)
            requests = server.counts()
            started = time.monotonic()
            try:
                codex_status.probe(pid, pane)
            except codex_status.StatusRefused as error:
                bounded_timeout = (isinstance(error.__cause__, subprocess.TimeoutExpired)
                                   and time.monotonic() - started >= 4.9)
                assert "deadline elapsed" in str(error) or bounded_timeout, "Unexpected repeat refusal"
            else:
                raise AssertionError("Identical native viewport was incorrectly accepted as fresh")
            after = wait_for(idle)
            assert after == (pid, before), "Repeat refusal was not an identical owned empty-composer viewport"
            assert server.counts() == requests, "Refused /status issued a provider request"
            return None

        try:
            pane = tmux("new-session", "-d", "-s", "fixture", "-x", str(args.width),
                        "-y", str(args.height), "-c", str(work), "-P", "-F", "#{pane_id}",
                        shlex.join([executable, *([] if args.alternate_screen else ["--no-alt-screen"])]))
            with patch.object(subprocess, "run", side_effect=routed), patch.dict(
                os.environ, {"XDG_RUNTIME_DIR": str(runtime)},
            ):
                if args.alternate_screen:
                    submit_owned_literal("/status")
                    wait_for(idle)
                first = status()
                repeated = repeat_status()
                if repeated is not None:
                    assert repeated.session_id == first.session_id
                    assert repeated.pid == first.pid
                    assert not codex_status.revalidate(first), "Previous status output stayed valid"
                submit_owned_literal("/new")
                wait_for(idle)
                assert not codex_status.revalidate(repeated or first), "Old thread proof stayed valid after /new"
                assert passive(repeated or first) is None, "Old passive view stayed valid after /new"
                if args.alternate_screen:
                    submit_owned_literal("/status")
                    wait_for(idle)
                second = status()
                again = repeat_status()
                assert second.session_id != first.session_id
                if again is not None:
                    assert again.session_id == second.session_id
                    assert again.pid == first.pid
                assert all(proof.client.chain == first.client.chain for proof in proofs)
                assert server.counts() == (0, 0), "Native status/new controls called the provider"

                height_restored = False
                if args.height == 76:
                    original = again or second
                    inputs = list(terminal_inputs)
                    tmux("resize-window", "-t", pane, "-y", "37")
                    pid, resized = wait_for(idle)
                    assert pid == original.pid and resized.height == 37 and resized.width == original.width
                    assert not codex_status.revalidate(original), "Normal proof accepted a resize"
                    assert passive(original) is None, "Passive proof accepted a resize"
                    renewed = native_status_evidence.after_owned_height_restore(original, 37)
                    assert renewed.session_id == original.session_id and codex_status.revalidate(renewed)
                    native_status_evidence.remember([renewed], scope)
                    expected = {**renewed.capture_record(), "confidence": "native-status-view"}
                    assert passive(renewed) == expected
                    pane_pid = int(tmux("display-message", "-p", "-t", pane, "#{pane_pid}"))
                    assert capture.codex_for_pane(pane_pid, str(work), pane_id=pane) == expected
                    assert terminal_inputs == inputs, "Owned height rebinding sent input"
                    assert server.counts() == (0, 0), "Height rebinding used the provider"
                    again = renewed
                    height_restored = True

                draft = "Synthetic pending draft: do not submit"
                tmux("send-keys", "-t", pane, "-l", draft)
                wait_for(lambda: current if (current := observation())
                         and composer(current[1]) in {"› " + draft, "» " + draft} else None)
                time.sleep(0.4)
                assert not codex_status.revalidate(again or second)
                assert passive(again or second) is None, "Pending draft reused a passive view"
                refuse_without_input()
                # Remove only the draft that this private fixture created.
                # The production helper never sends C-u, Escape or a clear.
                tmux("send-keys", "-t", pane, "C-u")
                wait_for(idle)

                submit_owned_literal("Synthetic local provider busy-state control.")
                assert server.started.wait(10), "Private busy request did not start"
                busy = wait_for(lambda: current if (current := observation())
                                and "esc to interrupt" in current[1].screen.lower() else None)
                assert composer(busy[1]) in {"›", "»", "› Ask Codex to do anything", "» Ask Codex to do anything"}
                assert not codex_status._safe_composer(busy[1]), "Busy empty composer looked idle"
                assert passive(again or second) is None, "Running turn reused a passive view"
                refuse_without_input(running_turn=True)
                server.release.set()
                wait_for(idle)
                print(json.dumps({"passed": True, "native_cli": version,
                                  "dimensions": [args.width, args.height],
                                  "fresh_status": True, "new_status": True,
                                  "repeat_status_proofs": not args.expect_identical_repeat_refusal,
                                  "identical_viewport_repeat_refused": args.expect_identical_repeat_refusal,
                                  "same_terminal_owner": True, "draft_refused_untouched": True,
                                  "busy_empty_composer_refused": True, "status_provider_requests": 0,
                                  "passive_native_capture": True,
                                  "passive_revalidation_additional_input": 0,
                                  "passive_new_draft_busy_invalidated": True,
                                  "passive_output_change_invalidated": passive_output_invalidated,
                                  "height_restore_exercised": args.height == 76,
                                  "owned_height_restore_verified": height_restored,
                                  "busy_control_provider_posts": server.counts()[1],
                                  "authentication": False, "inference": False,
                                  "alternate_screen": args.alternate_screen,
                                  "private_seed_status_commands": 2 if args.alternate_screen else 0,
                                  "unsupported": ["initial alternate-screen welcome-logo redraw",
                                                  "byte-identical repeated status viewport"]}))
        finally:
            server.release.set()
            original_run(["tmux", "-S", str(socket), "kill-server"], capture_output=True)
            # Stop only leftover native daemons with this exact private home.
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
