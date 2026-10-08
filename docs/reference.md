# Configuration and command reference

[Overview](../README.md) · [Visual guide](visual-guide.md)

## Install

The startup-only **Важливе** tab collects persistent critical incidents from an
explicit inventory of your own programs, daemons and plugins. Private inventory
files may also name remote services. It excludes third-party applications and has
no shutdown role.
See [owned-system alerts](owned-system-alerts.md) for the narrow installer,
inventory, coverage limits and report/resolve API.

```sh
make install
```

Nemo integration needs Ubuntu packages `nemo-python` and `gir1.2-nemo-3.0`.
`make install-file-manager` installs only that integration without restarting
Nemo, GNOME or the session coordinator. See [file manager restoration](file-manager.md)
for behavior, safety checks and isolated verification.

`make install` stages one immutable production release from the current sibling
repositories and schedules its installation **before the next graphical login**.
It leaves this session's helper paths and loaded coordinator/HUD together until
logout. The next-login activation unit switches all owned paths transactionally;
a failed installation retains the previous release and does not block the desktop.
Only systemd unit definitions are reloaded when scheduling; no applications,
cleanup services, extensions, or desktop sessions are restarted or enabled.
Inspect source/installed/scheduled/running versions with `wsctl deployment doctor`.
See [deployment and rollback](deployment.md).

Python 3.11 or newer is required. Production `make install` rejects a nondefault
`PREFIX` before writing anything; isolated tests/install roots use the documented
`deployment.Locations` API. `make install-dev` explicitly retains the former
mutable-source setup and host/service activation. Component-specific legacy
install targets, including `install-session`, are explicit development/host helpers.
Keep GNOME on Wayland; enable any first-time GNOME extensions explicitly.

Branded Google Chrome does not permit command-line installation of unpacked
extensions. Install the companion once in every Chrome profile that wsctl should
manage:

1. Open `chrome://extensions` and enable **Developer mode**.
2. Choose **Load unpacked** and select
   `~/.local/share/workspace-state/chrome-extension`.
3. Open the extension options, keep the profile label unique, and enter its
   Chrome profile directory. The supported browser app ID is `google-chrome`.
4. Restart Chrome once so its service worker connects to the native host. After
   applying a scheduled release, use **Reload** on `chrome://extensions` (or
   restart Chrome) so the running service worker uses the new protocol. For a
   signed-in profile, the native host can resolve its real Chrome directory (for
   example `Default` or `Profile 1`) automatically; the options remain the
   fallback for unsigned profiles.

If Chrome currently registered the checkout directory, re-register **Load unpacked**
from the stable managed directory after the scheduled installation, using the
unchanged key/ID and checking the profile mapping. Reloading the checkout path
cannot activate the immutable release. Doctor flags this mismatch; preferences
are never rewritten automatically.

The fixed unpacked-extension ID is `gnccboicpdhhhpdcogeleiegokieocmn`.

Chrome restoration reuses native tab groups and never creates replacement groups.
Groups are recognized by their member tabs, so unnamed groups with the same color
remain separate. Retries preserve their existing group IDs.

Chrome's extension API cannot reopen closed saved groups from the bookmarks bar.
If Chrome has not restored an original group, its tabs can be restored ungrouped;
the HUD reports the missing group for manual reopening. No duplicate saved group
is created. Group membership and names are left as Chrome restored them.
See the [Chrome tab-group API](https://developer.chrome.com/docs/extensions/reference/api/tabGroups).

`make install` also installs the Alacritty startup wrappers. Both the desktop
launcher and the GNOME terminal shortcut can claim the first terminal launch of
a login. That first Alacritty attaches to `main`. If all saved tmux sessions are
already running (for example after working in a TTY), wsctl recognizes them
immediately; otherwise it gives Continuum a bounded start window before using
the canonical fallback. It then places the existing window and starts/places
only missing saved windows. Later terminal launches behave normally.

Tmux checkpoints also preserve each pane's explicit pane-local `@pane_label`
and its `pane_title`. The visible label is authoritative: restoration sets it
on the mapped live pane and initializes the title to the same name. Unlabeled
panes retain their captured title; applications can subsequently update that
title normally. No global border format, title hook, or window name is changed
to implement pane naming, including for panes inside `DEBUG_WINDOW`. Names are
reapplied after tmux-resurrect adoption as well as wsctl's fallback recreation.
Older checkpoints without these fields leave existing pane names untouched.
If additional live panes make the saved index mapping ambiguous, reconciliation
fails before mutation rather than assigning a saved name to an inserted pane;
save the updated layout before restoring it. Pane names do not change the exact
terminal session UUIDs used for resumption.

The enabled `wsctl-gnome-session.service` makes this recovery automatic after a
GNOME login, without waiting for the first manually opened terminal. The unit is
ordered after GNOME Shell's Wayland service and still verifies the compositor,
workspace names, and stable display topology before publishing the first HUD
status or launching restoration. The HUD and wsctl do not configure, restart,
or delay NVIDIA; display readiness is observational only.

The same coordinator registers as a GNOME session client. For ordinary Power
Off and Restart, GNOME first shows its native confirmation dialog. Only after
the user confirms does the HUD defer GNOME's exact final signal and hand
preparation to a separate managed service. GNOME's earlier `QueryEndSession`,
emitted while the native confirmation is open, is passive and cannot start
work. While the graphical session is active, the coordinator holds a logind
`block` inhibitor. It releases that descriptor only after the exact managed
worker, painted HUD, countdown, and private authorization markers have all been
verified. This also prevents an ordinary unprivileged `shutdown now` or
`systemctl poweroff` from bypassing the checkpoint. A privileged forced
shutdown can still override logind inhibitors and must be treated as an
emergency path.

The worker refreshes tmux-resurrect, runs `wsctl save` while Chrome and
Alacritty are still available, and then executes configured shutdown profiles.
An isolated unresolved live terminal session UUID becomes a visible degraded checkpoint
instead of discarding every other current window. If current Chrome capture is
incomplete, shutdown retains the previous verified browser category. Missing
GNOME placement, broken tmux capture, an absent last-good browser category, or a
failed critical profile still fails closed and leaves the inhibitor active. The
worker does not stop GNOME, mounts, cloud drives, NVIDIA, or unrelated user
services. If the Shell extension is unavailable, GNOME's own `EndSession`
request releases the coordinator normally and follows Ubuntu's shutdown path.

The Login HUD appears as always-on-top, non-modal GNOME Shell chrome only once
per OS boot: during the first GNOME login after power-on or restart, and only
after GNOME Shell is active and the coordinator publishes a complete valid
status for that exact GNOME Session Manager instance. Logging out and back in
during the same boot still runs workspace reconciliation in the background but
publishes `show_startup_hud: false`, so no startup HUD chrome is created. The
persistent claim under `~/.local/state/workspace-state/` stores only the kernel
boot ID and claiming GNOME session ID. A coordinator restart in that same first
session retains the visible HUD, while a changed kernel boot ID permits the next
startup HUD.
Until then the extension remains hidden and installs no Shell chrome, stage
listeners, timer, or input grab. `wsctl-gnome-session`, the startup transaction,
and the post-workspace finalizer atomically publish real milestone states and counts to
`$XDG_RUNTIME_DIR/workspace-state/login-hud-status.json`. The private full-login
diagnostic log is `login-hud.log` in the same directory. Successful completion
shows only an **OK** button; any unrecovered failure remains visible with a
**Show full error log** button. Status publication is best-effort and can never
block restoration.

After native confirmation, the same Shell HUD switches to a GNOME system-modal
shutdown mode. It is raised above regular, popup, and fullscreen windows and
takes the full keyboard/pointer grab used by system dialogs. GNOME's exact
original action remains deferred while a managed worker checkpoints
tmux-resurrect, captures the final Alacritty/Chrome desktop recipe, and prepares
each applicable profile. Cloud drives, metadata workers, GNOME, NVIDIA, and
other system components remain untouched for Ubuntu to stop normally after
handoff. The coordinator verifies that exact systemd invocation, waits for the
ready HUD to be physically painted, and enforces a visible five-second countdown.
After commitment, it closes proven workspace-state graphical app units while
GNOME Shell is still alive. Only successful, bounded completion and a fresh
authorization check permit the final marker. The extension then calls
GNOME's saved confirmation exactly once. A checkpoint or critical-profile
failure keeps the HUD and full-log button visible, releases the modal grab, and
cancels the pending GNOME action.

The countdown cancels if the HUD becomes hidden, loses its allocation or enters
the lock/greeter screen before handoff. Verification/paint/commit/EndSession
milestones are recorded in the user journal for the next boot's diagnosis.

`make install-tmux-lifecycle` installs a separate Ubuntu-owned tmux stop hook.
Its start action is inert; it never changes live sessions or participates in a
HUD profile. Only when the **system** manager reports `stopping` may its stop
action ask the validated current-user default tmux socket to close normally.
This prevents terminal-owned daemonized tmux from being left behind until the
user manager's five-second forced kill. Normal unit stop/uninstall/relogin is a
no-op for tmux. The helper refuses unknown identities and nonstandard sockets;
it never sends direct process signals or changes the vendor system timeout.

**Cancel shutdown** and `Esc` remain available throughout the active modal phase.
The request is bound to the current random shutdown operation ID. One action
cancels GNOME's original dialog exactly once and stops the exact managed worker.
Before every profile mutation, the worker persists private, operation-bound
rollback instructions. The systemd unit's `ExecStopPost` finishes recovery even
if the worker was interrupted or had already exited. The journal is disarmed
only when GNOME emits final `EndSession`; a cancelled or rejected handoff first
restores every prepared job. The HUD reports recovery and becomes terminal only
after the rollback journal is empty.

Cancellation after application closing has begun withdraws shutdown permission
immediately, but retains operation ownership until the pending stops settle.
Profile recovery does not reopen closed applications; the HUD reports that
separately and preserves the checkpoint. A coordinator restart rejoins the same
recorded application stops instead of issuing a second set.

Separate opt-in Ubuntu service shutdown-order/exit-status corrections are
documented in [the shutdown compatibility guide](shutdown-compatibility.md).
Use `make check-shutdown-compat` to verify staged definitions without changing
the live system. They are not HUD jobs and are not installed by default.


## Commands

```sh
wsctl save                 # replace the one saved state
wsctl show --details       # inspect it
wsctl restore              # terminals, browsers, visible social apps, pending VM
wsctl restore terminals    # terminals only
wsctl restore browsers     # Google Chrome only
wsctl restore social-apps  # built-in and locally configured desktop apps
wsctl restore virtual-machines  # retry a pending post-shutdown VM restore
wsctl restore --dry-run    # do not change the desktop
wsctl startup              # restore once per login, starting missing apps
wsctl tmux configure       # install the resilient terminal-session restore mapping
wsctl shutdown-profiles list --probe
```

To check saving separately from power off, run `wsctl save` and inspect
`wsctl show --details`. A normal save captures the current desktop and explicitly
accepts it as the new baseline. It does not request shutdown, close applications
or clear failed restore markers. It refuses incomplete capture and unresolved
conversation identities; `--allow-partial` is not a complete-save verification.

Wait for cancellation recovery to finish first. A current shutdown transaction
or armed profile rollback blocks manual save even when application draining has
not started. Fix the reported recovery problem before retrying; deleting its
journal or clearing the HUD does not prove recovery.

For unresolved older native clients, `wsctl save --verify-idle-codex` explicitly
allows a bounded `/status` query in each affected pane. The client must be idle
with an empty composer. Draft input, active work, copy mode, ambiguous output or
changed ownership refuses the save; input is never cleared or interrupted.
The query makes no model request. Leave those composers untouched during the
check. This option is never used by automatic capture or shutdown.
Unsupported rendering, pane resizing or a byte-identical repeated report refuses
the save and preserves the checkpoint. A refusal may leave the exact `/status`
literal in the composer; it never submits or clears an unrelated draft. Use a
larger visible pane for a separately authorized retry, or resume the known exact
UUID. The flag does not make ambiguous terminal evidence acceptable.

`restore` also supports `--workspace NAME`, repeatable `--session NAME`,
`--select`, and `--no-place`. `startup` uses markers under
`$XDG_RUNTIME_DIR/workspace-state/startup-<boot-id>-<login-id>/`, so the Alacritty launcher
and tmux hook can safely trigger it together. `startup --force` deliberately
runs it again during the same login.

New and reused Alacritty windows share the same verified placement path. For an
inactive destination workspace, the exact window is first placed on the current
workspace, its monitor/state/frame are allowed to settle, and only then is it
sent to the saved workspace. A GNOME `placed: true, deferred: true` response is
not proof of completion: the observed final workspace, display and state must
remain correct for one second before the HUD counts that terminal as restored.
Each window has a bounded 12-second placement wait with expected/observed details
on failure. A known but disconnected saved display is an error, not silent
success on the primary monitor. Already-correct windows are verified without
moving them, and this procedure never restarts tmux or its panes.

`save` refuses to replace the recipe if the GNOME or Chrome companion is
unavailable, a window lacks placement, or a live terminal session UUID cannot be resolved.
Use `--allow-partial` only when incomplete state is intentional. Continuum's
tmux hook can still refresh terminal state while Chrome is closed; in that case
it preserves the last saved browser and social-app categories.

A successful explicit `save` accepts each fully captured application category as
the baseline for the current login. Later shutdown captures can then record new
tabs and intentional closes even if that application's earlier startup failed.
The failed startup result remains visible. With `--allow-partial`, failed browser,
file-manager and VS Code captures retain their previous recipes and are not newly
accepted. Acceptance is bound to the boot, login and exact saved category data;
a terminal-only autosave cannot accept an application category.

`show` distinguishes the checkpoint update time from each category's capture
time. The JSON `category_provenance` records carry capture evidence, content
digests, explicit acceptance and retention reasons. Terminal-only autosaves
preserve this evidence and any preservation warnings. Capture times for legacy
categories without trustworthy evidence remain unknown.


## Desktop app visibility and placement

The HUD has a separate **Desktop apps** job for Slack, Discord, Telegram, Viber
and locally configured apps
with per-app activity logs and progress. Its shutdown counterpart checkpoints
the same apps' visibility and placement as part of the durable workspace save.
Each app records whether its process was running and one of three modes:

- `windowed`: at least one non-minimized normal GNOME window, on any workspace.
  Launch only if needed; reuse existing windows and restore their named workspace,
  physical display, geometry and window state. Windows on an inactive workspace
  still count; a window need not be focused or unobscured.
- `background`: running with no such window (including tray-only/minimized apps).
  Do not launch or raise it.
- `stopped`: not running. Do not launch it.

Restore never kills an independently running app and never sends messages.
It uses installed desktop launchers and independent graphical service ownership.
If a launcher replaces its splash/updater window (as Discord does), restoration
follows the replacement instead of waiting on the vanished window ID. Newly
launched windows must keep their verified placement for three seconds; retries
share the original per-app deadline and never relaunch the app. Activity logs
identify window replacements and report the observed placement on timeout.
One app's failure remains visible in the HUD but does not skip the other apps or
block the subsequent drive handoff. An absent saved monitor/workspace or missing
secondary app window is an explicit failure, not a guessed new destination or
false success. Unsupported app-specific pop-out creation is not fabricated.
Old checkpoints without `social_apps` launch none of these apps until a full
save records their actual state. Terminal-only autosave preserves this recipe.

For a non-disruptive inspection or isolated reconciliation (no host power action):

```sh
wsctl show --details
wsctl restore social-apps --dry-run
wsctl restore social-apps
```

Additional desktop apps can be registered in the private file
`~/.config/workspace-state/desktop-apps.toml` (`XDG_CONFIG_HOME` is respected):

```toml
[[apps]]
id = "notes"
label = "Notes"
aliases = ["org.example.notes"]
desktop_ids = ["org.example.Notes"]
executables = ["notes"]
```

Use the app IDs reported by `gnome-winctl windows`. Desktop IDs name installed
`.desktop` launchers without the suffix; executable names identify background
processes. IDs must be unique and must not overlap a built-in app. No commands
or application credentials belong in this file.

The next save captures these windows using the same visibility and placement
rules. Adding an entry alone does not launch it: an older checkpoint with no
saved entry is skipped. Removing configuration for an app still present in the
checkpoint reports an error instead of guessing a launcher. App content and
sign-in remain the application's responsibility. The HUD lists this category
as **Desktop apps**; the stable CLI name remains `social-apps`.

Install the matching runtime before saving additional app records. Older
releases that only support the four built-in apps cannot read those records.

This policy governs wsctl's launches. Independently configured application or
desktop autostart is a separate launch source; disable any conflicting autostart
entry if it should follow the checkpoint instead.


## Shutdown profiles

Shutdown profiles are TOML files in
`~/.config/workspace-state/shutdown-profiles.d/` (and optionally
`/etc/workspace-state/shutdown-profiles.d/`). Files must be regular, owned by
the current user or root, and not writable by group or others. Profile IDs are
unique and become expandable HUD jobs. A critical profile must prepare and
verify successfully before Ubuntu receives the saved Power Off or Restart
action.

The installed Windows workspace VM uses the structured
`qemu-windows-hibernate` adapter. Install or refresh it with:

```sh
wsctl shutdown-profiles install-qemu-windows \
  /absolute/path/to/windows-vm --id windows-word-vm --force
wsctl shutdown-profiles list --action poweroff --probe
```

The adapter is active only when its exact QEMU process is live. It verifies the
PID, executable, disk and QMP socket, requires QMP `running` plus a responsive
QEMU Guest Agent, sends Windows `shutdown.exe /h`, and waits for the original
QEMU process to exit. Cancellation never kills hibernation halfway through. It
lets Windows finish, then runs the VM's protected `launch.sh` and waits until
both QMP and QGA are ready again.

The protected launcher runs in its own `wsctl-vm-<path-hash>.service` user
service, with QEMU as the tracked main PID. It is **not** a child of the
short-lived restore worker, nor tied to `graphical-session.target`; finishing
restoration or logging out must not terminate Windows. A missing viewer is
recovered through the VM's canonical viewer supervisor, never a parallel viewer.

For an explicitly requested real guest-only test, with Ubuntu kept running:

```sh
wsctl shutdown-profiles test windows-word-vm --restore-only
wsctl shutdown-profiles test windows-word-vm
```

The second command captures actual placement, hibernates Windows, verifies
QEMU exit, resumes Windows and checks the actual viewer workspace, physical
display, geometry and state. Both commands leave Windows running. Explicit
`--restore-only` uses the matching committed placement even if QEMU is already
running; the round-trip test preserves the current visible viewer placement.
A stopped VM requires a matching saved placement. A running VM with no viewer
must first use `--restore-only`, not accept a new window's temporary placement.
Neither invokes the host power/session coordinator nor changes its startup
receipt or shutdown transaction. Private step/error/recovery reports remain in
`~/.local/state/workspace-state/profile-tests/`. SIGINT/SIGTERM request
cancellation; an in-progress hibernation finishes before recovery is attempted.
A failure is not a successful test even if recovery subsequently succeeds.

Before hibernation, the adapter also resolves the one verified `remote-viewer`
process to its stable `gnome-winctl` window ID and records the named workspace,
window geometry/state, and physical monitor EDID/serial identity in the private
write-ahead journal. The coordinator captures this placement into a private,
operation-bound preflight record before publishing status that lets Shell make
the HUD modal. The worker verifies the same QEMU and viewer process identities
and therefore never depends on GNOME placement RPC while the system-modal HUD
owns input. That record becomes a durable startup receipt only at
GNOME's final `EndSession` point of no return. A cancelled or failed shutdown
removes it while rolling the VM back. On a later kernel boot, wsctl launches the
VM once per kernel boot, remaps the saved workspace name and physical display,
synchronizes the VM viewer's connector placement, and verifies the exact GNOME
window. The durable active intent remains valid across an unexpected reset, so
the VM is recovered again on the following boot; a later clean managed shutdown
replaces it with the VM's then-current active or inactive state. It never runs
during a same-boot relogin, and a VM that was not active at the confirmed clean
shutdown is not started. If the saved physical display is absent, VM launch
fails visibly instead of silently using another monitor; terminal/browser
restoration and cloud-drive startup still continue.

Arbitrary integrations use the `command` adapter. Commands are exact argv
arrays; the first element must be an absolute executable path and no shell is
used. Exit status 3 from `probe` means “not active”; zero means applicable. The
`prepare`, `verify`, and idempotent `rollback` commands must all succeed with
zero. Example:

```toml
schema_version = 1
id = "example-service"
label = "Example service checkpoint"
adapter = "command"
actions = ["poweroff", "restart"]
critical = true
timeout_seconds = 120
rollback_timeout_seconds = 120
cancel_policy = "terminate-then-rollback"
probe = ["/absolute/bin/examplectl", "is-active"]
prepare = ["/absolute/bin/examplectl", "checkpoint"]
verify = ["/absolute/bin/examplectl", "checkpoint-status", "--ready"]
rollback = ["/absolute/bin/examplectl", "resume"]
```

Use `finish-then-rollback` only when interrupting `prepare` could corrupt the
managed application. Profile configuration is snapshotted into the private
runtime rollback journal before `prepare`, so later edits cannot change an
in-flight recovery command. Profiles never replace Ubuntu's ordinary service
teardown; they prepare only applications that need explicit pre-shutdown state.


## tmux-resurrect and continuum contract

The installed tmux configuration uses these hooks:

```tmux
set -g @resurrect-processes '"wsctl-codex->wsctl-codex-resume *"'
set -g @resurrect-hook-post-save-layout '~/.local/bin/wsctl tmux save'
set -g @resurrect-hook-pre-restore-all '~/.local/bin/wsctl tmux begin "$$"'
set -g @resurrect-hook-post-restore-all '~/.local/bin/wsctl tmux restore'
set -g @continuum-restore 'on'
set -g @continuum-save-interval '15'
set -g @resurrect-save-script-path '/home/example/.local/bin/wsctl-continuum-save'
set -g @resurrect-restore-script-path '/home/example/.local/bin/wsctl-continuum-restore'
```

The post-save-layout hook receives the resurrect state-file path as its final
argument. `wsctl tmux save FILE` maps each pane to the root session UUID
recorded in its live rollout metadata. Open child-session records are normalized to
that same root UUID, and conflicting rollout identities make the pane
unrestorable rather than selecting an arbitrary child. The hook stores a compact
`wsctl-codex UUID` command. Resurrect's documented `->`/`*` expansion turns
that into `wsctl-codex-resume UUID`. The wrapper resumes the exact session
inside resurrect's existing interactive pane shell. When the resumed application exits or cannot
resume, the wrapper returns to that same shell instead of spawning a nested
second shell, so the restored tmux pane/window remains usable without a
`zsh -> zsh` process chain.
Automatic resume inherits the terminal application's global configuration and
shared background service. A per-application-data-directory startup
lock serializes initialization, releasing
after thread ownership is verified rather than holding it for the session's
lifetime. Only an early SQLite initialization lock error from the current attempt
is retried, at most three times. Other exits return to the existing shell.
Missing saved directories wait outside that lock; cloud-backed directories
require their mount first. Desktop restoration can then proceed to mount startup,
and the login finalizer rechecks the deferred sessions. A slow live terminal
process is left intact and reported unverified, never killed to force a retry.
`make install-dev` (or explicit `wsctl tmux configure`) updates this one mapping both in the persistent tmux config and
in the live tmux server; `make uninstall` removes only that managed mapping.
If a terminal session identity is not provable, wsctl deliberately
leaves that process unrestorable instead of starting an unrelated session.

The same hook autosaves the terminal category every 15 minutes. Continuum
restores tmux at boot, including the contracted session-resume commands, and resurrect's
post-restore hook calls `wsctl startup` to restore application windows and
Wayland placement. The Alacritty trigger is a fallback and shares the same
startup lock. Bootstrap Alacritty uses a main-process-only transient unit, so
systemd cannot adopt and later force-kill the long-lived tmux server during
graphical teardown. Continuum uses a small wrapper that owns an explicit lifecycle
marker, so the launcher fallback cannot touch tmux while resurrect is still
rebuilding a large layout.

The GNOME session client starts only after the Wayland Shell service is active,
and it does not launch Alacritty or Chrome during compositor startup. Login
restore starts only after `graphical-session.target` is active,
`gnome-winctl` reports valid physical display identities with no active monitor
recovery, every saved workspace name exists, and the complete monitor/workspace
topology has remained unchanged through a short post-handoff settle interval.
The same readiness gate is enforced again inside the serialized startup
transaction, so neither terminal placement nor browser restoration can bypass
it. The launcher performs exactly one full transaction. It never automatically
replays a failed pass whose application windows may already be live. Terminal session UUID
verification has a bounded allowance (at least 15 seconds, five seconds per
saved session, capped by `--wait`); unverified sessions are reported
as degraded while Chrome, placement, and cloud-drive startup continue. Check
their panes for startup errors or prompts. A live process with a UUID on its
command line alone is not proof of a successful resume: verification requires
its open rollout, or a ready TUI with the exact thread loaded in the local daemon.
A repeated startup pass preserves this session-verification result rather than treating the
terminal-layout completion marker as proof of a successful resume.
Automatic checkpointing stays disarmed while terminal-session restoration is incomplete,
so failed or waiting resumes cannot overwrite saved UUIDs with plain shells.
Its worker, bootstrap terminal, and direct restore fallback run in
separate transient user services. Every restored Alacritty or Chrome process
tree gets its own
graphical-session service with `ExitType=cgroup`, so finishing a bounded restore
worker cannot kill an application's tmux client, renderers, or GPU process; the
application units still stop normally with the graphical session.
After terminal, browser, and conditional VM restoration finish, wsctl starts
the static `wsctl-workspace-restored.target`. Services that must not compete with
login can
use `WantedBy=wsctl-workspace-restored.target`; the target is never enabled at
boot and is stopped with the graphical session. Restore journals are scoped to
the current GNOME Session Manager instance, so a same-boot re-login cannot reuse
the preceding session's completion state.

`wsctl-login-finalize.service` is ordered after that target. It starts Google
Drive, the private Nextcloud drive, and Proton Drive together, waits for each
mountpoint to be verified with `findmnt`, then runs the first metadata warm-up
and starts its hourly timer. Explicit legacy `make install-session` host setup removes those four units from
`default.target` and gives them a `Requisite`/`After` gate on the workspace
milestone. Consequently a fresh boot never lets network-backed mounts compete
with GNOME, tmux, Alacritty, terminal sessions, or Chrome restoration. The mount services
remain persistent after they have started; a later same-boot login can report
an already-mounted drive immediately.

The HUD presents GNOME/Wayland plus display/workspace readiness as one job,
tmux-resurrect plus Alacritty/tmux reconciliation as one job, and the three
mounts plus metadata warm-up as one **Cloud drives and metadata** job. Every HUD
job can be expanded to show its bounded, timestamped activity log and internal
substeps. A separate **Windows VM restoration** job reports whether a committed
hibernated VM was skipped, restored, or could not reach its exact saved display.
Shutdown shows the workspace capture jobs followed by each applicable profile.
The countdown starts after the capture worker finishes successfully, without
a separate checkpoint-integrity job.

The save wrapper holds a process-lifetime lock around resurrect's complete save
and suppresses duplicate saves in the same second. This closes the filename
collision where resurrect could point `last` at a candidate and then delete that
same file. Rebind resurrect's manual save key to the wrapper after TPM loads so
manual and continuum saves share the lock:

```tmux
bind C-s run-shell '/home/example/.local/bin/wsctl-continuum-save'
```

Automatic saves are armed only after terminals have been restored successfully
for the current login, or after an explicit `wsctl save`. Until then, the save
hook makes continuum retain the previous `last` state. This prevents the first
partial login state from overwriting the recipe that is still needed for
recovery.

Terminal capture failures, dangling client/session references, and an
unexpected empty autosave are rejected. Each successful replacement keeps a
private rolling copy at
`~/.local/share/workspace-state/recovery/current.last-good.json`.


## Browser and desktop integration

Chrome communicates through a native-messaging host and a private Unix socket
under `$XDG_RUNTIME_DIR/workspace-state/`; wsctl does not parse Chrome's private
session files. After the one-time companion installation, startup launches each
missing saved profile with its exact `--profile-directory` and
`--restore-last-session`, then waits until Chrome's own window set settles.
The settle check tracks window IDs and ordered tab identities; page titles,
loading flags, focus, and geometry cannot prolong this structural wait.

For each saved window, the companion first claims an unclaimed live window by
its full ordered tab URL/pinned-state fingerprint and window type/incognito
identity. A homepage never substitutes for a saved session path, query, or
fragment. Newly created and reused windows must expose matching committed URLs
with completed loading; pending URLs and discarded/unloaded tabs do not prove
readiness. When the complete window identity still matches the saved recipe,
restoration activates lazy tabs to load their existing URLs and then restores
the previously active tab. It does not reload already loaded tabs.
New/reused results require two successful observations within a bounded polling
period. This verifies URL/loading state, not HTTP content or arbitrary later
redirects. Failed windows retain their restore token and ID for inspection and
retry without navigating user tabs or creating duplicate windows. URL verification
failure does not prevent placement of the identified window or restoration of
later windows. The HUD reports the verified count and retains the failure;
duplicate cleanup requires every saved window to succeed. `wsctl` temporarily activates a
private marker tab in that Chrome window, focuses it through `chrome.windows`,
and observes the one active marker-titled native window to obtain its exact
stable `gnome-winctl` ID. That existing window is moved and verified against the
saved workspace, physical monitor, geometry, and state. Only a saved fingerprint
that Chrome did not restore causes a new window to be created through the
sequential expectation mechanism. When wsctl starts Chrome at login, it freezes
the IDs returned by Chrome's native session restore. After every saved window
has one distinct, verified live keeper, wsctl closes only fully loaded windows
from that frozen set whose full fingerprint matches a keeper. Unrelated windows,
unverified/loading tabs, already-connected profiles, and windows created after
the frozen set was taken are excluded from automatic cleanup.

The CLI requires companion protocol version 2 and all focus/identification
capabilities plus `exact_url_restore` and `lazy_tab_restore` before it starts
placement. Completed
login journal entries are rechecked against exact live URLs before being reused.
The explicit `repair_restored_tabs` companion RPC is available for manual
recovery of a redirected saved URL. It requires the inspected live window ID
and full signature, validates unchanged tab identities before each write, and
uses the saved snapshot URLs. Startup never invokes URL repair automatically.
After an extension reload clears session claims, manual adoption also requires
the inspected signature and saved window, and cannot replace another claim. Per-window login journals make a
partial browser retry idempotent. Periodic tmux/Continuum autosaves preserve the
durable browser recipe unchanged; Chrome is captured only by explicit/full
saves, including the strict GNOME end-session checkpoint.
When an application's startup did not complete, the shutdown checkpoint retains
its previous nonempty recipe and reports that preservation as degraded. A
successful startup or explicit acceptance of a complete capture in the same
login permits later intentional closes to be saved normally.
Full saves and tmux autosaves also refuse to replace a saved physical-monitor
layout with an explicit fallback output such as `None-1`; the existing checkpoint
remains available until the display driver recovers.

`gnome-winctl` exports window capture, stable-window placement, and sequential
expectation/status methods at `org.sagecat.GnomeWinCtl1`. `wsctl` uses its JSON
CLI as the integration boundary so the placement service remains independently
useful to other tools.
