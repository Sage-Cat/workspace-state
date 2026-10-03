# Integration tests

Run these from the workspace-state checkout. They use temporary state and do not
restore or shut down the active desktop.

## Lifecycle processes

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -p test_lifecycle_integration.py -v
```

Checks operation ownership, stale publishers, cancellation, concurrent writes,
login generations and preservation of pending shell input. Requires Python,
Bash and tmux; the tmux test uses a separate socket.

## Native window placement

```sh
python3 tests/integration/run_headless.py --run --output /tmp/window-test
```

Checks three virtual monitors, resizing a maximized window before an inactive
workspace handoff, verified placement, deferred placement and cancellation.
Requires GNOME Shell 46 with headless Wayland support, `gdbus`,
`dbus-run-session`, `glib-compile-schemas`, Python GTK 3 bindings and the sibling
`gnome-winctl` checkout.

## Application companions

```sh
python3 tests/integration/run_headless.py --run --companions --output /tmp/companion-test
```

Adds Chrome, VS Code and Nemo checks where installed. Each uses temporary
profiles and generated content. Missing or unsupported dependencies are recorded
as skips, not passes.

## HUD cancellation

```sh
python3 tests/integration/run_hud_headless.py --run --output /tmp/hud-test
```

Checks real modal grabs, cancellation, late status, countdown and enable/disable
behavior. Requires sibling `login-hud` and `gnome-winctl` checkouts. The fixture
blocks native shutdown handoff.

## Results and isolation

- Each native run uses its own session bus, XDG directories and Wayland socket.
- `HOME` is preserved; no existing application profiles are imported.
- Deadlines and process-group cleanup bound each run.
- `results.json` records checks and failures; logs stay in the output directory.
- Virtual monitors do not test physical GPU or hotplug behavior.

[Lifecycle design](../../docs/lifecycle-architecture.md)

## Disposable Ubuntu VM at desktop scale

`run_vm_scale.py` is deliberately restricted to a KVM guest named
`wsctl-validation`, and requires `--disposable-guest`. It configures that guest's
applications, default file manager, synthetic launchers, tmux plugins and user
services. Never run it on a working desktop or copy real profiles into the guest.

Install the desktop release first, with all native companions. The guest needs
Alacritty, tmux, Google Chrome, VS Code, Nemo with its Python bridge, Python GTK 3,
`virt-viewer`, `qemu-system-x86`, `qemu-system-modules-spice`, and the real
`tmux-plugins/tmux-resurrect` and `tmux-plugins/tmux-continuum` checkouts under
`~/.tmux/plugins/`. Guest sudo must support noninteractive creation of the
explicit synthetic bind mounts and restarting its display manager.

Use three connected virtual outputs with EDIDs. QEMU's D-Bus display backend on
a **private** bus can expose three virtio-vga heads; call
`org.qemu.Display1.Console.SetUIInfo` on each corresponding `Console_N` with
physical dimensions, pixel offset and resolution before seeding. Merely forcing
disconnected DRM connectors on produces unknown identities and correctly fails
later checkpoint safety checks.

Copy `run_vm_scale.py`, `vm_scale_fixture.py` and `vm_slow_pages.py` into the disposable
guest's home directory, outside its clean deployment checkout. Run the phases
there, preserving that path for the fixture's startup launchers:

```sh
cd "$HOME"
python3 run_vm_scale.py --disposable-guest --real-social seed
python3 run_vm_scale.py --disposable-guest cycle
# After startup reaches a terminal outcome:
python3 run_vm_scale.py --disposable-guest verify
# After a successful login cycle, repeat through an actual guest reboot:
python3 run_vm_scale.py --disposable-guest reboot
# Reconnect after the guest boots and startup completes:
python3 run_vm_scale.py --disposable-guest verify
```

Seeding uses six native Alacritty windows, ten tmux sessions, 23 local conversation
processes with distinct open rollout UUIDs, seven native Chrome windows with 42
local HTTP tabs (4/4/1/11/11/10/1) and three existing groups, four social application
windows, four native Nemo windows, one native Code project and a real
`remote-viewer` connected to a tiny QEMU firmware display. It uses four GNOME
workspaces and the real full `wsctl save` checkpoint. The cycle ends the guest's
GNOME login, stops only its captured synthetic tmux server, and starts a new
login through GDM. Normal tmux-resurrect/continuum and the enabled workspace
coordinator then reconstruct the desktop and run login finalization.

`--real-social` requires installed Slack, Discord, Telegram and Viber Snap desktop
applications and uses their unauthenticated windows. Omit that option to use
explicitly labeled GTK proxies instead; proxy mode does not test application
startup behavior. Conversation processes are synthetic in both modes: no chat
service authentication or model calls occur. Three local bind mounts substitute
for cloud accounts. The VM payload is firmware only; its command shutdown profile
checks the synthetic SPICE endpoint and does not prove Windows hibernation.
The committed Windows VM restore-job stage is not exercised by this command
profile; it is expected to report a skip. Viewer launch and placement replay
are an autostart fixture using the real native placement companion. Code uses a
persistent basic password-store setting, and the real Slack fixture desktop
entry passes `--password-store=basic`. Other apps can request a default keyring;
create an empty test-only keyring through their native dialog before freezing
the fixture, and record that setup separately. Never import a host keyring.
The real applications retain their packaged rendering backends;
this virtual display does not validate physical GPU acceleration.
Chrome's fixture launcher registers the companion through private DevTools pipes
on every browser start because CDP unpacked installation is temporary. This does
not test persistence of a user's extension installation. When the managed
companion revision changes, the launcher archives only the synthetic profile's
service-worker cache to avoid a native Chrome crash observed during temporary
CDP re-registration. It verifies that session files remain unchanged, retains
tabs/groups/settings, and leaves the cache intact for same-revision cycles.
Its Python controller forwards the session's actual SIGTERM to Chrome and waits
at most eight seconds, recording the native exit. This prevents the extra test
controller from dropping Chrome's private pipes before Chrome handles logout.
It does not issue `Browser.close` or close windows ahead of real power-off.

Evidence stays under the guest's `~/.local/state/wsctl-scale/`, with separate
`cycles/<cycle-id>/` archives. Each passing result states the social fixture mode,
coverage limits, installed release and available social package versions.
The seed is frozen only after count validation;
both checkpoint SHA256 digests and the separate viewer target digest must remain
unchanged across each cycle. Counts, exact conversation UUIDs and tmux pane
ownership, folder/editor URIs, tab order and
group membership, saved workspace/monitor/frame placement, captured desktops,
checkpoint data, startup status and process replacement evidence are JSON.
Chrome's native group/window/tab IDs are compared before and after each restore;
a native-host readiness barrier records the browser's baseline before restoration
can mutate it. Chrome may assign new IDs when its browser process restarts. All
16 coordinator stages are required: 15 must be ready with verified provider
receipts, and the unsupported committed VM transaction must report its exact
documented skip. Social receipts require verified identity and placement; their
explicit content skip records that message/account recovery belongs to each
application and is outside this unauthenticated fixture. The running native and HUD companions must expose the production UUIDs and the
exact immutable release revision; experimental UUIDs are rejected. A new graphical
login is mandatory, and reboot mode requires a changed boot ID. On the same boot,
saved PID/start-time identities must be
absent for every captured native application and conversation process. Native
inventory includes minimized social windows and requires exactly 23 native
windows in total. Repeated cycles require the same counts, preventing duplicate
growth. Individual setup phases are
available for diagnosing a failed prerequisite; a failed phase is not a passed
scale run. This harness does not power off or reboot the host.

## Real GNOME power-off and browser adoption

Copy `run_vm_poweroff.py` alongside `run_vm_scale.py` in the same disposable
guest. It uses the installed immutable release and refuses to run outside the
named KVM guest with the scale fixture's consent record. After seeding and
bringing up the complete desktop, run:

```sh
python3 run_vm_poweroff.py --disposable-guest prepare
python3 run_vm_poweroff.py --disposable-guest watch
# While watch runs, request and confirm GNOME's real Power Off dialog using
# the VM console. Power the same VM back on, then wait for startup to finish.
# Copy the actual host exit observation to poweroff/<run-id>/qmp-exit.json.
python3 run_vm_poweroff.py --disposable-guest verify
```

The helper never requests shutdown, precloses applications, stops tmux, invokes
the older `cycle`/`reboot` shortcut, or fabricates HUD acknowledgements. Its
watcher records changed status, canonical snapshots and actual renderer,
worker, commit and prepared receipts every 200 ms for at most 120 seconds. It
must already be running before the real dialog is confirmed. Keep the watcher
alive independently of the SSH connection when operating the VM console.
An expired watcher is a failed observation, not evidence of successful shutdown.

Preparation first evolves the synthetic browser and attempts the original whole
browser catalog with placement disabled and ungrouped originals first. It requires
the ungrouped identity refusal and exact unchanged native window, tab and group
identities, archived separately in `stale-catalog-probe.json`. The old-version
`--expect browser-retention-regression` run explicitly records this probe as
skipped because that release predates the guard. Preparation also attempts a stale
grouped restore with placement disabled and requires a real refusal with unchanged
native browser identities. It explicitly labels these as fault injection rather
than a naturally failed startup. It records a corresponding failed startup
marker, performs a real manual full save, evolves the browser again and renames
an existing synthetic tmux window. The final expected state is captured directly
from applications and is not saved into the canonical checkpoint. Browser
evolution measures preserved group/window IDs, seven windows, 42 tabs and one
replaced tab; declared success strings alone cannot satisfy it.

Before any fault injection, save or browser/tmux changes, preparation requires
the fixture's exact inventory: six Alacritty windows, ten tmux sessions, 23
distinct fixture conversation UUIDs, seven Chrome windows with 42 tabs and three
groups, four Nemo windows, one Code window and one viewer. Every configured
desktop app must have exactly one visible captured and native window. The four
built-in social apps give 23 native windows total; configured extras such as
ChatGPT and Remmina raise that total to 25. Each native window must belong to
exactly one expected application. An extra bootstrap Alacritty, unmanaged
Firefox window, missing app or ambiguous identity causes a failure before the
desktop can be accepted as the new expected baseline. The same guard runs on
the final independent capture.

Verification requires a changed boot ID, the same installed release, matching
real renderer and worker receipts, and either the real commit receipt or the
coordinator's durable authorization with at least three seconds of countdown
evidence. Completed cancellation followed by a new attempt is recorded
explicitly; an earlier unfinished operation or stale receipt fails verification.
Current canonical data, the last shutdown snapshot and live application data
are checked against the independent expected content, placement and tmux
identities. Native inventory includes every window, including added ChatGPT or
Remmina applications, so missing or duplicated windows cannot pass through a
fixed old fixture count. Chrome window/tab/group IDs must remain unchanged
between native browser startup and workspace restoration. Every startup stage
must be ready except the explicitly documented synthetic VM-job skip; provider
identity, content and placement receipts are also checked.

A positive result also requires the previous boot's PID 1 journal records for
`UNIT=user@<uid>.service`, collected with a bounded, noninteractive privileged
read. Their boot identity must match preparation and a successful stop job must
be present. Timeout, failure, forced kill or main-process signal termination
fails the cycle even if a later journal line says “Stopped” and startup is green.
The structured receipt is archived as `verified-os_shutdown.json`.

The host must supply the real `qmp-exit.json` observation in that run's directory:
`vm_name` must be `wsctl-ubuntu-validation`, `run_id` and `previous_boot_id` must
match preparation, and `qmp_eof` must be true. Preserve the actual QMP `events`,
UTC `observed_at`, and nonempty `systemd_state` output. Exactly one `SHUTDOWN`
event must identify `guest: true` with `reason: guest-shutdown`; its original
seconds/microseconds timestamp must fall after preparation and before the current
guest boot time. Missing, foreign or stale host evidence blocks a positive
result. The validated receipt is archived as `verified-host_qmp_exit.json`.

Use `--expect browser-retention-regression prepare` with an older release for a
negative baseline. A reproduced stale-browser overwrite reports
`outcome: expected-regression`, `passed: false`, the exact differences and any
other failures. It is never reported as a passing desktop. Use `--new-run` only
to explicitly supersede an unfinished run; old evidence remains under
`~/.local/state/wsctl-scale/poweroff/<run-id>/`. Missing shutdown observations are
recorded even when sealed canonical evidence proves the negative regression;
that outcome remains `passed: false`. The 23 conversation
workers remain synthetic; this does not prove authenticated real Codex startup.

For delayed page loading, also copy `vm_slow_pages.py` beside the fixture and run
`python3 run_vm_scale.py --disposable-guest slow-pages` before preparing a new
power-off cycle. This converts the existing synthetic tabs to local HTTP pages
without creating groups or changing counts. A guest-only user service listens
on loopback and delays each response by three seconds, including after boot.
Its journal records actual request durations; archive that evidence with the
cycle result. The browser fixture allows this loopback origin through its test
proxy. No external website, account or host network configuration is involved.
Daytime mutation waits at most 15 seconds for complete navigation with no pending
URLs. The controller reports the exact 42 intended tab URLs; the next capture
must contain those loaded URLs and unchanged window/group identities. Temporary
empty or old URLs during slow navigation cannot become the expected checkpoint.
An older running fixture controller must be replaced during setup before this
test; missing loading metadata fails explicitly.
Ordinary seeding uses the same real HTTP server with zero delay. Fragment-only
navigation on a restored single `about:blank` tab caused a native Chrome 154
SIGTRAP during validation; full HTTP navigation preserved the windows and groups.
That upstream/browser-fixture limitation is not counted as a repaired desktop
restoration bug. A crashed browser's native Restore dialog must be handled before
the fixture's count barrier allows coordinator mutations.
