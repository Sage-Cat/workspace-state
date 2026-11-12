# Testing

The test suite separates local regression checks, isolated native GNOME tests,
and complete power-off/boot cycles in a disposable Ubuntu VM. Application launch
is tested while signed out; account authentication is not an acceptance
requirement. Synthetic conversation workers provide scale without credentials
or model requests.

[Commands](#local-regression-checks) · [Headless GNOME](#native-window-placement) ·
[VM setup](#disposable-ubuntu-vm-at-desktop-scale) ·
[Power-off procedure](#real-gnome-power-off-and-browser-adoption) ·
[Terminal regression](#terminal-identity-and-names-2026-10-06) ·
[Original validation](#recorded-validation-2026-10-03)

## Local regression checks

Launcher tests exercise the actual finalizer entry point with systemd's minimal
PATH. Placement tests require one topology observation per target and retain
refusals for ambiguous workspace names and physical display mismatches. Terminal
tests cover mixed verified and ambiguous identities, repeated attempts, swapped
anchors and duplicate anchors. The native HUD harness installs all six runtime
files, including its report and view modules, before loading real GNOME Shell.

From the workspace-state checkout:

```sh
make check
```

This runs Python regression tests, JavaScript syntax and Chrome protocol checks,
and VS Code companion tests. It does not shut down the working desktop. The
regressions cover capture/adoption provenance, exact browser claims and ambiguity,
loading/cancellation/retry of startup restoration, provider readiness, operation
ownership, the bounded final save, stale service sockets, and the VM verifier's
false-pass guards.

The commands below use separate temporary state for native tests. Only the
explicit disposable-VM procedures change and shut down a complete guest desktop.

## Standalone manual save

The final standalone-save candidate passed 1,286 Python tests and the Chrome
and VS Code protocol checks. Its public guest harness ran three strict saves
against a real Ubuntu 24.04 GNOME/Wayland desktop in QEMU/KVM:

| Check | Result |
| --- | --- |
| Native desktop inventory | 25 unchanged window IDs, processes and placements |
| Terminal inventory | 6 terminal records, 10 tmux sessions, 23 synthetic UUID workers |
| Native tmux state | Literal names, labels, policies, layout, geometry and selection unchanged |
| Chrome content | 7 original windows, 42 tabs and 3 groups; IDs, URLs and membership unchanged |
| Checkpoint | All five categories fully captured and deliberately adopted; no retained recipe or warning |
| Lifecycle state | Startup status and operation record unchanged |
| Three commands | Exit 0; 1.637, 1.585 and 1.635 seconds |

The immutable candidate was `r-be0d082bd04b3a52b15a5391`. The guest coordinator
remained on `r-527d1ba937b3924da3065b5f`; this test invokes the candidate save
command without activating or restarting the desktop. It is a save test, not
an additional power-off cycle. Conversation workers are synthetic; native CLI checks are
separate and use no account or inference service.

```sh
python3 tests/integration/run_vm_manual_save.py \
  --disposable-guest --expected-release r-<24hex>
python3 scripts/test-codex-status-live.py
python3 scripts/test-codex-status-live.py --width 80 --height 37
python3 scripts/test-codex-status-live.py --alternate-screen --width 319 --height 76
python3 scripts/test-codex-status-live.py --alternate-screen --width 159 --height 37 \
  --expect-identical-repeat-refusal
```

The native 0.160.1 controls prove fresh and changed-thread identity, unchanged
PID/TTY ownership, and untouched draft/busy refusal. Status commands make zero
requests to the loopback provider. The deliberate busy-state control sends a
synthetic turn to a held local error response; it performs no inference.
The 159×37 alternate-screen repeat control expects a refusal when the entire
visible report is byte-identical. That is not a successful identity proof.
Welcome-logo redraws and unsupported/wrapped reports also refuse. Pane resizing
has an explicit refusal before input, before Enter, after Enter and at final
publication. No refusal clears input, interrupts a turn or guesses an ID.

The follow-up native controls also store and passively revalidate the explicitly
collected evidence. Real pane capture must reuse it without additional terminal
input or provider requests; `/new`, new output, draft input and busy rendering
withdraw it. A private 76-to-37-row restoration control checks that normal proof
validation rejects the resize. Explicit operator rebinding accepts only an exact
suffix crop containing the complete unchanged final report. Width, owner,
transcript or expected-height changes still refuse.

Unit regressions cover process/PID reuse, ancestry, arguments, foreground TTY,
catalog/title conflicts, boot/login changes, changed views and private-file
permissions. Capture, restore tokens and checkpoint sealing all pass the exact
native pane ID to the same identity reader.

An experimental native output observer did not resolve saturation: an identical
alternate-screen repeat emitted only limits, composer and footer cell updates,
with no new session UUID. There is no output-pipe fallback in production and
no reconstruction of identity from stale cells.

An early private verifier failed because its SSH environment omitted
`~/.local/bin`, so every GNOME window query was unavailable. The test environment
was corrected; Chrome code was not changed to hide that error. The public
harness uses the established guest environment and independently checks native
window and Chrome identities. Identification leases were cleaned up after both
failed attempts; no tabs, groups or windows were lost.

## Terminal identity and names (2026-10-06)

Fresh daemon-backed clients did not own rollout descriptors or carry resume UUIDs
in argv. Their pane labels survived capture, but their conversation IDs remained
unknown and automatic restoration opened shells. A separate split-order defect
reversed later panes before applying saved geometry. Narrow model footers were
also missed by composer readiness. The new regressions cover these paths, explicit
unknown-UUID HUD reporting, native title binding and literal naming policies.

```sh
make check
python3 scripts/test-tmux-names-live.py
python3 scripts/test-codex-title-live.py --codex /path/to/native/codex
```

The tmux test creates seven panes on a private socket, including a five-pane
asymmetric layout, nonzero base index and literal names with Unicode, format-like
text, semicolons and delimiters. It verifies active selection, rename policies,
application OSC updates and rejection of swapped, replaced or extra panes without
mutating them. It creates no terminal windows and never addresses the host socket.

The title test uses the real CLI and shared app-server with a temporary home.
A loopback provider returns an empty catalog and intentionally rejects synthetic
turns; no account or inference service is contacted. It proves fresh-thread
capture, `/new` capture by the same process and rejection of the obsolete UUID.
Native CLI 0.160.1 passed on the host; 0.160.0 passed in Ubuntu 24.04.5/QEMU.
This is real native rendering/binding, not authenticated conversation recovery.
Configuration changes after launch, older picker threads and ambiguous prefixes
remain visible refusals. See [the adapter limits](terminal-restoration.md).

The full guest procedure keeps the existing 23 synthetic workers and ten tmux
sessions. It moves two workers between existing fixture windows to form five
asymmetric panes, sets names and policies, and records independent native output,
UUID positions, geometry, selection and whole-desktop inventory. Installed
production `wsctl save` must match that observation without changing HUD markers.
No status is reset to make a trial pass.

```sh
python3 run_vm_tmux_names.py --disposable-guest --expected-release r-<24hex> prepare
python3 run_vm_tmux_names.py --disposable-guest watch
# Power off through the guest console (see the acceptance cycles below); observe QMP SHUTDOWN/EOF.
# Cold boot with all three display heads configured before login.
# Copy the matching host QMP artifact into the printed run directory.
python3 run_vm_tmux_names.py --disposable-guest verify
# Later cycles observe the settled desktop without a save or fixture mutation:
python3 run_vm_tmux_names.py --disposable-guest --expected-release r-<24hex> prepare-continuity
# Repeat the same watch, power-off, QMP evidence, cold boot and verify.
```

The verifier checks the independent pane oracle, the final-save snapshot,
the bounded final-save journal evidence, actual guest power-off, previous-boot
user-manager journal, normal startup and the rest of the synthetic desktop. This
names/layout scenario uses deliberate manual save; it does not replace the
no-manual-save browser upgrade tests below. Copy-mode readiness is tested
separately: an exact live-owned rollout can remain verified under a history
overlay, while daemon/title-only proofs still reject that overlay.

The final source suite passed 1,236 Python tests and Chrome/VS Code protocol
checks. Both isolated native checks passed on the host and guest. The first
whole-desktop names cycle passed terminal names, order, layout and all 23 worker
identities, but failed overall: Nemo restored only three of four windows and one
of those had wrong geometry. That failed trial remains recorded.

Nemo shared one 30-second budget across all windows and could wait past it on
the placement lock. A real-lock regression reproduced an expired wait with no
placement request and a misleading compositor error. The historical trace did
not record the exact lock-entry instant, so that specific timing is an inference;
the shared-budget defect and missing deadline checks are demonstrated. Each
window now gets its own budget, capped by the unchanged operation deadline.
Lock waits and native calls are bounded; cancellation and ownership are checked
after waits and before moves. An unissued placement has an explicit diagnostic.

The exact failed guest then shut down through GNOME and cold-booted into final
runtime `r-527d1ba937b3924da3065b5f`. All 25 native windows, four Nemo windows,
ten tmux sessions, five named asymmetric panes, 23 workers, seven Chrome windows,
42 tabs and three groups passed independent content/placement checks. This retry used the retained complete oracle;
there was no preparatory manual save, fixture mutation or corrective placement.
Real QMP shutdown/EOF, new boot identity and clean user-manager termination were
required. Account recovery and the next physical host login are not proved by
these guest trials.

The following ordinary cycle on that same runtime also passed the complete
independent oracle. `prepare-continuity` only
observed the settled desktop: no manual save, fixture mutation, HUD reset or
corrective placement was used. The final two successful cycles therefore cover
both retrying the retained failed desktop and normal subsequent continuity.
These two cycles predate the bounded final save and were judged by the retired
transaction harness; the acceptance criteria below replace that harness.

Terminal identity checks also inspect all eligible foreground candidates rather
than stopping at the first client. TTY, ancestry and process start ticks must
remain consistent. Shared daemon descriptors, conflicting UUIDs, prompt text and
nearby timestamps cannot identify an independent client. A fresh client without
exact ownership or a supported, proven native title remains unresolved;
signed-out launch tests do not prove authenticated conversation resumption.

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

## HUD startup

```sh
python3 tests/integration/run_hud_headless.py --run --output /tmp/hud-test
```

Checks the startup presentation in a private Shell: finite progress through
resize and row reconstruction, accessible Abort and Run in background actions,
the status indicator, dismissal and reopening, abort receipts, a
backend-cancelled restoration, the lock-screen session boundary (hidden, no
grab) and disable/re-enable with a Gio load in flight (the old epoch is rejected
and the startup status reloads). The HUD has no shutdown interface: a status
document with `mode: "shutdown"` must be ignored, both with no prior status and
while a startup status is shown, and no modal grab is ever taken. Requires
sibling `login-hud` and `gnome-winctl` checkouts.

## Results and isolation

- Each native run uses its own session bus, XDG directories and Wayland socket.
- `HOME` is preserved; no existing application profiles are imported.
- Deadlines and process-group cleanup bound each run.
- `results.json` records checks and failures; logs stay in the output directory.
- Virtual monitors do not test physical GPU or hotplug behavior.

[Lifecycle design](lifecycle-architecture.md)

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
processes with distinct synthetic rollout identities, seven native Chrome windows with 42
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
for cloud accounts. The VM payload is firmware only; the viewer shows a
synthetic SPICE display and does not prove Windows hibernation or any VM state
handling. Viewer launch and placement replay
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
It does not issue `Browser.close` or bypass the production final save.

Evidence stays under the guest's `~/.local/state/wsctl-scale/`, with separate
`cycles/<cycle-id>/` archives. Each passing result states the social fixture mode,
coverage limits, installed release and available social package versions.
The seed is frozen only after count validation;
the fixture checkpoint and the separate viewer target digests must remain
unchanged across each cycle. The canonical snapshot digest may change only when
the product itself rewrote it (periodic `auto-save` or `final-save` provenance)
and its content, placement, tmux intent and workspace names still equal the
frozen checkpoint; the comparison is recorded as `canonical-semantic-check.json`. Counts, exact synthetic conversation identities and tmux pane
ownership, folder/editor URIs, tab order and
group membership, saved workspace/monitor/frame placement, captured desktops,
checkpoint data, startup status and process replacement evidence are JSON.
Chrome's native group/window/tab IDs are compared before and after each restore;
a native-host readiness barrier records the browser's baseline before restoration
can mutate it. Chrome may assign new IDs when its browser process restarts. All
coordinator stages are required to be ready with verified provider receipts; the
status has no `virtual-machines` stage (VM profiles are retired). Social receipts require verified identity and placement; their
explicit content skip records that message/account recovery belongs to each
application and is outside this signed-out fixture. The running native and HUD
companions must expose the production extension identities and exact immutable
release revision; experimental extension identities are rejected. A new graphical
login is mandatory, and reboot mode requires a changed boot ID. On the same boot,
saved PID/start-time identities must be
absent for every captured native application and conversation process. Native
inventory includes minimized social windows and requires exactly 23 native
windows in total. Repeated cycles require the same counts, preventing duplicate
growth. Individual setup phases are
available for diagnosing a failed prerequisite; a failed phase is not a passed
scale run. This harness does not power off or reboot the host.

## Real power-off and the bounded final save

Shutdown is a bounded best-effort save, not a transaction. The session agent
(`wsctl-gnome-session.service`) holds a logind delay lock and never cancels a
shutdown. On `PrepareForShutdown(true)` (GNOME menu, `systemctl poweroff`,
`sudo shutdown now`) or GNOME `EndSession` it runs one `wsctl save --auto --final`
within `min(20 s, InhibitDelayMaxSec - 3 s)`, at least one second, kills the
process group at the deadline, and then releases the lock. A forced power-off
runs nothing; the next login restores from the last periodic snapshot, written
every five minutes by `wsctl-checkpoint.timer`. If the graphical session is
already being torn down, or login restoration is running, the final save
publishes nothing and the periodic snapshot stays current.

Copy `run_vm_poweroff.py` alongside `run_vm_scale.py` in the same disposable
guest. It uses the installed immutable release and refuses to run outside the
named KVM guest with the scale fixture's consent record. After seeding and
bringing up the complete desktop, run:

```sh
python3 run_vm_poweroff.py --disposable-guest --method systemctl-poweroff prepare
python3 run_vm_poweroff.py --disposable-guest watch                # --timeout defaults to 900 s
# While watch runs, power off the guest as the cycle requires (below).
# Power the same VM back on, then wait for startup to finish.
# Copy the actual host exit observation to poweroff/<run-id>/qmp-exit.json.
python3 run_vm_poweroff.py --disposable-guest verify
```

`--method` is one of `gnome-menu`, `systemctl-poweroff`, `sudo-shutdown` or
`abrupt`; preparation records it and verification checks that the journal
matches it. Preparation also records, from the guest, logind's
`InhibitDelayMaxUSec` and the agent's inhibitors (`systemd-inhibit --list`), and
any `shutdown-*.json` files an older coordinator left in the data directory; only
new files count later. Recording never refuses: a release before M5 holds a
`block` lock and runs the shutdown transaction, and it must still be preparable
for the upgrade cycle and the negative baseline. The lock mode (a `workspace-state`
`delay` lock on `shutdown`, no `block`) is judged only where the agent must be the
new one:

- acceptance cycles on an M5+ candidate (`run_vm_poweroff.py --expect pass`,
  `run_vm_tmux_names.py prepare` and `prepare-continuity`): preparation refuses to
  continue without the delay lock or with a `block` lock, before any capture;
- retained-checkpoint upgrade: preparation runs on the old release and only
  records; `verify` re-reads the logind facts after the new release booted and
  fails on a missing delay lock or a `block` lock (`upgrade-logind.json`);
- negative baseline (`--expect browser-retention-regression`, or
  `run_vm_upgrade.py verify --expect-failure`): the block lock is an expected
  divergence, recorded as `logind_expected_divergence` and never a failure.

The helper never requests shutdown, precloses applications, stops tmux or
invokes the older `cycle`/`reboot` shortcut. Its watcher records changed status
and canonical snapshots every 200 ms for 15 minutes by default (`--timeout`),
long enough for the abrupt cycle's five-minute wait, and any new
`shutdown-*.json` file in the runtime or data directory. It must already
be running before the power-off is requested. Keep the watcher alive
independently of the SSH connection when operating the VM console. An expired
watcher writes `failure-watch.json` (`names-failure-watch.json` for
`run_vm_tmux_names.py`; its verify reads both, and a new watch sets the old one
aside); verification fails on it. The runtime
directory is a fresh tmpfs after boot, so it is judged only by the watcher.

The final save does not capture Chrome. The periodic save reads Chrome by
window titles (milestone 8); preparation does not change Chrome tabs after the
manual save, so a periodic checkpoint keeps the same recipe. The expected `browsers` state is the manual save's recipe. Pass
`--mutate-chrome` only to add that later browsing change deliberately: the
oracle then keeps the live changed state, so a loss of the daytime Chrome
changes shows as a `browsers` difference. Milestone 8 keeps them only when a
periodic checkpoint ran after the change, during a login whose Chrome restore
was verified or accepted by a manual save. With
`--expect browser-retention-regression` that is a reproduced regression; with
`--expect pass` it fails.

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
distinct fixture conversation identities, seven Chrome windows with 42 tabs and three
groups, four Nemo windows, one Code window and one viewer. Every configured
desktop app must have exactly one visible captured and native window. The four
built-in social apps give 23 native windows total; configured extras such as
ChatGPT and Remmina raise that total to 25. Each native window must belong to
exactly one expected application. An extra bootstrap Alacritty, unmanaged
Firefox window, missing app or ambiguous identity causes a failure before the
desktop can be accepted as the new expected baseline. The same guard runs on
the final independent capture.

### Acceptance cycles

Acceptance is four cycles on the same disposable guest. Each is prepared, watched,
powered off, cold booted and verified separately:

| Cycle | `--method` | How the guest is powered off | Expected |
| --- | --- | --- | --- |
| 1 | `gnome-menu` | GNOME menu Power Off, or its equivalent through gnome-session (`gnome-session-quit --power-off --no-prompt`) | A `wsctl: GNOME EndSession` line and one final save |
| 2 | `systemctl-poweroff` | Unprivileged `systemctl poweroff` | One final save, `trigger=prepare-for-shutdown` |
| 3 | `sudo-shutdown` | `sudo shutdown now` | One final save, `trigger=prepare-for-shutdown` |
| 4 | `abrupt` | `systemctl poweroff --force`, or host QMP `quit`. Wait more than five minutes after preparation so a periodic checkpoint runs | No final save; restore from the last periodic or manual snapshot |

All three different clean methods and the abrupt one are required; the verify
report shows the method it judged.

`--method` is declarative. The report also carries `method_evidence`, read from the
prepared boot's journal: the agent's `wsctl: GNOME QueryEndSession|EndSession`,
`wsctl: logind PrepareForShutdown(...)` and `trigger=...` lines in order of
appearance, for `sudo-shutdown` whether a `sudo` record with a `COMMAND=`
containing `shutdown` exists, and a preliminary `conclusion` of `consistent`,
`unknown` or `inconsistent` (the menu shows `GNOME QueryEndSession`;
`systemctl poweroff` and `sudo shutdown` do not; an abrupt cycle logs no trigger
or `PrepareForShutdown(true)`). It is a record only and never changes PASS or
FAIL: the real signal order on the guest is not yet verified, and a failure rule
will be added after the first cycles. An unreadable journal gives `unknown`. The
report also names the top-level `pending` index of the restored snapshot as
`restored_pending_index`. The final save keeps items that vanished during it
(VS Code and other applications close on `PrepareForShutdown`, GNOME clients on
`EndSession`) in their categories, so the oracle compares category content and
the index is never a difference.

For cycles 1 to 3 verification requires all of the following, taken from the
prepared boot's journal (addressed by its boot ID, not `-b -1`) for the session
agent unit, the user manager and PID 1. An empty journal proves nothing: the
boot must show session-agent records, and clean cycles must show an orderly
shutdown line: systemd-logind's `System is powering down` (unit
`systemd-logind.service`) or PID 1's `Reached target shutdown.target` or
`poweroff.target`. The same text from any other source does not count:

- exactly one `wsctl: final save started; trigger=...; budget=...s` line, and
  exactly one of `final save finished` or `final save stopped at deadline`; an
  unforced cycle must show `finished` with `rc=0`;
- the logged `budget` equals `max(1, min(20, InhibitDelayMaxSec - 3))` for the
  delay recorded at preparation (within 0.1 s); the logged `elapsed` agrees with
  the journal's monotonic timestamps of the start and end lines within one
  second, and that measured gap does not exceed the budget by more than a second;
- the final save finished before the user manager's first `Stopping` line for an
  application unit (`app-`, `wsctl-app-`, `tmux-spawn-`, `snap.` or
  `org.gnome.Shell@`), so the save never raced application teardown;
- `wsctl: logind delay lock released` after the save;
- no `wsctl: GNOME CancelEndSession` line, and no `shutdown-*.json` file in
  the runtime or data directory, either seen by the watcher or left behind;
- the snapshot the next login restored from has `source: "final-save"` in its
  `category_provenance` for `terminals`, and a `created_at` no earlier than
  the logged start of the final save (one-second tolerance) and before the new
  boot. That snapshot is named by the `terminals` startup stage marker's
  `snapshot` and read from the canonical file or the recovery history, because
  the first periodic checkpoint after login rewrites the live canonical;
- a clean previous-boot user-manager stop, described below.

For the abrupt cycle verification requires session-agent records in the boot but
no final-save line, no orderly shutdown lines (logind or PID 1), no cancellation and no
`shutdown-*.json` file; a restored snapshot whose `terminals` provenance is
`auto-save` or `manual-save` and whose `created_at` precedes the QMP SHUTDOWN
event; and, from the `wsctl-checkpoint.service` journal of that boot, a last run
(`checkpoint saved` or `checkpoint unchanged`) after preparation and no more than
six minutes (the five-minute timer plus a minute) before the power-off. An
unchanged checkpoint is not rewritten, so its age is judged from the service
journal, not from `created_at`. A forced power-off runs no orderly user-manager
stop, so that check is skipped; a host QMP `quit` is accepted as the exit reason
for this cycle only.

Every cycle also requires a changed boot ID, the same installed release, and the
restore result: current canonical data, the last observed snapshot and live
application data are checked against the independent expected content, placement
and tmux identities. Native inventory includes every window, including added
ChatGPT or Remmina applications, so missing or duplicated windows cannot pass
through a fixed base-fixture count. Chrome window/tab/group IDs must remain
unchanged between native browser startup and workspace restoration. Every startup
stage must be ready (the status has no `virtual-machines` stage: VM profiles are
retired); provider identity, content and placement receipts are also checked.

A positive clean-cycle result also requires the previous boot's PID 1 journal
records for `UNIT=user@<uid>.service`, collected with a bounded, noninteractive
privileged read. Their boot identity must match preparation and a successful stop
job must be present. Timeout, failure, forced kill or main-process signal
termination fails the cycle even if a later journal line says "Stopped" and
startup is green. The structured receipt is archived as `verified-os_shutdown.json`;
the final-save judgement is archived as `verified-final_save.json`.

The host must supply the real `qmp-exit.json` observation in that run's directory:
`vm_name` must be `wsctl-ubuntu-validation`, `run_id` and `previous_boot_id` must
match preparation, and `qmp_eof` must be true. Preserve the actual QMP `events`,
UTC `observed_at`, and nonempty `systemd_state` output. Exactly one `SHUTDOWN`
event must identify `guest: true` with `reason: guest-shutdown`; its original
seconds/microseconds timestamp must fall after preparation and before the current
guest boot time. Missing, foreign or stale host evidence blocks a positive
result. The validated receipt is archived as `verified-host_qmp_exit.json`.

Use `--expect browser-retention-regression prepare` with an older release for a
negative baseline; its block lock is recorded, not refused (see above). A reproduced stale-browser overwrite reports
`outcome: expected-regression`, `passed: false`, the exact differences and any
other failures. It is never reported as a passing desktop. Use `--new-run` only
to explicitly supersede an unfinished run; old evidence remains under
`~/.local/state/wsctl-scale/poweroff/<run-id>/`. Missing shutdown observations are
recorded even when the archived snapshot proves the negative regression;
that outcome remains `passed: false`. The 23 conversation
workers remain synthetic. Real signed-out CLI launch is checked separately;
account sign-in and authenticated conversation resume are outside acceptance.

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

## Retained-checkpoint upgrade without manual save

`run_vm_upgrade.py` is a separate guarded scenario. The earlier power-off
fixture explicitly accepted a new baseline with manual save; that does not
exercise upgrading a retained old recipe whose newer native session is intact.

Copy this helper beside the scale and power-off helpers in the disposable guest.
Start with the complete real application fixture and the historical installed
release. No credentials or personal profiles belong in the fixture.

```sh
python3 run_vm_upgrade.py --disposable-guest --omit-original-ungrouped-window prepare
python3 run_vm_upgrade.py --disposable-guest watch
# Schedule the candidate through the normal deployment workflow.
# Power off the guest (any acceptance-cycle method); observe QMP shutdown and EOF.
# Cold-boot the guest, log into Ubuntu, and wait for startup to finish.
python3 run_vm_upgrade.py --disposable-guest verify
```

Preparation constructs a synthetic canonical recipe directly, without adoption
metadata. It then changes real browser tabs, proves the stale grouped recipe is
refused without mutation, and records that failed attempt. It never calls
`wsctl save`. The shutdown save (the old release's, or the final save) must
retain the original browser recipe unchanged; the harness requires no
`latest_observation` and no measured newer changes, because Chrome is not
captured by the shutdown save until the Chrome milestone.
Keep fixture preparation and any guest setup recovery separate from acceptance.
Use `--same-release-retry` explicitly for a later retained replay on the same
release; do not describe that replay as another version upgrade.

Once Chrome capture returns, the candidate may select a retained observation only after proving an exact,
unambiguous match of all native profiles, windows, ordered URLs, pinning and
group membership/metadata. Observation timestamps must belong to the complete
capture, including native placement. Runtime IDs and ordinal window labels are
not cross-boot identity evidence. Missing, extra or indistinguishable windows,
changed content, incomplete evidence or changed operation ownership must refuse
reconciliation and preserve both the checkpoint and native browser state.

Reconciliation uses existing native window IDs only: no replacement creation,
URL rewriting, group creation or duplicate cleanup. The full native catalog is
rechecked during reuse and after placement. A scoped receipt records checkpoint
and observation digests, native IDs and verification outcome. The canonical
recipe is not rewritten during startup; a subsequent independently successful
capture can save the current state while retaining recovery history. Terminal
autosaves preserve the original capture evidence in a separate witness bound to
the complete browser/observation digest. They do not assign a new capture time
to browser data or turn unknown provenance into permission to reconcile.

A passing upgrade needs genuine shutdown/boot evidence (the bounded final-save
journal criteria above), exact live content and placement, unchanged native IDs
throughout that boot's restoration, no extra windows, completed startup and a
verified receipt for the current operation. The release that ran the shutdown is
read from the lock mode recorded at preparation and the prepared boot's agent
journal: a `block` lock with no `wsctl: final save started` line after preparation
means the old release, reported as `shutdown_release: legacy` in
`upgrade-result.json`; otherwise `current` (a release activated by a later
logout/login ran the final save). A legacy shutdown ran the old transaction, so no final-save journal
lines or `shutdown-*.json` absence are required (never fabricate them); instead
the snapshot the new login restored must be the last one published before the
power-off (created after preparation and before the power-off, with no newer one
published before the new boot). A `current` shutdown needs the full final-save
evidence. An unfixed
negative run remains a product failure even when it reproduces the expected bug.

## Delayed content and placement: 2026-10-05

The retained-session upgrade had a second failure path. When the native
companion returned pending URLs, restoration deferred content verification but
never submitted placement. A later content observation could therefore verify
all tabs while leaving a failed placement without a compositor request.

Placement now has an operation-scoped continuation. It checks the original
window claim, complete native tab/group inventory and current startup ownership
before submitting the move. The observer only reads evidence. Cancellation,
changed content, a different owner and expired deadlines prevent late moves.
Login finalization waits for genuine pending receipts.

Terminal verification also records the actual restored tmux session, server
start identity and immutable pane IDs. Changed saved indices do not create a
second process for an already active conversation. A partial or ambiguous match
is preserved and reported rather than duplicated.

`run_vm_pending.py` gates synthetic loopback responses until at least three
production restoration calls have actually returned pending. It records both
the initial browser receipt and the final phases. The previous three-second
HTTP test finished inside the native restore call and did not cover this path.
The unfixed cold-boot trial reproduced pending content followed by verified
URLs and failed, unrequested placement. This is recorded as an expected
regression, not a passing product trial. Candidate cold-boot results are
recorded separately after validation.

The first candidate trial used source `7c02d04` in an immutable guest release.
After a real GNOME power-off and cold boot, the first three native returns had
waiting content and a scoped placement continuation. After the gate opened,
all three reached verified content and placement. The independent full-catalog
check passed: seven windows, 42 tabs, three original groups, 25 native windows,
ten tmux sessions and 23 exact synthetic conversation identities. No measured
manual save, replacement window or corrective placement was used. QMP confirmed
guest shutdown and the user manager stopped cleanly. This trial proves the delayed-content fix, not equivalence with
an authenticated account or the physical host's next login.

Those first pending trials kept all Chrome windows on workspace 0/display 0;
the other application windows exercised the remaining workspaces/displays.
They prove the missing continuation and receipt transition, not scattered
browser targets. Later scattered-browser checks are recorded separately.

The synthetic browser observer also required a protocol sequencing fix. Native
hello and explicit read-only requests pass through while mutations wait for a
durable original catalog. Frames, queues and diagnostics are bounded; truncated
EOF, unreleased requests and wrong running companion revisions fail the fixture.
Observer errors do not terminate the managed browser controller. These are test
infrastructure changes, not a substitute native host or product restoration.

The isolated renderer and the rest of the suite are independent of VM trials.
Those delayed-content trials ran under the retired shutdown transaction; the
retry, cancellation and drain trials recorded in earlier revisions of this page
belong to that transaction and are not repeated here. Current acceptance is the
set of cycles in [Real power-off and the bounded final save](#real-power-off-and-the-bounded-final-save).

## Retained-upgrade investigation: 2026-10-04

This section is a dated record. Its cycles ran under the retired shutdown
transaction (HUD countdown, drain receipts, Cancel and retry); current
acceptance is [the bounded final save](#real-power-off-and-the-bounded-final-save).

The repeated failure was a different path from the manual-save test below.
An older shutdown retained the previous browser recipe after a failed restore,
while capturing newer intact windows in `latest_observation`. On the next login,
strict grouped-window matching compared those windows with the older recipe and
refused them. Workspace and finalizer failures summarized that browser failure;
they were not three independent crashes. Successful release activation alone
could not reconcile this retained state.

The fix requires complete capture provenance and a unique exact match of the
entire live browser catalog to the newer observation. It reuses those native
windows without adopting a new canonical baseline. Ambiguous or changed input
still fails with a concrete explanation. Terminal-only autosaves preserve a
validated, digest-bound capture witness; they cannot manufacture capture proof.
Late workers lose mutation authority when their startup operation changes.

The real guest regression deliberately omits one ungrouped window from the
constructed old recipe while leaving it open. Thus the old recipe has six
windows and 38 tabs, but the real newer session has seven windows, 42 tabs and
three groups. This models the window-count difference as well as changed tabs.
No preparatory `wsctl save`, credential import or replacement-window cleanup
is allowed in this scenario.

Development results remain separate from acceptance:

- The unfixed `ef24480` release reproduced the failure through actual GNOME
  shutdown, cold boot and graphical login. All seven windows, 42 tabs and three
  groups survived; browser restoration still refused them. The result remains
  `expected-regression`, `passed: false`.
- A historical `cd42271` → `723de36` upgrade verified reuse of all seven browser
  windows with unchanged content and a scoped reconciliation receipt. The full
  cycle **failed**: that older release still hit the terminal user-manager stop
  timeout, and the viewer fixture started with inconsistent placement. The old
  release also lacked strict browser-exit telemetry. This is not an acceptance
  pass; subsequent preparation rejects the inconsistent viewer fixture.

The final runtime candidate is
[`437b038`](https://github.com/Sage-Cat/workspace-state/commit/437b03872110790b4a3ae86ebe89221081235973).
Its source suite passed 964 Python tests and the browser/editor companion checks.
Disposable native GNOME checks passed six placement and 17 HUD scenarios.
The source CI and commit-addressed release also passed; these checks are separate
from the full guest cycles.

The strict `ef24480` → `437b038` upgrade passed with the workload described below:
25 native windows, seven browser windows, 42 tabs, three groups, ten tmux sessions
and 23 synthetic workers. The observed countdown was 5.060 seconds. Acceptance
included the real QMP shutdown and cold boot, clean user-manager stop, native
browser exit before Shell teardown, durable application-drain receipts, exact
content/placement, unchanged canonical browser data and a current-operation
`verified-reuse-only` receipt. Running companion builds matched the package.

Five adverse probes used the real native host/companion or production reconciliation
validator: an extra catalog window, altered group metadata, altered URL, wrong
native window ID and ambiguous duplicate content. All refused mutation; native
window/tab/group identities and content stayed unchanged. Two real terminal
save hooks then preserved the entire retained browser object and original capture
witness; exact reconciliation remained eligible after both autosaves.

A second strict cold-boot cycle replayed a retained checkpoint on the same
candidate. It included a real HUD Escape cancellation followed by a new GNOME
Power Off request; cancellation preserved the native identities and content.
The successful retry showed a 5.004-second countdown. All 42 real HTTP responses
after the cold boot took at least three seconds. The complete inventory,
placement, clean shutdown and scoped receipts passed again. This was a replay,
not a second version upgrade.

A third cycle checked ordinary continuity on the same candidate. Preparation
only recorded expected/native/process evidence: no fixture reset, checkpoint
write, failure-marker injection, tab change or manual save. Healthy shutdown
captured the complete seven-window baseline and removed the obsolete retained
observation through the normal capture path. After a real cold boot, the exact
25-window inventory, browser content/groups, terminal state and placement passed.
Its countdown was 5.007 seconds. All three final cycles drained eight managed
units and exited the native browser before Shell; the measured margins were
102.291, 137.157 and 195.371 milliseconds respectively.

The real signed-out Codex 0.160.0 launch was repeated on the final guest boot in
a bounded popup inside an existing Alacritty/tmux client. The authentication
screen appeared without a new tmux tab, changed pane identities, credentials,
a sign-in attempt or model request. The 23 synthetic workers remain a separate
scale test. The disposable guest was shut down for cleanup after acceptance;
that cleanup is not another tested cold-boot cycle.

Runtime packaging used the public component revisions listed in the parent
pins: gnome-winctl `e0b35f8`, Login HUD `c0d3ed0`, hide-suspend `273bec2`,
input-source-popup-guard `3ef3555` and gc-profiled `748dac0`. The private cleaner
was excluded. Exact source digests, installed/running build receipts, QMP events,
per-window comparisons and failed runs are retained with the private evidence.
Later test-report-only commits do not change this tested runtime source.

## Recorded validation: 2026-10-03

This records completed tests of
[workspace-state ef24480](https://github.com/Sage-Cat/workspace-state/commit/ef24480e0974442fedb6c100be67c9bfbe84826b),
with the matching packaged native and HUD companions. It is a dated result,
not a promise that every later revision or hardware configuration has passed.
These cycles ran under the retired shutdown transaction (HUD countdown, drain
receipts and a seal of the snapshot); they are history, not current acceptance.
Raw logs, application profiles, transcripts and per-run identifiers remain
outside the repository; the procedures and synthetic fixture code are public.

### Acceptance and fixture

Acceptance covered real application launch while signed out, native windows and
placement, terminal/tmux layouts, synthetic browser tabs/groups, and an actual
GNOME power-off followed by a cold boot and Ubuntu graphical login. Application
account sign-in was not required. No credentials, private profiles, keyrings or
conversation histories were imported, and no model request was submitted.

The disposable QEMU/KVM guest used six virtual CPUs, 12 GiB of RAM, three
1280×800 virtual displays with monitor identities, and six GNOME workspaces.
The default seed uses four workspaces; the recorded run expanded that setup
before taking its baseline. The tested desktop stack was Ubuntu 24.04.5 with
kernel 7.0.0-34-generic, GNOME 46.0, Mutter 46.2, systemd 255.4 and QEMU 8.2.2. Package versions were
inventoried before freezing the fixture; refreshes were held during the cycles.

| Fixture item | Count | What ran |
| --- | ---: | --- |
| Alacritty windows | 6 | Real terminal windows |
| tmux sessions | 10 | Real tmux with explicit stable window names |
| Conversation workers | 23 | Synthetic processes and synthetic rollout identities |
| Chrome windows / tabs / groups | 7 / 42 / 3 | Real browser and companion; generated local HTTP content |
| Nemo windows | 4 | Real file manager and bridge; generated folders |
| Code window | 1 | Real editor and companion; generated project |
| Remote viewer | 1 | Real viewer connected to a firmware-only test VM |
| Other application windows | 6 | One each of Slack, Discord, Telegram, Viber, ChatGPT and Remmina |
| Total native windows | 25 | Exact inventory, including minimized windows |

The recorded application versions included Chrome 154.0.8037.57, Alacritty
0.13.2, tmux 3.4, Nemo 6.0.2, Code 1.139.1, ChatGPT 26.928.21956, Slack 4.52.162,
Discord 1.0.159, Telegram 7.2.9, Viber 27.3.0.2, Remmina 1.4.43 and remote-viewer 11.
These describe that run; they are not requirements to downgrade installed apps.

Real CLI launch was checked separately after the final cold boot. The official
Codex 0.160.0 executable rendered its signed-out authentication screen in a
25-second bounded popup inside an existing Alacritty/tmux client. The existing panes and
their process identities stayed unchanged; no additional tmux tab, sign-in or
model request was needed. Native windows and executable process identities also
confirmed real signed-out ChatGPT and Code launch. The 23 scale workers were
never counted as 23 real CLI launches or authenticated conversations.

### Reproduction and sequence

The original negative test ran an older release through the real power-off
path. After a failed browser restore, a manual save and subsequent browsing
changes, the shutdown checkpoint retained the manual browser baseline instead
of the later live state. The verifier recorded `expected-regression` with
`passed: false`. That failed cycle and earlier candidate passes were not counted
among the final three passing cycles.

Each final cycle used this sequence:

1. Validate the complete native inventory, capture independent application and
   placement state, and record the installed release and exact process identities.
2. Change the first tab URL in each browser window and replace one tab inside an
   existing group. Require seven windows, 42 tabs, the same window/group identities,
   and exactly one removed and one added tab identity.
3. Submit the stale full browser catalog with ungrouped windows first, then a
   stale grouped recipe. Both probes disable placement and require real refusal
   with unchanged live window/tab/group identities. They are explicit fault
   injection, not a claim that startup failed naturally in every cycle.
4. Record the controlled failed startup marker, run an ordinary full manual save,
   and verify that the failure evidence remains. Make further browser changes and
   rename an existing synthetic tmux window. Capture the new expected state
   independently without writing it to the canonical checkpoint.
5. Start the bounded watcher before using the VM console to power off the guest.
   The production shutdown path runs normally.
6. Observe the guest shutdown through host QMP, including its actual event and
   end-of-stream. Start the same VM, complete Ubuntu graphical login, wait for
   startup to finish, and run the strict verifier against the independent state.

The final run also retried Chrome restoration twice without placement and once
with placement. Window/tab/group identities and geometry stayed unchanged.
Changing observation timestamp, tab-title and capture-provenance metadata alone
did not change the semantic restore prefix. These checks establish reuse within
one browser boot; native Chrome IDs are allowed to change across boots.

### Failures found while testing

The earlier code could create replacement ungrouped Chrome windows when their
native originals had changed since capture. A real reproduction grew the
inventory from seven windows and 42 tabs to eleven windows and 58 tabs while
the three original groups remained. The final whole-catalog guard refuses
unresolved identity rather than guessing or creating a duplicate. The stale
catalog probes above exercise that refusal on the real browser.

Repeated cycles also exposed a compositor teardown race after earlier candidates
had passed: Shell began voluntary shutdown about 77 ms before the Chrome unit's
stop began. Native Chrome exited with code 1, and the next launch contained one
new-tab window instead of the session. Ordering systemd jobs with `After=` did
not prevent Shell from exiting voluntarily. The retired transaction answered
this with an owned application drain. The current design saves before any
application is stopped, and verification proves that order from the journal:
the final save finished before the user manager's first application stop.

A separate VM reproduction found terminal child processes surviving TERM and
causing a user-manager shutdown timeout. The tested compatibility rule applies
SIGHUP handling only to the relevant tmux-spawn scopes. This establishes the
reproduced path; it does not identify every possible cause of a historical
user-manager timeout.

The packaged Remmina agent was tested with a stale Unix socket left by a separate
test user at its exact revision-specific location. The real service reproduced its bind failure.
The guarded preparation helper then allowed normal startup; checks retained live
listening and bound-but-not-listening sockets. The service ended with a successful
runtime result and no restart loop. A vendor nonzero exit on ordinary stop was
recorded rather than relabeled. VM success does not install privileged system
integration on another machine; that installation is checked separately.

The fixture itself also needed correction. Its extra Python controller must
forward the real session TERM and record Chrome's actual exit, including natural
exit before the controller receives TERM. Slow tab creation can expose an empty
URL or an old URL with a pending navigation; the harness now waits for complete
navigation and checks all 42 intended loaded URLs. These fixture changes do not
relax production identity or placement verification. The restored `about:blank`
fragment-navigation crash described above remains a browser/fixture limitation.

### Final cycle results

All three final cycles used the same installed source build and restored the
complete 25-window inventory, ten tmux sessions and 23 synthetic workers.
Cycle 2 served three-second HTTP responses (all 42 requests took at least three
seconds). These three passes do not establish a failure rate or guarantee that
every future shutdown will succeed.

### What made a cycle pass

The criteria that still apply are independent of the retired transaction:

- PID 1's exact user-manager unit records showing successful stop with no timeout,
  failed result or forced termination. A later "Stopped" message cannot hide failure.
- A real QMP guest-shutdown event and EOF tied to the run and previous boot, followed
  by a different guest boot. A captured systemd state string alone is not exit proof.
- Exact semantic comparison of the snapshot, canonical and live state with the
  independent expected state: ordered URLs, group membership and metadata, files,
  editor state, tmux identities/names and saved placement. Native frames use the
  documented three-pixel comparison tolerance.
- The exact complete native inventory, healthy provider identity/content/placement
  receipts, and completed startup. Social-app content has the explicit
  application-owned skip.
- Unchanged Chrome window/tab/group identities between the native post-boot baseline
  and workspace restoration, with no replacement creation during retries.

Run archives contain independent expected/manual/snapshot/live captures, raw
post-boot Chrome launch and baseline observations, process identities, journals,
QMP events and the structured result. Digests and native placement were also
recomputed independently from the archived data. Source revisions and content
digests matched across all eight packaged components of the tested and staged
builds; differing local bundle identifiers were not treated as differing source
code.

### Other completed checks and limits

At the tested revision, `make check` passed 910 Python regression tests plus the
Chrome and VS Code protocol checks. The companion HUD checks passed 53 tests and
their syntax, metadata and package/release checks. The umbrella checks and real
headless GNOME integration also passed: six placement scenarios and the HUD
scenarios of that revision. Headless tests use real GNOME components in an
isolated session; they do not substitute for the complete VM power-off cycles.

The acceptance boundary is signed-out application launch and desktop restoration.
Authenticated conversation/content recovery, real cloud synchronization and
Windows OS hibernation were not required or demonstrated. The cloud mounts and
command-VM payload remain synthetic. Three virtual displays do not establish
physical GPU or hotplug equivalence. Temporary CDP registration and controlled
synthetic-profile cache provisioning do not prove migration of an existing
private browser profile or persistence of a user's extension installation.

Source publication, next-login deployment, privileged optional system setup and
the code currently running in an existing desktop session are separate checks.
No working host desktop was restarted, powered off, or rearranged to run these
tests. No claim about an unrelated screenshot-loop symptom is made without a
reproduction; screenshots here were bounded diagnostic observations.
