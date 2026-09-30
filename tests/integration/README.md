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

Copy `run_vm_scale.py` and `vm_scale_fixture.py` together into the disposable
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
local tabs (4/4/1/11/11/10/1) and three existing groups, four social application
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
entry passes `--password-store=basic`, because this credential-free guest has no
login keyring. The real applications retain their packaged rendering backends;
this virtual display does not validate physical GPU acceleration.
Chrome's fixture launcher registers the companion through private DevTools pipes
on every browser start because CDP unpacked installation is temporary. This does
not test persistence of a user's extension installation. When the managed
companion revision changes, the launcher archives only the synthetic profile's
service-worker cache to avoid a native Chrome crash observed during temporary
CDP re-registration. It verifies that session files remain unchanged, retains
tabs/groups/settings, and leaves the cache intact for same-revision cycles.

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
