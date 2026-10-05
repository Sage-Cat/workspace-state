# Testing

The test suite separates local regression checks, isolated native GNOME tests,
and complete shutdown/boot cycles in a disposable Ubuntu VM. Application launch
is tested while signed out; account authentication is not an acceptance
requirement. Synthetic conversation workers provide scale without credentials
or model requests.

[Commands](#local-regression-checks) · [Headless GNOME](#native-window-placement) ·
[VM setup](#disposable-ubuntu-vm-at-desktop-scale) ·
[Power-off procedure](#real-gnome-power-off-and-browser-adoption) ·
[Recorded validation](#recorded-validation-2026-10-03)

## Local regression checks

From the workspace-state checkout:

```sh
make check
```

This runs Python regression tests, JavaScript syntax and Chrome protocol checks,
and VS Code companion tests. It does not shut down the working desktop. The
regressions cover capture/adoption provenance, exact browser claims and ambiguity,
loading/cancellation/retry, provider readiness, operation ownership, graceful
application drain, stale service sockets, and the VM verifier's false-pass guards.

The commands below use separate temporary state for native tests. Only the
explicit disposable-VM procedures change and shut down a complete guest desktop.

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
It does not issue `Browser.close` or bypass the real checkpoint/countdown.
The production coordinator, rather than the fixture, orders application drain
before allowing GNOME to tear down the compositor.

Evidence stays under the guest's `~/.local/state/wsctl-scale/`, with separate
`cycles/<cycle-id>/` archives. Each passing result states the social fixture mode,
coverage limits, installed release and available social package versions.
The seed is frozen only after count validation;
both checkpoint SHA256 digests and the separate viewer target digest must remain
unchanged across each cycle. Counts, exact synthetic conversation identities and tmux pane
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
distinct fixture conversation identities, seven Chrome windows with 42 tabs and three
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
fixed base-fixture count. Chrome window/tab/group IDs must remain unchanged
between native browser startup and workspace restoration. Every startup stage
must be ready except the explicitly documented synthetic VM-job skip; provider
identity, content and placement receipts are also checked.

A positive result also requires the previous boot's PID 1 journal records for
`UNIT=user@<uid>.service`, collected with a bounded, noninteractive privileged
read. Their boot identity must match preparation and a successful stop job must
be present. Timeout, failure, forced kill or main-process signal termination
fails the cycle even if a later journal line says “Stopped” and startup is green.
The structured receipt is archived as `verified-os_shutdown.json`.

Positive preparation also pins the fixture browser/controller generation and
the exact managed Chrome and GNOME Shell unit invocation IDs. Verification reads
a bounded previous-boot journal for those units and requires Chrome's successful
stop before the prepared Shell's own “Shutting down GNOME Shell” message or its
manager's stop-start record, whichever is earlier. A later Shell “Stopped” line
cannot conceal an earlier voluntary compositor exit. The fixture controller
must independently record native Chrome exit code zero before that boundary,
either after forwarding TERM or when it observes natural process exit. This is
fixture process-exit evidence, not a claim that private profile preferences were
read or that a real authenticated Codex session was exercised.

The operation-bound `shutdown-graphical-drain-<operation-id>.json` helper receipt
must be successful, settled, error-free, within its immutable deadline, and
contain the prepared Chrome invocation. Its completion must precede Shell
teardown, and the matching prepared marker must authorize that exact receipt.
The coordinator's exact durable helper and prepared-marker copies are archived
after boot if the watcher missed their final writes. An unobserved transient
intent is reported explicitly; an observed intent must match the same operation
and deadline. These proofs are saved as `verified-graphical_shutdown.json` and
`verified-graphical_drain.json`; green startup and a clean user-manager stop are
insufficient without them.

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
# Confirm GNOME Power Off in the guest console; observe QMP shutdown and EOF.
# Cold-boot the guest, log into Ubuntu, and wait for startup to finish.
python3 run_vm_upgrade.py --disposable-guest verify
```

Preparation constructs a synthetic canonical recipe directly, without adoption
metadata. It then changes real browser tabs, proves the stale grouped recipe is
refused without mutation, and records that failed attempt. It never calls
`wsctl save`. The old shutdown must retain its recipe and capture the newer
catalog as `latest_observation`; otherwise this is not the intended regression.
Keep fixture preparation and any guest setup recovery separate from acceptance.
Use `--same-release-retry` explicitly for a later retained replay on the same
release; do not describe that replay as another version upgrade.

The candidate may select that observation only after proving an exact,
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

A passing upgrade needs genuine shutdown/boot evidence, exact live content and
placement, unchanged native IDs throughout that boot's restoration, no extra
windows, completed startup and a verified receipt for the current operation.
Historical releases without application-drain receipts must be identified as
such; never fabricate the newer receipt for an older shutdown. An unfixed
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

## Retained-upgrade investigation: 2026-10-04

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
Discord 1.0.159, Telegram 7.2.9, Viber 7.3.0.2, Remmina 1.4.43 and remote-viewer 11.
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
5. Start the bounded watcher before using the VM console to confirm the real
   GNOME Power Off dialog. The actual coordinator, HUD renderer, checkpoint
   worker, countdown, application drain and GNOME handoff run normally.
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
not prevent Shell from exiting voluntarily. Final validation therefore requires
owned application drain after checkpoint/countdown and before GNOME handoff,
plus independent proof that native Chrome exited cleanly before Shell teardown.

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

| Cycle | Scenario | Measured countdown | Native Chrome exit before Shell | Result |
| --- | --- | ---: | ---: | --- |
| 1 | Normal power-off and cold boot | 5.054 s | 325.447 ms | Pass |
| 2 | Three-second HTTP responses, real HUD Escape, then shutdown retry | 5.088 s | 145.082 ms | Pass |
| 3 | Normal power-off and cold boot | 5.008 s | 103.975 ms | Pass |

In cycle 2, server logs showed all 42 requests taking at least three seconds;
the shortest measured duration was just over 3.0001 seconds. The real HUD Escape
action completed cancellation/recovery, followed by a separate successful
shutdown operation. Interrupted drain, late cancellation and replacement-worker
branches were covered by regression tests; they were not reported as additional
live cancellation trials. These three passes do not establish a failure rate or
guarantee that every future shutdown will succeed.

### What made a cycle pass

The verifier required all of the following, rather than relying on HUD color:

- Matching real renderer, worker and commit or durable authorization receipts,
  correct boot/login/operation ownership, and at least three seconds of countdown.
- A successful, settled, error-free application-drain receipt within the immutable
  deadline, bound to the exact Chrome unit invocation and durable prepared marker.
- Actual native Chrome exit code zero and successful managed-unit stop before the
  earliest matching Shell shutdown-start evidence in the previous-boot journal.
- PID 1's exact user-manager unit records showing successful stop with no timeout,
  failed result or forced termination. A later “Stopped” message cannot hide failure.
- A real QMP guest-shutdown event and EOF tied to the run and previous boot, followed
  by a different guest boot. A captured systemd state string alone is not exit proof.
- Exact semantic comparison of shutdown, canonical and live state with the
  independent expected state: ordered URLs, group membership and metadata, files,
  editor state, tmux identities/names and saved placement. Native frames use the
  documented three-pixel comparison tolerance.
- The exact complete native inventory, healthy provider identity/content/placement
  receipts, and completed startup. The synthetic command-VM transaction has an
  explicit validated skip; social-app content has the explicit application-owned skip.
- Unchanged Chrome window/tab/group identities between the native post-boot baseline
  and workspace restoration, with no replacement creation during retries.

Run archives contain independent expected/manual/shutdown/live captures, raw
post-boot Chrome launch and baseline observations, exact controller exit and
process identities, journals, durable drain/prepared receipts, QMP events and the
structured result. Digests and native placement were also recomputed independently
from the archived data. Source revisions and content digests matched across all
eight packaged components of the tested and staged builds; differing local bundle
identifiers were not treated as differing source code.

### Other completed checks and limits

At the tested revision, `make check` passed 910 Python regression tests plus the
Chrome and VS Code protocol checks. The companion HUD checks passed 53 tests and
their syntax, metadata and package/release checks. The umbrella checks and real
headless GNOME integration also passed: six placement and seventeen HUD scenarios.
Headless tests use real GNOME components in an isolated session; they do not
substitute for the three complete VM power-off cycles.

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
