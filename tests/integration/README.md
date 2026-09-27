# Isolated GNOME integration

Run explicitly from the workspace-state checkout:

```sh
python3 tests/integration/run_headless.py --run --output /tmp/workspace-state-headless-evidence
```

Requires GNOME Shell with `--headless`, `--virtual-monitor`, and `--no-x11`,
`dbus-run-session`, `gdbus`, `glib-compile-schemas`, and Python PyGObject/GTK 3.
The adjacent `gnome-winctl` checkout supplies the extension under test. This is
not part of ordinary unit discovery and opens no window on the host desktop.

The harness retains `HOME`, uses temporary XDG settings/data/runtime directories,
a private session bus with no service activation directories, and a private
Wayland socket. It starts no session manager, loads only its temporary extension
copy, and uses an empty GTK application fixture rather than existing application
profiles. The temporary extension adds a test-only control method for workspace
activation and a one-shot compositor exception; production sources are unchanged.
It requests software rendering; the compositor may still open a DRM render device
without mode setting. This does not test physical display hotplug or GPU recovery.

It checks three-monitor startup, real window placement with observed verification,
deferred placement without workspace stealing, terminal replay failure attached to
the original token, and cancellation surviving workspace activation. The outer
100-second deadline terminates the private process group; normal cleanup stops the
fixture and compositor first. No real session resume, VM, shutdown, browser profile, or live
extension operation is performed.

`results.json` records each successful check and the first failure. The directory
also retains compositor, application, and driver logs plus the final desktop
snapshot on success. Missing desktop services in the compositor log are expected:
the private bus intentionally cannot activate host-profile services.

GNOME 46.2 validation on 2026-09-27 passed all five checks. Evidence was written to
`~/.local/state/workspace-state/reviews/2026-09-27-evidence/headless-integration/`.

## Lifecycle process integration (no GNOME required)

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_lifecycle_integration.py -v
```

These six tests run in ordinary discovery. Real Python subprocesses inherit exact
operation contexts and coordinate through pipes; mutable HUD status, owner records,
logs, locks, and legacy markers live under temporary XDG directories. Five-second
response deadlines and unconditional child cleanup bound failed runs. They verify:

- an already-running startup publisher cannot write into shutdown, cancellation
  recovery, or a later login, including when the presentation file is corrupt;
- cancellation and failed recovery retain the same owner until recovery completes,
  and a stale recovery process cannot cancel a successor;
- six concurrent publishers retain every stage and all 78 log events;
- two competing login generations can adopt boot-only legacy markers exactly once.

The sixth test, skipped only when tmux/bash is unavailable, creates a disposable
server on an explicit temporary socket with `/dev/null` configuration. It waits for
its profile-free shell prompt, types a harmless pending command without Enter, and
checks that restore refuses the existing shell before any mutation. Both captured
input and the absence of its execution marker are verified. All setup and cleanup
commands name the private socket; no host tmux server or real terminal session is resumed.

These tests cover the persistent ownership contract, not GNOME/systemd service
coordination. Coordinator callback/inhibitor ownership remains covered separately
by `test_operation_safety.py`.

## Opt-in real application companions

```sh
python3 tests/integration/run_headless.py --run --companions --output /tmp/workspace-state-companion-evidence
```

This adds Chrome, VS Code, and Nemo checks where installed, with a 180-second
outer deadline. Chrome and Code receive explicit temporary user-data directories;
Code also gets a private extensions directory containing only this companion.
Nemo loads the copied bridge from private XDG data and owns its name on the private
bus. Unreviewed additional system Nemo extensions cause an explicit skip. No
existing profiles are imported, `HOME` remains unchanged, and all opened content
is generated under the temporary directory. Chrome/Code use a blocked local proxy
and disable background update/telemetry features; no external page is opened.

Chrome's branded build rejects `--load-extension`, so this fixture loads the copied
extension through `Extensions.loadUnpacked` over inherited private DevTools pipes.
There is no TCP debugging listener. A concrete unsupported-API response is a skip;
loading/bridge failures after supported loading remain failures. The source native
messaging host is registered only in temporary profile/config directories. The test
maps two identical native titles to distinct Chrome window IDs, then disables its
private host wrapper and kills the exact receipt PID through a pidfd to verify that
companion loss cannot acknowledge identity.

VS Code must report the exact temporary profile, project and editor URI, answer its
storage probe, and identify one native window. Nemo must report both requested tab
URIs and correlate them with its own native window. Each companion's stamped build
revision is checked. Results and per-application logs are retained in the output
directory; skips are explicit and do not count as passing companion checks. Normal
placement checks still run first. Applications and descendants stay in the outer
private process group for bounded cleanup; none are restarted on the host desktop.

Final companion validation on 2026-09-27 passed all eight checks with Chrome
154.0.8037.57, VS Code 1.139.1, Nemo, and GNOME 46.2. No companion was skipped.
Evidence: `~/.local/state/workspace-state/reviews/2026-09-27-evidence/companion-integration/`.
The real fresh-profile run exposed and then verified fixes for Chrome's local
disconnect/reconnect behavior and Nemo's relative build-stamp loading.

## Native HUD grab and cancellation regression

```sh
python3 tests/integration/run_hud_headless.py --run --output /tmp/workspace-state-hud-evidence
```

This separate opt-in harness loads a temporary HUD UUID plus gnome-winctl into
one disposable headless GNOME compositor. It preserves `HOME`, isolates every
XDG directory, uses its own Wayland socket and a private session bus without
activation directories, and starts no session manager or application fixture.
The outer deadline covers preparation and the desktop run for 95 seconds,
followed by at most four seconds of owned-process-group cleanup.

`hud_fixture.js` is injected only into the temporary gnome-winctl extension. It
supplies the private login generation, current boot context and explicit confirmed
preflight ownership that normally come from the real session/coordinator, then
writes status and request files only below the temporary runtime directory.
The HUD's actual Gio asynchronous loader, UI construction, `Main.pushModal`,
`Main.popModal`, cancellation, countdown and lifecycle methods remain unchanged.
Native confirmation is guarded: any attempted handoff increments a failure
counter and throws. Native cancellation is recorded locally, so no real shutdown
or session-manager signal is required. The fixture cannot run outside its private
runtime prefix. GNOME's overview must finish releasing its own grab before the
baseline is recorded.

The ten checks verify passive enablement, an actual `Clutter.GrabState.ALL` grab,
synchronous release despite a cancellation-file write failure, rejection of late
ready status, cancellation with a stalled/unacknowledging backend, the real
five-second countdown and commit without unauthorized handoff, cancellation while
awaiting final backend authorization, disable with a genuine queued Gio load, and
re-enable with the old epoch rejected and the cancellation fence retained.
Waiting recovery remains visible while keyboard/pointer ownership is released.
These assertions inspect real `Main.modalCount` and the native grab; they do not
substitute a mocked modal API. No keyboard events are sent to the host desktop.

On GNOME 46.2 on 2026-09-27, all ten checks passed in 8.84 seconds. The baseline
modal count was zero, the HUD raised it to one, and cancellation returned it to
zero in 0.577 ms after an injected write failure and 3.452 ms while awaiting
backend authorization. Every case recorded zero native handoff attempts. The
running HUD diagnostics also verified the temporary UUID, version 18, and
`headless-hud-integration` build stamp.

Evidence: `~/.local/state/workspace-state/reviews/2026-09-27-evidence/hud-headless-integration/`.
`results.json` contains per-case native state and timing; `final-hud.json`,
`gnome-shell.log`, and `driver.log` retain the final state and logs. Missing
session services are expected on the deliberately bare private bus. This test
proves the local HUD grab lifecycle, not physical keyboard layout switching,
physical GPU behavior, or an actual system shutdown.
